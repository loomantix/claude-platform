#!/usr/bin/env python3
"""Add a dependency reference between two GitHub issues.

Writes bidirectional refs under a `## Dependencies` section in each issue's
body so that `ready.py` sees the relationship from either side.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

SECTION_HEADER = "## Dependencies"


def fetch_body(num: int) -> str:
    result = subprocess.run(
        ["gh", "issue", "view", str(num), "--json", "body"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout).get("body") or ""


def has_ref(body: str, phrase: str, target: int) -> bool:
    pattern = rf"(?im)^\s*[-*]?\s*{re.escape(phrase)}[:\s]+#{target}\b"
    return bool(re.search(pattern, body))


def add_ref(body: str, line: str) -> str:
    lines = body.splitlines()
    # Find an actual header line, not a prose mention like "see the ## Dependencies section".
    header_idx = next(
        (i for i, raw in enumerate(lines) if raw.strip() == SECTION_HEADER),
        None,
    )
    if header_idx is None:
        prefix = body.rstrip() + "\n\n" if body.strip() else ""
        return f"{prefix}{SECTION_HEADER}\n{line}\n"
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    while end > header_idx + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, line)
    return "\n".join(lines)


def set_body(num: int, body: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as fp:
        fp.write(body)
        path = fp.name
    try:
        result = subprocess.run(
            ["gh", "issue", "edit", str(num), "--body-file", path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            sys.exit(result.returncode)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def update(num: int, phrase: str, target: int) -> None:
    body = fetch_body(num)
    if has_ref(body, phrase, target):
        print(f"#{num} already has '{phrase} #{target}' — skipping")
        return
    new_body = add_ref(body, f"- {phrase} #{target}")
    set_body(num, new_body)
    print(f"#{num}: added '{phrase} #{target}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=int, help="issue number")
    parser.add_argument("relation", choices=["blocks", "blocked-by"])
    parser.add_argument("target", type=int, help="other issue number")
    args = parser.parse_args()

    if args.source == args.target:
        sys.stderr.write("Cannot link an issue to itself.\n")
        return 1

    if args.relation == "blocks":
        update(args.source, "Blocks", args.target)
        update(args.target, "Blocked by", args.source)
    else:
        update(args.source, "Blocked by", args.target)
        update(args.target, "Blocks", args.source)

    return 0


if __name__ == "__main__":
    sys.exit(main())
