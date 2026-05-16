#!/usr/bin/env python3
r"""Lint `claude` CLI invocations inside synced shell scripts.

Synced scripts under `.claude/skills/**/scripts/*.sh` get propagated to every
consumer of this upstream. One of them — `agent-loop/scripts/agent-loop.sh` —
spawns `claude` with `--permission-mode bypassPermissions`. A subtly malicious
upstream change to the flags or to the prompt-fallback literal would
weaponize Claude in every consumer's agent-loop runs without tripping
shellcheck or any other syntax-level gate.

This lint pins the bytes of every `claude` invocation inside a synced shell
script via an explicit `# claude-cli-invocations:start` / `:end` marker pair
plus a hash allowlist at `.claude/claude-cli-invocations.allowlist`. Any
change to a locked region produces a hash mismatch that fails CI; the
authorized edit path is "update the locked region AND its allowlist entry in
the same PR," which forces the change to be reviewer-visible.

The lint additionally rejects any `claude --...` invocation that sits
*outside* a marker pair, so an attacker can't bypass the gate by adding a
fresh `claude` call elsewhere in the file.

Usage:
    python3 .claude/lint-claude-cli-invocations.py             # scan all in-scope files
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

# Synced shell scripts are the propagation surface. Add directories here as
# the sync surface grows; the lint scope is intentionally narrow because the
# threat model is upstream-controlled execution in consumer environments.
SCOPE_DIRS = [".claude/skills"]
SCOPE_SUFFIX = ".sh"
ALLOWLIST_PATH = ".claude/claude-cli-invocations.allowlist"

START_MARKER = "# claude-cli-invocations:start"
END_MARKER = "# claude-cli-invocations:end"

# Matches the `claude` CLI binary invocation followed by at least one flag.
# `(?<![\w/.-])` avoids matching tokens like `myclaude` or `/usr/local/claude`
# (the latter is still a claude binary, but our convention is to call it
# unqualified). The trailing `--` requirement narrows to flag invocations,
# which is what every real call in this repo looks like.
CLAUDE_INVOCATION_RE = re.compile(r"(?<![\w/.-])claude\s+--")

# Comment-only lines are skipped by the bare-invocation check — they're
# documentation, not execution. (Quoted-string mentions of `claude --` are
# not skipped; see find_claude_invocations.)
COMMENT_LINE_RE = re.compile(r"^\s*#")


@dataclass(frozen=True)
class Region:
    path: str
    start_line: int  # 1-indexed line of the start marker
    end_line: int  # 1-indexed line of the end marker
    content: str  # bytes between the markers (exclusive), verbatim


@dataclass(frozen=True)
class AllowlistEntry:
    sha256: str
    description: str


# ---------- region extraction ----------


def extract_regions(path: str, text: str) -> tuple[list[Region], list[str]]:
    """Find marker-bounded regions in a file. Returns (regions, errors).

    Errors are messages about malformed marker structure (unmatched start,
    unmatched end, nested start). Regions are emitted only for well-formed
    pairs — the caller decides whether to also fail on the errors.
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
            # Body is the slice between start and end markers (exclusive on
            # both ends). 1-indexed lineno → 0-indexed list slice: lines
            # `start_line+1..idx-1` map to `lines[start_line:idx-1]`.
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


# ---------- claude invocation discovery ----------


def find_claude_invocations(path: str, text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for every line that contains a `claude --` call.

    Comment-only lines are skipped (they're documentation, not execution).
    Quoted-string contexts (`echo "claude --print ..."`) are NOT skipped:
    a fully accurate bash quote tracker is subtle and the marker-region
    check is the source of truth anyway. If a synced shell script ever
    needs to mention `claude --` inside a quoted string, the right move
    is to move that mention to a comment line or wrap it in the markers.
    """
    found: list[tuple[int, str]] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        if COMMENT_LINE_RE.match(raw):
            continue
        if CLAUDE_INVOCATION_RE.search(raw):
            found.append((idx, raw))
    return found


# ---------- allowlist ----------


def parse_allowlist(text: str) -> tuple[list[AllowlistEntry], list[str]]:
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
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
        entries.append(AllowlistEntry(sha256=sha, description=description))
    return entries, errors


def hash_region(region: Region) -> str:
    return hashlib.sha256(region.content.encode("utf-8")).hexdigest()


# ---------- file scan ----------


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
            # A security lint can't tolerate "couldn't read the file" — an
            # attacker who can affect permissions might use that to hide
            # malicious content from scanning.
            print(f"unreadable: {path}: {exc}", file=sys.stderr)
            findings += 1
            continue
        regions, region_errors = extract_regions(path, text)
        for err in region_errors:
            print(err)
            findings += 1
        # Hash gate: every locked region must be in the allowlist.
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
        # Bare-invocation gate: every claude call must sit inside a region.
        invocations = find_claude_invocations(path, text)
        for inv_line, inv_text in invocations:
            if not any(r.start_line < inv_line < r.end_line for r in regions):
                print(
                    f"{path}:{inv_line}: `claude --` invocation outside any "
                    f"{START_MARKER!r}/{END_MARKER!r} pair."
                )
                print(f"    > {inv_text.rstrip()}")
                print(
                    "    wrap the invocation in marker comments and add its hash to "
                    f"{ALLOWLIST_PATH}, or remove the call."
                )
                findings += 1
    return findings


# ---------- self test ----------


# Minimal well-formed file: one marked region containing a claude invocation.
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

# Comment-only line referencing claude — also not an invocation.
_FIXTURE_COMMENT_REFERENCE = """\
#!/bin/bash
# claude --print is invoked below
# claude-cli-invocations:start
claude --print "$PROMPT"
# claude-cli-invocations:end
"""

# Whitespace at the END of the body — exercises that body bytes are
# captured verbatim including trailing whitespace.
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


def run_self_test() -> int:
    failures: list[str] = []

    # --- region extraction ---
    regions, errors = extract_regions("x.sh", _FIXTURE_OK)
    if len(regions) != 1 or errors:
        failures.append(f"OK fixture: expected 1 region 0 errors, got {len(regions)} {errors!r}")
    regions, errors = extract_regions("x.sh", _FIXTURE_UNCLOSED_REGION)
    if regions or len(errors) != 1 or "without matching end" not in errors[0]:
        failures.append(f"UNCLOSED: expected 1 error about missing end, got {regions!r} {errors!r}")
    regions, errors = extract_regions("x.sh", _FIXTURE_ORPHAN_END)
    if regions or len(errors) != 1 or "without matching start" not in errors[0]:
        failures.append(f"ORPHAN END: expected 1 error about missing start, got {regions!r} {errors!r}")
    regions, errors = extract_regions("x.sh", _FIXTURE_NESTED)
    if not errors or "nested" not in errors[0]:
        failures.append(f"NESTED: expected nested-start error, got {errors!r}")

    # --- hash determinism + sensitivity ---
    regions_a, _ = extract_regions("a.sh", _FIXTURE_WHITESPACE_SENSITIVE_A)
    regions_b, _ = extract_regions("b.sh", _FIXTURE_WHITESPACE_SENSITIVE_B)
    if len(regions_a) != 1 or len(regions_b) != 1:
        failures.append(f"WHITESPACE fixtures: expected 1 region each, got {regions_a!r} / {regions_b!r}")
    elif hash_region(regions_a[0]) == hash_region(regions_b[0]):
        failures.append("WHITESPACE: hash should differ when internal whitespace changes — got same hash")

    # --- invocation discovery ---
    inv = find_claude_invocations("x.sh", _FIXTURE_BARE_INVOCATION)
    if len(inv) != 1:
        failures.append(f"BARE: expected 1 invocation, got {inv!r}")
    inv = find_claude_invocations("x.sh", _FIXTURE_COMMENT_REFERENCE)
    if len(inv) != 1:
        failures.append(f"COMMENT REFERENCE: expected 1 invocation (comment skipped, real one kept), got {inv!r}")

    # --- end-to-end scan: bare invocation → finding ---
    # Redirect scan()'s stdout into a string so the fixture-driven finding
    # doesn't leak into the user-visible self-test output.
    import contextlib
    import io
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bare_path = os.path.join(tmp, "bare.sh")
        with open(bare_path, "w") as fh:
            fh.write(_FIXTURE_BARE_INVOCATION)
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            bare_findings = scan([bare_path], [])
        if bare_findings == 0:
            failures.append("SCAN(bare): expected at least one finding, got 0")

        ok_path = os.path.join(tmp, "ok.sh")
        with open(ok_path, "w") as fh:
            fh.write(_FIXTURE_OK)
        regions, _ = extract_regions(ok_path, _FIXTURE_OK)
        digest = hash_region(regions[0])
        entry = AllowlistEntry(sha256=digest, description="test")
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            ok_findings = scan([ok_path], [entry])
        if ok_findings != 0:
            failures.append(
                f"SCAN(ok+allowlist): expected 0 findings, got {ok_findings}; "
                f"output:\n{sink.getvalue()}"
            )

    # --- allowlist parser ---
    # Implicit string-literal concatenation binds tighter than the `*`
    # repetition, so we use explicit `+` to keep the fixture readable.
    allowlist_fixture = (
        "# header comment\n"
        + ("0" * 64) + "  description here\n"
        + "\n"
        + "abc-not-sha256\n"
    )
    entries, errors = parse_allowlist(allowlist_fixture)
    if len(entries) != 1:
        failures.append(f"ALLOWLIST: expected 1 entry, got {entries!r}")
    if not errors or "not a sha256" not in errors[0]:
        failures.append(f"ALLOWLIST: expected sha256 error, got {errors!r}")

    if failures:
        print("Self-test failures:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(
        "Self-test ok: region extraction, hash sensitivity, invocation discovery, "
        "scan integration, allowlist parser."
    )
    return 0


# ---------- main ----------


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

    try:
        with open(ALLOWLIST_PATH, encoding="utf-8") as fh:
            allowlist_text = fh.read()
    except FileNotFoundError:
        allowlist_text = ""
    allowlist_entries, allowlist_errors = parse_allowlist(allowlist_text)
    if allowlist_errors:
        for err in allowlist_errors:
            print(err, file=sys.stderr)
        return 2

    return 1 if scan(paths, allowlist_entries) else 0


if __name__ == "__main__":
    sys.exit(main())
