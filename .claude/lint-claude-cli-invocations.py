#!/usr/bin/env python3
r"""Lint `claude` CLI invocations inside synced shell scripts.

Every `claude` invocation (or use of `--permission-mode` / `bypassPermissions`)
in `.claude/skills/**/scripts/*.sh` must sit inside a
`# claude-cli-invocations:start` / `:end` marker pair, and the bytes between
those markers must hash to an entry in `.claude/claude-cli-invocations.allowlist`.
Any change to the locked region — flags, fallback literal, surrounding glue —
breaks the hash and fails CI, forcing the edit to be reviewer-visible.

Usage:
    python3 .claude/lint-claude-cli-invocations.py             # scan in-scope files
    python3 .claude/lint-claude-cli-invocations.py --self-test # run unit fixtures only

Exit codes: 0 clean, 1 findings, 2 usage/internal error.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass

SCOPE_DIRS = [".claude/skills"]
SCOPE_SUFFIX = ".sh"
ALLOWLIST_PATH = ".claude/claude-cli-invocations.allowlist"

# Files that MUST be present in the scan set. Pins the gate's scope so an
# attacker can't escape by moving agent-loop.sh outside SCOPE_DIRS while
# keeping it synced via a fresh manifest entry.
REQUIRED_FILES = [
    ".claude/skills/agent-loop/scripts/agent-loop.sh",
]

START_MARKER = "# claude-cli-invocations:start"
END_MARKER = "# claude-cli-invocations:end"

# Sensitive tokens that must only appear inside a marker pair. The binary-call
# pattern catches `claude --flag`, `claude "$arg"`, and `claude $arg` — both
# the flag form and the positional-arg form that an attacker might use to slip
# in a fresh invocation. The `(?<![\w/.-])` guard skips path mentions like
# `.claude/skills/...` and filename mentions like `claude.err`. The two flag
# literals catch the dangerous escalation signal directly, even when the
# `claude` token has been variable-aliased (`CMD=claude; $CMD --permission-mode
# bypassPermissions ...`). Trivial obfuscation of these flag literals via
# bash quote-concat is left to reviewer + CODEOWNERS defense.
SENSITIVE_TOKEN_RE = re.compile(
    r"(?<![\w/.-])claude\s+(?:--|[\"'$])"
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
    description: str

    def __post_init__(self) -> None:
        # Defense in depth: parse_allowlist gates entries at construction-from-text,
        # but a direct caller bypassing the parser could otherwise ship an invalid
        # entry that silently never matches. Cheap to enforce here.
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError(
                f"sha256 must be 64 lowercase hex chars, got {self.sha256!r}"
            )


def extract_regions(path: str, text: str) -> tuple[list[Region], list[str]]:
    """Find marker-bounded regions in a file. Returns (regions, errors)."""
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


def find_sensitive_token_lines(path: str, text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for every non-comment line containing a sensitive token.

    Comment-only lines (matching `^\\s*#`) are skipped — they're documentation.
    Everything else is in scope, including quoted-string mentions: if a synced
    shell script ever needs to refer to `claude --print` in an `echo`, the
    right move is a comment line or wrapping in markers, not a quote-state
    machine we'd have to keep correct against bash's full grammar.
    """
    found: list[tuple[int, str]] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        if COMMENT_LINE_RE.match(raw):
            continue
        if SENSITIVE_TOKEN_RE.search(raw):
            found.append((idx, raw))
    return found


def parse_allowlist(text: str) -> tuple[list[AllowlistEntry], list[str]]:
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        sha = parts[0]
        description = parts[1] if len(parts) == 2 else ""
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            errors.append(f"{ALLOWLIST_PATH}:{lineno}: not a sha256: {sha!r}")
            continue
        if sha in seen:
            errors.append(f"{ALLOWLIST_PATH}:{lineno}: duplicate hash {sha}")
            continue
        seen.add(sha)
        entries.append(AllowlistEntry(sha256=sha, description=description))
    return entries, errors


def hash_region(region: Region) -> str:
    return hashlib.sha256(region.content.encode("utf-8")).hexdigest()


def _git_tracked_files() -> list[str]:
    cmd = ["git", "ls-files", "--", *SCOPE_DIRS]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [p for p in result.stdout.splitlines() if p.endswith(SCOPE_SUFFIX)]


def scan(paths: list[str], allowlist_entries: list[AllowlistEntry]) -> int:
    """Return number of findings (0 = clean)."""
    allowed_hashes = {e.sha256 for e in allowlist_entries}
    findings = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
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
        for region in regions:
            digest = hash_region(region)
            if digest not in allowed_hashes:
                print(
                    f"{path}:{region.start_line}: locked region hash not in allowlist."
                )
                print(f"    expected entry: {digest}  <description of the change>")
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
    return findings


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
    inv = find_sensitive_token_lines("x.sh", _FIXTURE_BYPASS_FLAG_OUTSIDE)
    check("BYPASS TOKENS", len(inv) == 1, f"got {inv!r}")

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

        # OK + allowlisted → 0 findings.
        ok_path = os.path.join(tmp, "ok.sh")
        with open(ok_path, "w") as fh:
            fh.write(_FIXTURE_OK)
        regions, _ = extract_regions(ok_path, _FIXTURE_OK)
        entry = AllowlistEntry(sha256=hash_region(regions[0]), description="test")
        n, out = scan_into([ok_path], [entry])
        check("SCAN(ok+allowlist)", n == 0, f"got {n}; output:\n{out}")

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
            sha256=hash_region(mixed_regions[0]), description="test"
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
            sha256=hash_region(two_regions[0]), description="first"
        )
        n, out = scan_into([two_path], [first_entry])
        check(
            "SCAN(two-regions) flags unallowlisted second region",
            n == 1 and "hash not in allowlist" in out,
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

    # --- allowlist parser ---
    allowlist_fixture = (
        "# header comment\n"
        + ("0" * 64) + "  description here\n"
        + "\n"
        + "abc-not-sha256\n"
    )
    entries, errors = parse_allowlist(allowlist_fixture)
    check("ALLOWLIST entry count", len(entries) == 1, f"got {entries!r}")
    check(
        "ALLOWLIST error message",
        bool(errors) and "not a sha256" in errors[0],
        f"got {errors!r}",
    )

    # Duplicate hash → reported, only first kept.
    dup_fixture = ("0" * 64) + "  first\n" + ("0" * 64) + "  second\n"
    entries, errors = parse_allowlist(dup_fixture)
    check(
        "ALLOWLIST duplicate detected",
        len(entries) == 1 and any("duplicate" in e for e in errors),
        f"entries={entries!r} errors={errors!r}",
    )

    # --- dataclass invariant enforcement ---
    try:
        AllowlistEntry(sha256="not-a-hash", description="x")
        failures.append("AllowlistEntry: expected ValueError on invalid sha256")
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
    print(f"Self-test ok: {32 - len(failures)} assertions across extract/hash/scan/parse/invariants.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in fixtures (no git access).",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    try:
        paths = _git_tracked_files()
    except subprocess.CalledProcessError as exc:
        print(f"git ls-files failed: {exc.stderr}", file=sys.stderr)
        return 2

    # Scope-pin: REQUIRED_FILES must be in the scan set. Defends against
    # an attacker moving the locked script out of SCOPE_DIRS while keeping
    # it synced from a fresh manifest entry (lint would otherwise scan an
    # empty file list and exit clean).
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
