#!/usr/bin/env python3
r"""Lint `claude` CLI invocations inside synced shell scripts.

Scope (the scan set is the union of):
  - every `.sh` file tracked under `.claude/skills/` (SCOPE_DIRS), AND
  - every `.sh` `source:` entry in `scripts/sync-targets.yml`.

Every `claude` invocation (or use of `--permission-mode` / `bypassPermissions`)
inside that scope must sit inside a `# claude-cli-invocations:start` / `:end`
marker pair, and the bytes between those markers must hash to a
`(sha256, path)` entry in `.claude/claude-cli-invocations.allowlist`. Any
change to a locked region — flags, fallback literal, surrounding glue —
breaks the hash and fails CI, forcing the edit to be reviewer-visible.

Usage:
    python3 .claude/lint-claude-cli-invocations.py              # scan in-scope files
    python3 .claude/lint-claude-cli-invocations.py --self-test  # run unit fixtures only
    python3 .claude/lint-claude-cli-invocations.py --compute-hash <path>...
        # print each marked region's hash — use when rotating allowlist entries

Exit codes: 0 clean, 1 findings, 2 usage/internal error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SCOPE_DIRS = [".claude/skills"]
SCOPE_SUFFIX = ".sh"
ALLOWLIST_PATH = ".claude/claude-cli-invocations.allowlist"
SYNC_TARGETS_PATH = "scripts/sync-targets.yml"

# Files that MUST be present in the scan set. Pins the gate's scope so an
# attacker can't escape by removing agent-loop.sh from sync-targets.yml or
# moving it outside SCOPE_DIRS while keeping it synced from a fresh entry.
REQUIRED_FILES = [
    ".claude/skills/agent-loop/scripts/agent-loop.sh",
]

START_MARKER = "# claude-cli-invocations:start"
END_MARKER = "# claude-cli-invocations:end"

# Sensitive tokens that must only appear inside a marker pair. The binary-call
# pattern catches `claude --flag`, `claude -p`, `claude "$arg"`, `claude $arg`,
# `claude < file`, `claude <<< str`, and path-qualified forms like
# `/usr/local/bin/claude --print`. The lookbehind allows `/` so path-prefixed
# invocations are detected, but blocks `\w` and `.-` so `.claude/skills/...`
# path mentions and `claude.err` filename mentions don't false-flag. The
# `--permission-mode` and `bypassPermissions` literals catch the dangerous
# escalation signal directly even when the binary name has been variable-
# aliased (`CMD=claude; $CMD --permission-mode bypassPermissions ...`).
#
# Out-of-model (explicitly NOT caught — relies on CODEOWNERS + reviewer
# attention as the structural defense):
#  - Bare-word positional invocations: `claude help`, `claude prompt.txt`,
#    `claude foo` (where `foo` doesn't start with `-`, `"`, `'`, `$`, or `<`).
#    Detecting these reliably without false-flagging benign `claude exited`
#    -style echo strings would require a quote-state tracker.
#  - Bash quote-concat of the flag literals: `--per""mission-mode`,
#    `bypa""ssPermissions`. Same false-flag-vs-coverage trade-off.
SENSITIVE_TOKEN_RE = re.compile(
    r"(?<![\w.-])claude\s+(?:--?|[\"'$<])"
    r"|--permission-mode"
    r"|bypassPermissions"
)

COMMENT_LINE_RE = re.compile(r"^\s*#")


@dataclass(frozen=True)
class Region:
    path: str
    start_line: int
    end_line: int
    content: str

    def __post_init__(self) -> None:
        if self.start_line < 1 or self.end_line <= self.start_line:
            raise ValueError(
                f"invalid region {self.path}: "
                f"start_line={self.start_line}, end_line={self.end_line}"
            )


@dataclass(frozen=True)
class AllowlistEntry:
    sha256: str
    path: str  # bound to a specific source file — defends against region-copy attacks
    description: str

    def __post_init__(self) -> None:
        # Defense in depth: parse_allowlist gates entries at construction-from-text,
        # but a direct caller bypassing the parser could otherwise ship an invalid
        # entry that silently never matches. Cheap to enforce here.
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError(
                f"sha256 must be 64 lowercase hex chars, got {self.sha256!r}"
            )
        if not self.path:
            raise ValueError("path must be non-empty")


def extract_regions(path: str, text: str) -> tuple[list[Region], list[str]]:
    """Find marker-bounded regions in a file. Returns (regions, errors).

    Uses `keepends=True` because the body slice between markers is hashed
    verbatim — line terminators are content. `find_sensitive_token_lines`
    intentionally uses `keepends=False` (it only inspects line content, not
    bytes), so the asymmetry between the two callers is by design.
    """
    regions: list[Region] = []
    errors: list[str] = []
    lines = text.splitlines(keepends=True)
    start_line: int | None = None
    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped == START_MARKER:
            if start_line is not None:
                errors.append(
                    f"{path}:{idx}: nested {START_MARKER!r} "
                    f"(previous start at line {start_line})"
                )
                continue
            start_line = idx
        elif stripped == END_MARKER:
            if start_line is None:
                errors.append(f"{path}:{idx}: {END_MARKER!r} without matching start")
                continue
            content = "".join(lines[start_line:idx - 1])
            regions.append(
                Region(path=path, start_line=start_line, end_line=idx, content=content)
            )
            start_line = None
    if start_line is not None:
        errors.append(
            f"{path}:{start_line}: {START_MARKER!r} without matching end"
        )
    return regions, errors


def _join_continuations(text: str) -> list[tuple[int, str]]:
    """Return (first-lineno, logical-line) pairs with `\\`-continuations joined.

    Bash splits a command across lines with a trailing backslash. A line-based
    scan would see `claude \\` (no token-followed-by-flag) on line 1 and
    `"$PROMPT"` (no `claude`) on line 2, missing the joined invocation. The
    returned lineno is the FIRST physical line of the joined command, so
    findings still point at the `claude` token's actual location in the file.

    Trailing-backslash detection uses `endswith("\\\\")` after stripping
    trailing whitespace — handles `claude \\` followed by a trailing space
    that bash would otherwise reject. Escaped-trailing-backslash (`\\\\\\\\`)
    is treated as a literal backslash, not a continuation.
    """
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_start: int | None = None
    for idx, raw in enumerate(text.splitlines(), start=1):
        stripped_right = raw.rstrip()
        is_continuation = (
            stripped_right.endswith("\\") and not stripped_right.endswith("\\\\")
        )
        if buffer_start is None:
            buffer_start = idx
        if is_continuation:
            buffer.append(stripped_right[:-1])
            continue
        buffer.append(raw)
        out.append((buffer_start, " ".join(s.strip() for s in buffer)))
        buffer = []
        buffer_start = None
    if buffer and buffer_start is not None:
        out.append((buffer_start, " ".join(s.strip() for s in buffer)))
    return out


def find_sensitive_token_lines(path: str, text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for every non-comment line containing a sensitive token.

    Comment-only lines are skipped — they're documentation, not execution.
    Backslash-continuation lines are joined before scanning, so a split
    invocation like `claude \\` + `"$PROMPT"` is caught as one logical
    command (the lineno reported is the first physical line of the join).
    Quoted-string mentions remain in scope: if a synced shell script ever
    needs to refer to `claude --print` in an `echo`, the right move is a
    comment line or wrapping in markers, not a quote-state machine we'd
    have to keep correct against bash's full grammar.
    """
    found: list[tuple[int, str]] = []
    for lineno, line in _join_continuations(text):
        if COMMENT_LINE_RE.match(line):
            continue
        if SENSITIVE_TOKEN_RE.search(line):
            found.append((lineno, line))
    return found


def parse_allowlist(text: str) -> tuple[list[AllowlistEntry], list[str]]:
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            errors.append(
                f"{ALLOWLIST_PATH}:{lineno}: expected `<sha256>  <path>  <description>`, got: {line!r}"
            )
            continue
        sha, path = parts[0], parts[1]
        description = parts[2] if len(parts) == 3 else ""
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            errors.append(f"{ALLOWLIST_PATH}:{lineno}: not a sha256: {sha!r}")
            continue
        key = (sha, path)
        if key in seen:
            errors.append(
                f"{ALLOWLIST_PATH}:{lineno}: duplicate (hash, path) {sha[:12]}…/{path}"
            )
            continue
        seen.add(key)
        entries.append(AllowlistEntry(sha256=sha, path=path, description=description))
    return entries, errors


def hash_region(region: Region) -> str:
    return hashlib.sha256(region.content.encode("utf-8")).hexdigest()


def _git_tracked_files() -> list[str]:
    cmd = ["git", "ls-files", "--", *SCOPE_DIRS]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [p for p in result.stdout.splitlines() if p.endswith(SCOPE_SUFFIX)]


def _synced_shell_sources() -> tuple[list[str], list[str]]:
    """Read sync-targets.yml and return all .sh `source` paths + any errors.

    The lint's primary mission is "no malicious bytes propagated to consumer
    repos." sync-targets.yml is the authoritative list of files that get
    synced; deriving scope from it future-proofs the gate against a synced
    .sh file landing outside `.claude/skills/` (e.g. a future `.claude/hooks/`
    or any other manifest target).
    """
    errors: list[str] = []
    try:
        import yaml
    except ImportError:
        errors.append(
            f"pyyaml not installed — required to derive scope from {SYNC_TARGETS_PATH}."
        )
        return [], errors
    try:
        with open(SYNC_TARGETS_PATH, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except FileNotFoundError:
        errors.append(f"{SYNC_TARGETS_PATH} not found — cannot derive sync scope")
        return [], errors
    except yaml.YAMLError as exc:
        errors.append(f"{SYNC_TARGETS_PATH}: {exc}")
        return [], errors
    # yaml.safe_load("") returns None; a top-level list or scalar isn't a
    # mapping either. Guard before .get(...) so the lint surfaces the
    # malformed-manifest case instead of crashing with AttributeError.
    if not isinstance(doc, dict):
        errors.append(
            f"{SYNC_TARGETS_PATH}: expected top-level mapping, got {type(doc).__name__}"
        )
        return [], errors
    sources: list[str] = []
    for target in doc.get("targets", []) or []:
        if not isinstance(target, dict):
            continue
        source = target.get("source", "")
        if isinstance(source, str) and source.endswith(SCOPE_SUFFIX):
            # Normalize so `./foo`, `foo/`, `foo//bar`, etc., all map to the
            # same canonical form as `git ls-files`'s output — keeps the
            # union-with-tracked-files dedup honest.
            sources.append(os.path.normpath(source))
    return sources, errors


def _read_file_preserving_newlines(path: str) -> str:
    """Read a file without Python's universal-newline translation.

    The default text mode translates CRLF→LF on read, which would let an
    attacker flip line endings without rotating the region hash — silently
    defeating the byte-level invariant the allowlist is documented to enforce.
    `newline=""` disables the translation; line endings are returned as-is.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def scan(paths: list[str], allowlist_entries: list[AllowlistEntry]) -> int:
    """Return number of findings (0 = clean)."""
    # (hash, path) pairs are the unit of approval — an allowlisted region in
    # `foo.sh` does NOT auto-approve the same bytes appearing in `bar.sh`.
    # Defends against the region-copy attack (clone allowlisted block into a
    # new synced .sh file, evade the gate).
    allowed: set[tuple[str, str]] = {(e.sha256, e.path) for e in allowlist_entries}
    observed: set[tuple[str, str]] = set()
    findings = 0
    for path in paths:
        try:
            text = _read_file_preserving_newlines(path)
        except OSError as exc:
            # Security-lint posture: an attacker who can affect file
            # permissions might otherwise hide weaponized content from the scan.
            print(f"unreadable: {path}: {exc}", file=sys.stderr)
            findings += 1
            continue
        regions, region_errors = extract_regions(path, text)
        for err in region_errors:
            print(err)
            findings += 1
        # Per-file duplicate detection: one allowlist entry approves one
        # locked region. If a file contains two regions with the same hash,
        # an attacker has cloned the approved block within the same file
        # to double up `bypassPermissions` invocations without rotating the
        # allowlist. Flag every duplicate occurrence beyond the first.
        seen_hashes_this_file: dict[str, int] = {}
        for region in regions:
            digest = hash_region(region)
            seen_hashes_this_file[digest] = seen_hashes_this_file.get(digest, 0) + 1
            if seen_hashes_this_file[digest] > 1:
                print(
                    f"{path}:{region.start_line}: duplicate locked region "
                    f"(hash {digest[:12]}… already appears earlier in this file)."
                )
                print(
                    "    Each region must be unique within a file — the allowlist "
                    "approves one occurrence, not arbitrarily many."
                )
                findings += 1
                continue  # don't double-count via the allowlist gate
            observed.add((digest, path))
            if (digest, path) not in allowed:
                print(
                    f"{path}:{region.start_line}: locked region hash not in allowlist for this path."
                )
                print(f"    expected entry: {digest}  {path}  <description of the change>")
                print(
                    f"    add the entry to {ALLOWLIST_PATH} in this PR — the diff is the audit trail."
                )
                findings += 1
        for inv_line, inv_text in find_sensitive_token_lines(path, text):
            if not any(r.start_line < inv_line < r.end_line for r in regions):
                print(
                    f"{path}:{inv_line}: sensitive token (`claude` / `--permission-mode` / "
                    f"`bypassPermissions`) outside any {START_MARKER!r}/{END_MARKER!r} pair."
                )
                print(f"    > {inv_text.rstrip()}")
                print(
                    "    wrap in marker comments and add the region hash to "
                    f"{ALLOWLIST_PATH}, or remove the token."
                )
                findings += 1
    # Unused allowlist entries: prevents pre-seeding an opaque (hash, path)
    # in one PR and adding the matching region in a follow-up PR without
    # touching the allowlist (which would defeat the audit-trail invariant).
    for sha, entry_path in sorted(allowed - observed):
        print(
            f"{ALLOWLIST_PATH}: unused allowlist entry {sha[:12]}… for {entry_path}"
        )
        print(
            "    no locked region in scope hashes to this entry. "
            "Remove the entry or add the matching region in this PR."
        )
        findings += 1
    return findings


def compute_hashes(paths: list[str]) -> int:
    """Print each region's hash for the given paths. Helper for allowlist rotation.

    Surfaces region-parse errors to stderr and returns nonzero if any are
    found, so a maintainer rotating the allowlist sees a malformed locked
    region (orphan/nested/unclosed markers) before pasting hashes into the
    allowlist and shipping a malformed gate.
    """
    any_regions = False
    any_errors = False
    for path in paths:
        try:
            text = _read_file_preserving_newlines(path)
        except OSError as exc:
            print(f"unreadable: {path}: {exc}", file=sys.stderr)
            return 2
        regions, region_errors = extract_regions(path, text)
        for err in region_errors:
            any_errors = True
            print(err, file=sys.stderr)
        for region in regions:
            any_regions = True
            print(f"{hash_region(region)}  {path}  <description of the change>")
    if any_errors:
        return 2
    if not any_regions:
        print("(no locked regions found in the given paths)", file=sys.stderr)
        return 1
    return 0


# ---------- self test ----------


_FIXTURE_OK = """\
#!/bin/bash
echo hello
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
echo done
"""

_FIXTURE_BARE_INVOCATION = """\
#!/bin/bash
claude --print "leak"
"""

_FIXTURE_UNCLOSED_REGION = """\
#!/bin/bash
# claude-cli-invocations:start
claude --print "$PROMPT"
"""

_FIXTURE_ORPHAN_END = """\
#!/bin/bash
# claude-cli-invocations:end
"""

_FIXTURE_NESTED = """\
#!/bin/bash
# claude-cli-invocations:start
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
"""

_FIXTURE_COMMENT_REFERENCE = """\
#!/bin/bash
# claude --print is invoked below
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
"""

_FIXTURE_WHITESPACE_SENSITIVE_A = """\
# claude-cli-invocations:start
claude --print "x"
# claude-cli-invocations:end
"""
_FIXTURE_WHITESPACE_SENSITIVE_B = """\
# claude-cli-invocations:start
claude --print  "x"
# claude-cli-invocations:end
"""

# Markers stripped but the call remains — the bare-invocation gate must
# catch this. Defends against an attacker removing the markers (and the
# allowlist entry going stale, which on its own would pass the hash check
# because there's no region to hash).
_FIXTURE_MARKER_DELETED = """\
#!/bin/bash
claude --print "$PROMPT"
"""

# CRLF line endings on marker lines — `.strip()` removes the trailing
# `\\r`, so the marker still matches. The body bytes (between markers)
# carry their own CRLF, which a hash check correctly treats as different
# content from the LF version.
_FIXTURE_CRLF_MARKERS = (
    "# claude-cli-invocations:start\r\n"
    "claude --print \"x\"\r\n"
    "# claude-cli-invocations:end\r\n"
)

# Mixed scope: one marked region followed by a bare invocation later. The
# scan must flag exactly the bare invocation, not the in-region one.
_FIXTURE_MIXED_REGION_AND_BARE = """\
#!/bin/bash
# claude-cli-invocations:start
claude --print "in"
# claude-cli-invocations:end
echo middle
claude --print "out"
"""

# Two regions in one file. Allowlisting only one must surface a finding on
# the other — this exercises the per-region hash loop.
_FIXTURE_TWO_REGIONS = """\
#!/bin/bash
# claude-cli-invocations:start
claude --print "first"
# claude-cli-invocations:end
echo middle
# claude-cli-invocations:start
claude --print "second"
# claude-cli-invocations:end
"""

# bypassPermissions flag outside a marker — the sensitive-token gate
# must flag this even though `claude` itself isn't on the line.
_FIXTURE_BYPASS_FLAG_OUTSIDE = """\
#!/bin/bash
CMD=claude
$CMD --permission-mode bypassPermissions --print "$PROMPT"
"""

# Widened-regex coverage — exercises every alternative trailing-context
# branch that was added to `SENSITIVE_TOKEN_RE`. Each line MUST match so a
# future regex narrowing can't silently re-open one of these bypass forms.
_FIXTURE_SHORT_OPTION = """\
#!/bin/bash
claude -p "$EVIL"
"""
_FIXTURE_STDIN_REDIRECT = """\
#!/bin/bash
claude < prompt.txt
"""
_FIXTURE_HERE_STRING = """\
#!/bin/bash
claude <<< "$EVIL"
"""
_FIXTURE_POSITIONAL_VAR = """\
#!/bin/bash
claude "$EVIL"
"""
_FIXTURE_POSITIONAL_BARE_VAR = """\
#!/bin/bash
claude $EVIL
"""
_FIXTURE_PATH_QUALIFIED = """\
#!/bin/bash
/usr/local/bin/claude --print "$EVIL"
"""

# Negative fixtures — these MUST NOT match (false-positive avoidance for
# the existing in-script references the widening had to step around).
_FIXTURE_PATH_MENTION_IN_PRESENCE_CHECK = """\
#!/bin/bash
for cmd in gh jq python3 claude; do echo "$cmd"; done
"""
_FIXTURE_DOTPATH = """\
#!/bin/bash
echo "$PROJECT/.claude/skills/x"
"""
_FIXTURE_FILENAME_MENTION = """\
#!/bin/bash
CLAUDE_ERR="/tmp/claude.err"
"""
_FIXTURE_URL_MENTION = """\
#!/bin/bash
echo "See https://claude.com/docs for help."
"""

# Continuation-line evasion — `claude \\` on one line, `"$PROMPT"` on the
# next. The line-based scan would miss this; `_join_continuations` joins
# them before regex matching so the invocation surfaces.
_FIXTURE_CONTINUATION_INVOCATION = """\
#!/bin/bash
claude \\
    "$PROMPT"
"""

# Duplicate region within the same file — the allowlist approves one
# region, not arbitrarily many. The per-file duplicate detector must flag
# the second occurrence.
_FIXTURE_DUPLICATE_REGION = """\
#!/bin/bash
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
echo middle
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
"""


def run_self_test() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if not cond:
            failures.append(f"{label}: {detail}" if detail else label)

    # --- region extraction ---
    regions, errors = extract_regions("x.sh", _FIXTURE_OK)
    check("OK fixture", len(regions) == 1 and not errors, f"got {len(regions)} {errors!r}")
    regions, errors = extract_regions("x.sh", _FIXTURE_UNCLOSED_REGION)
    check(
        "UNCLOSED",
        not regions and len(errors) == 1 and "without matching end" in errors[0],
        f"got {regions!r} {errors!r}",
    )
    regions, errors = extract_regions("x.sh", _FIXTURE_ORPHAN_END)
    check(
        "ORPHAN END",
        not regions and len(errors) == 1 and "without matching start" in errors[0],
        f"got {regions!r} {errors!r}",
    )
    regions, errors = extract_regions("x.sh", _FIXTURE_NESTED)
    check("NESTED", bool(errors) and "nested" in errors[0], f"got {errors!r}")

    # --- hash sensitivity ---
    regions_a, _ = extract_regions("a.sh", _FIXTURE_WHITESPACE_SENSITIVE_A)
    regions_b, _ = extract_regions("b.sh", _FIXTURE_WHITESPACE_SENSITIVE_B)
    check(
        "WHITESPACE regions",
        len(regions_a) == 1 and len(regions_b) == 1,
        f"got {regions_a!r} / {regions_b!r}",
    )
    if regions_a and regions_b:
        check(
            "WHITESPACE hash sensitivity",
            hash_region(regions_a[0]) != hash_region(regions_b[0]),
            "internal whitespace change should rotate the hash",
        )

    # CRLF on the marker line itself still matches; the body content
    # differs from the LF variant by its line endings → different hash.
    regions_lf, _ = extract_regions("lf.sh", _FIXTURE_WHITESPACE_SENSITIVE_A)
    regions_crlf, _ = extract_regions("crlf.sh", _FIXTURE_CRLF_MARKERS)
    check(
        "CRLF markers parse",
        len(regions_crlf) == 1,
        f"got {regions_crlf!r}",
    )
    if regions_lf and regions_crlf:
        check(
            "CRLF hash differs from LF",
            hash_region(regions_lf[0]) != hash_region(regions_crlf[0]),
            "line-ending flip should rotate the hash",
        )

    # --- sensitive-token discovery ---
    inv = find_sensitive_token_lines("x.sh", _FIXTURE_BARE_INVOCATION)
    check("BARE", len(inv) == 1, f"got {inv!r}")
    inv = find_sensitive_token_lines("x.sh", _FIXTURE_COMMENT_REFERENCE)
    check("COMMENT REFERENCE", len(inv) == 1, f"got {inv!r}")
    # The dangerous flag literals are caught even when the binary name has
    # been variable-aliased and the line itself doesn't contain `claude`.
    # Line 2 of the fixture (`CMD=claude`) is deliberately NOT expected to
    # match — `claude` at end-of-line has no following `\s+` so the binary
    # pattern fails, and there's no flag literal on that line. If the regex
    # is later widened to detect variable assignments, this expected count
    # would change and the test should be updated deliberately.
    inv = find_sensitive_token_lines("x.sh", _FIXTURE_BYPASS_FLAG_OUTSIDE)
    check("BYPASS TOKENS", len(inv) == 1, f"got {inv!r}")

    # Every widened-regex case must produce exactly one match — locks in
    # the iter-2 widening so a future narrowing reopens a documented bypass.
    for label, fixture in [
        ("SHORT OPTION (claude -p)", _FIXTURE_SHORT_OPTION),
        ("STDIN REDIRECT (claude <)", _FIXTURE_STDIN_REDIRECT),
        ("HERE STRING (claude <<<)", _FIXTURE_HERE_STRING),
        ("POSITIONAL \"$VAR\"", _FIXTURE_POSITIONAL_VAR),
        ("POSITIONAL $VAR", _FIXTURE_POSITIONAL_BARE_VAR),
        ("PATH-QUALIFIED", _FIXTURE_PATH_QUALIFIED),
    ]:
        inv = find_sensitive_token_lines("x.sh", fixture)
        check(f"REGEX/{label}", len(inv) == 1, f"got {inv!r}")
    # Negative cases — these must NOT match (otherwise the lint false-flags
    # benign references that exist in the current agent-loop.sh).
    for label, fixture in [
        ("PRESENCE-CHECK loop", _FIXTURE_PATH_MENTION_IN_PRESENCE_CHECK),
        (".claude/ path mention", _FIXTURE_DOTPATH),
        ("claude.err filename", _FIXTURE_FILENAME_MENTION),
        ("claude.com URL", _FIXTURE_URL_MENTION),
    ]:
        inv = find_sensitive_token_lines("x.sh", fixture)
        check(f"REGEX-NEG/{label}", inv == [], f"got {inv!r}")

    # Continuation-line join: `claude \` + `"$PROMPT"` must surface as one
    # finding, lineno pointing at the line where `claude` lives.
    inv = find_sensitive_token_lines("x.sh", _FIXTURE_CONTINUATION_INVOCATION)
    check(
        "CONTINUATION joined into one logical line",
        len(inv) == 1 and inv[0][0] == 2,
        f"got {inv!r}",
    )

    # Direct `_join_continuations` unit tests — lock the four documented
    # behaviors so a regex regression doesn't slip past `find_sensitive_token_lines`
    # by happening to still produce a match.
    join_cases = [
        # (label, input, expected output)
        ("single-line passthrough", "a\nb\n", [(1, "a"), (2, "b")]),
        ("normal join", "a \\\nb\n", [(1, "a b")]),
        ("triple join", "a \\\nb \\\nc\n", [(1, "a b c")]),
        # Escaped trailing backslash (`\\` at EOL) is NOT a continuation —
        # it's a literal pair. The line stands alone.
        ("escaped backslash not a continuation", "a \\\\\nb\n", [(1, "a \\\\"), (2, "b")]),
        # Unclosed trailing continuation at EOF — emit what we have.
        ("unclosed trailing continuation", "a \\\n", [(1, "a")]),
        # Empty input.
        ("empty input", "", []),
        # Continuation with trailing whitespace after the backslash —
        # `rstrip()` normalizes so the `\\` at the visual EOL still wins.
        ("continuation with trailing space", "a \\  \nb\n", [(1, "a b")]),
    ]
    for label, text_in, expected in join_cases:
        actual = _join_continuations(text_in)
        check(f"_JOIN/{label}", actual == expected, f"got {actual!r}")

    # --- end-to-end scan integration ---
    import contextlib
    import io
    import os
    import tempfile

    def scan_into(paths: list[str], entries: list[AllowlistEntry]) -> tuple[int, str]:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            n = scan(paths, entries)
        return n, sink.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        # bare invocation → at least one finding, message names the file:line.
        bare_path = os.path.join(tmp, "bare.sh")
        with open(bare_path, "w") as fh:
            fh.write(_FIXTURE_BARE_INVOCATION)
        n, out = scan_into([bare_path], [])
        check("SCAN(bare) findings", n >= 1, f"got {n}")
        check("SCAN(bare) message", f"{bare_path}:2:" in out, f"output:\n{out}")

        # OK + allowlisted (correct path) → 0 findings.
        ok_path = os.path.join(tmp, "ok.sh")
        with open(ok_path, "w") as fh:
            fh.write(_FIXTURE_OK)
        regions, _ = extract_regions(ok_path, _FIXTURE_OK)
        entry = AllowlistEntry(
            sha256=hash_region(regions[0]), path=ok_path, description="test"
        )
        n, out = scan_into([ok_path], [entry])
        check("SCAN(ok+allowlist)", n == 0, f"got {n}; output:\n{out}")

        # Path-binding gate: the same hash allowlisted for a DIFFERENT path
        # must NOT auto-approve the region in this file. Defends against the
        # region-copy attack — clone allowlisted bytes into a new synced .sh
        # file and the gate still fires.
        other_path = os.path.join(tmp, "ok-clone.sh")
        with open(other_path, "w") as fh:
            fh.write(_FIXTURE_OK)
        n, out = scan_into([other_path], [entry])
        check(
            "SCAN(region-copy) blocked by path binding",
            n >= 1 and "not in allowlist for this path" in out,
            f"got {n}; output:\n{out}",
        )

        # Markers deleted but allowlist entry stale → bare-token finding
        # surfaces. The stale entry is harmless on its own (no region to
        # hash); the call is what trips the gate.
        del_path = os.path.join(tmp, "del.sh")
        with open(del_path, "w") as fh:
            fh.write(_FIXTURE_MARKER_DELETED)
        n, out = scan_into([del_path], [entry])
        check(
            "SCAN(marker-deleted) catches bare call",
            n >= 1 and "sensitive token" in out,
            f"got {n}; output:\n{out}",
        )

        # Mixed: one in-region call (allowlisted) + one bare call → exactly
        # one finding, pointing at the bare call's line (line 6).
        mixed_path = os.path.join(tmp, "mixed.sh")
        with open(mixed_path, "w") as fh:
            fh.write(_FIXTURE_MIXED_REGION_AND_BARE)
        mixed_regions, _ = extract_regions(mixed_path, _FIXTURE_MIXED_REGION_AND_BARE)
        mixed_entry = AllowlistEntry(
            sha256=hash_region(mixed_regions[0]), path=mixed_path, description="test"
        )
        n, out = scan_into([mixed_path], [mixed_entry])
        check(
            "SCAN(mixed) flags only bare call",
            n == 1 and f"{mixed_path}:6:" in out,
            f"got {n}; output:\n{out}",
        )

        # Two regions, allowlist only the first → finding on the second.
        two_path = os.path.join(tmp, "two.sh")
        with open(two_path, "w") as fh:
            fh.write(_FIXTURE_TWO_REGIONS)
        two_regions, _ = extract_regions(two_path, _FIXTURE_TWO_REGIONS)
        first_entry = AllowlistEntry(
            sha256=hash_region(two_regions[0]), path=two_path, description="first"
        )
        n, out = scan_into([two_path], [first_entry])
        check(
            "SCAN(two-regions) flags unallowlisted second region",
            n == 1 and "not in allowlist for this path" in out,
            f"got {n}; output:\n{out}",
        )

        # bypassPermissions flag outside any marker pair → finding.
        bypass_path = os.path.join(tmp, "bypass.sh")
        with open(bypass_path, "w") as fh:
            fh.write(_FIXTURE_BYPASS_FLAG_OUTSIDE)
        n, out = scan_into([bypass_path], [])
        check(
            "SCAN(bypass-flag) catches token outside marker",
            n >= 1 and "sensitive token" in out,
            f"got {n}; output:\n{out}",
        )

        # Unused allowlist entry → finding. Closes the iter-2 attack of
        # pre-seeding an opaque (hash, path) in one PR and adding the
        # matching region in a follow-up PR without re-touching the
        # allowlist (which would defeat the audit-trail invariant).
        empty_path = os.path.join(tmp, "no-regions.sh")
        with open(empty_path, "w") as fh:
            fh.write("#!/bin/bash\necho hello\n")
        orphan_entry = AllowlistEntry(
            sha256="a" * 64, path="some/synced.sh", description="pre-seeded"
        )
        n, out = scan_into([empty_path], [orphan_entry])
        check(
            "SCAN(unused-entry) flagged",
            n >= 1 and "unused allowlist entry" in out,
            f"got {n}; output:\n{out}",
        )

        # compute_hashes surfaces region-parse errors and returns nonzero.
        bad_path = os.path.join(tmp, "malformed.sh")
        with open(bad_path, "w") as fh:
            fh.write(_FIXTURE_UNCLOSED_REGION)
        sink_err = io.StringIO()
        sink_out = io.StringIO()
        with contextlib.redirect_stdout(sink_out), contextlib.redirect_stderr(sink_err):
            ch_rc = compute_hashes([bad_path])
        check(
            "COMPUTE-HASH surfaces region errors",
            ch_rc == 2 and "without matching end" in sink_err.getvalue(),
            f"rc={ch_rc} stderr={sink_err.getvalue()!r}",
        )

        # Duplicate region within the same file → finding on the second
        # occurrence. The allowlist approves ONE region, not two with the
        # same bytes — a clone introduces another bypassPermissions call.
        dup_path = os.path.join(tmp, "dup-region.sh")
        with open(dup_path, "w") as fh:
            fh.write(_FIXTURE_DUPLICATE_REGION)
        dup_regions, _ = extract_regions(dup_path, _FIXTURE_DUPLICATE_REGION)
        dup_entry = AllowlistEntry(
            sha256=hash_region(dup_regions[0]), path=dup_path, description="test"
        )
        n, out = scan_into([dup_path], [dup_entry])
        # First region matches allowlist (no finding); second region trips
        # the duplicate detector exactly once. Tight `n == 1` catches a
        # regression where the second region ALSO hits the hash-mismatch
        # gate (which the `continue` after the duplicate emit is meant to
        # prevent).
        check(
            "SCAN(duplicate-region) flagged within same file",
            n == 1 and "duplicate locked region" in out,
            f"got {n}; output:\n{out}",
        )

        # CRLF on disk: write CRLF bytes through binary mode so Python's
        # text-mode universal-newline translation can't normalize them
        # before the lint sees them. The lint MUST read with newline=""
        # and treat the bytes as written, so the LF and CRLF versions of
        # the same logical region hash differently.
        lf_disk_path = os.path.join(tmp, "lf.sh")
        crlf_disk_path = os.path.join(tmp, "crlf.sh")
        with open(lf_disk_path, "wb") as fb:
            fb.write(_FIXTURE_WHITESPACE_SENSITIVE_A.encode("utf-8"))
        with open(crlf_disk_path, "wb") as fb:
            fb.write(_FIXTURE_CRLF_MARKERS.encode("utf-8"))
        lf_text = _read_file_preserving_newlines(lf_disk_path)
        crlf_text = _read_file_preserving_newlines(crlf_disk_path)
        lf_disk_regions, _ = extract_regions(lf_disk_path, lf_text)
        crlf_disk_regions, _ = extract_regions(crlf_disk_path, crlf_text)
        check(
            "ON-DISK CRLF regions parse",
            len(lf_disk_regions) == 1 and len(crlf_disk_regions) == 1,
            f"lf={lf_disk_regions!r} crlf={crlf_disk_regions!r}",
        )
        if lf_disk_regions and crlf_disk_regions:
            check(
                "ON-DISK CRLF hash differs from LF after file-read",
                hash_region(lf_disk_regions[0])
                != hash_region(crlf_disk_regions[0]),
                "universal-newline translation must not normalize CRLF→LF",
            )

    # --- allowlist parser ---
    allowlist_fixture = (
        "# header comment\n"
        + ("0" * 64) + "  path/to/file.sh  description here\n"
        + "\n"
        + "abc-not-sha256  path/to/other.sh  bad\n"
        + ("1" * 64) + "  too-few-cols-was-here-but-actually-this-is-a-path\n"
    )
    entries, errors = parse_allowlist(allowlist_fixture)
    check("ALLOWLIST entry count", len(entries) == 2, f"got {entries!r}")
    check(
        "ALLOWLIST error message",
        bool(errors) and "not a sha256" in errors[0],
        f"got {errors!r}",
    )

    # Same hash, different path → both kept (path-binding is the dedup key).
    dual_fixture = (
        ("0" * 64) + "  a.sh  first\n"
        + ("0" * 64) + "  b.sh  second\n"
    )
    entries, errors = parse_allowlist(dual_fixture)
    check(
        "ALLOWLIST same-hash-different-path keeps both",
        len(entries) == 2 and not errors,
        f"entries={entries!r} errors={errors!r}",
    )

    # Same (hash, path) → duplicate, second rejected.
    dup_fixture = (
        ("0" * 64) + "  a.sh  first\n"
        + ("0" * 64) + "  a.sh  second\n"
    )
    entries, errors = parse_allowlist(dup_fixture)
    check(
        "ALLOWLIST duplicate (hash, path) detected",
        len(entries) == 1 and any("duplicate" in e for e in errors),
        f"entries={entries!r} errors={errors!r}",
    )

    # Missing path field → format error.
    short_fixture = ("0" * 64) + "\n"
    entries, errors = parse_allowlist(short_fixture)
    check(
        "ALLOWLIST missing path errors",
        not entries and any("expected" in e for e in errors),
        f"entries={entries!r} errors={errors!r}",
    )

    # --- _synced_shell_sources via temporary manifest files ---
    # Cover the manifest parser end-to-end: a synced .sh outside SCOPE_DIRS
    # surfaces, non-.sh sources are filtered out, malformed YAML and
    # missing manifests produce clear errors. Runs in a tempdir-as-cwd so
    # the test never touches the real `scripts/sync-targets.yml`.
    test_cases: list[tuple[str, str, list[str], bool]] = [
        # (label, manifest_yaml, expected_sources, expect_errors)
        (
            "synced .sh outside SCOPE_DIRS surfaces",
            "targets:\n"
            "  - source: .claude/hooks/foo.sh\n"
            "    destination: .claude/hooks/foo.sh\n"
            "  - source: .claude/skills/x/SKILL.md\n"
            "    destination: .claude/skills/x/SKILL.md\n",
            [".claude/hooks/foo.sh"],
            False,
        ),
        (
            "non-.sh sources filtered out",
            "targets:\n"
            "  - source: .claude/agents/code-reviewer.md\n"
            "    destination: .claude/agents/code-reviewer.md\n",
            [],
            False,
        ),
        (
            "empty manifest is an error, not a crash",
            "",
            [],
            True,
        ),
        (
            "non-mapping top-level YAML is an error, not a crash",
            "- not-a-mapping\n",
            [],
            True,
        ),
        (
            "malformed YAML reports parse error",
            "targets:\n  - this is: bad\n      yaml: '\n",
            [],
            True,
        ),
        (
            "path normalization: `./foo/bar.sh` matches `foo/bar.sh`",
            "targets:\n  - source: ./.claude/skills/x.sh\n    destination: ./.claude/skills/x.sh\n",
            [".claude/skills/x.sh"],
            False,
        ),
    ]
    for label, yaml_text, expected_sources, expect_errors in test_cases:
        with tempfile.TemporaryDirectory() as tmp:
            cwd_was = os.getcwd()
            try:
                os.chdir(tmp)
                manifest_dir = os.path.join(tmp, "scripts")
                os.makedirs(manifest_dir, exist_ok=True)
                with open(os.path.join(manifest_dir, "sync-targets.yml"), "w") as fh:
                    fh.write(yaml_text)
                sources, errors = _synced_shell_sources()
            finally:
                os.chdir(cwd_was)
        if expect_errors:
            check(
                f"SYNCED/{label} reports errors",
                bool(errors),
                f"got sources={sources!r} errors={errors!r}",
            )
        else:
            check(
                f"SYNCED/{label}",
                sources == expected_sources and not errors,
                f"got sources={sources!r} errors={errors!r}",
            )
    # Missing manifest → error.
    with tempfile.TemporaryDirectory() as tmp:
        cwd_was = os.getcwd()
        try:
            os.chdir(tmp)
            sources, errors = _synced_shell_sources()
        finally:
            os.chdir(cwd_was)
        check(
            "SYNCED/missing manifest reports error",
            sources == [] and any("not found" in e for e in errors),
            f"got sources={sources!r} errors={errors!r}",
        )

    # --- dataclass invariant enforcement ---
    try:
        AllowlistEntry(sha256="not-a-hash", path="x", description="x")
        failures.append("AllowlistEntry: expected ValueError on invalid sha256")
    except ValueError:
        pass
    try:
        AllowlistEntry(sha256="0" * 64, path="", description="x")
        failures.append("AllowlistEntry: expected ValueError on empty path")
    except ValueError:
        pass
    try:
        Region(path="x", start_line=10, end_line=5, content="")
        failures.append("Region: expected ValueError on inverted line bounds")
    except ValueError:
        pass

    if failures:
        print("Self-test failures:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("Self-test ok: all assertions passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in fixtures (no git access).",
    )
    parser.add_argument(
        "--compute-hash",
        metavar="PATH",
        nargs="+",
        help=(
            "Print each marker-bounded region's hash for the given path(s). "
            "Use to generate or rotate allowlist entries when you edit a locked region."
        ),
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if args.compute_hash:
        return compute_hashes(args.compute_hash)

    try:
        tracked = _git_tracked_files()
    except subprocess.CalledProcessError as exc:
        print(f"git ls-files failed: {exc.stderr}", file=sys.stderr)
        return 2

    # Belt-and-braces scope: union of (a) tracked .sh files under SCOPE_DIRS
    # — covers upstream-only files that a developer might still execute —
    # and (b) all .sh `source` paths in sync-targets.yml — covers files
    # outside SCOPE_DIRS that get propagated to consumers. (b) is the
    # primary mission per the threat model; (a) catches the on-disk
    # variant. If sync-targets.yml is unreadable / unparseable, fail loud
    # rather than silently degrading to (a) only.
    synced, sync_errors = _synced_shell_sources()
    if sync_errors:
        for err in sync_errors:
            print(err, file=sys.stderr)
        return 2
    paths = sorted(set(tracked) | set(synced))

    # Scope-pin: REQUIRED_FILES must be in the scan set. Defends against
    # an attacker moving the locked script out of SCOPE_DIRS *and* removing
    # it from sync-targets.yml in the same PR (lint would otherwise scan
    # an empty file list and exit clean).
    missing = [f for f in REQUIRED_FILES if f not in paths]
    if missing:
        print(
            f"required scope files missing from scan: {missing}. "
            f"If a file was intentionally moved/renamed, update REQUIRED_FILES.",
            file=sys.stderr,
        )
        return 2

    try:
        with open(ALLOWLIST_PATH, encoding="utf-8") as fh:
            allowlist_text = fh.read()
    except FileNotFoundError:
        # Treating missing-allowlist as empty would be fail-open: an attacker
        # could delete the file alongside marker edits and the gate would not
        # detect the change. Require the file to exist unconditionally.
        print(
            f"allowlist file missing: {ALLOWLIST_PATH}. "
            "Create the file (commit at least the header) before running.",
            file=sys.stderr,
        )
        return 2
    allowlist_entries, allowlist_errors = parse_allowlist(allowlist_text)
    if allowlist_errors:
        for err in allowlist_errors:
            print(err, file=sys.stderr)
        return 2

    return 1 if scan(paths, allowlist_entries) else 0


if __name__ == "__main__":
    sys.exit(main())
