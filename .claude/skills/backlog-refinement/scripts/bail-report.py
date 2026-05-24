#!/usr/bin/env python3
"""Aggregate `agent-bail:*` issues + their inline RCA stubs for the post-loop RCA pass.

Reads every open/closed issue carrying an `agent-bail:*` label, parses the
`<!-- agent-loop-rca ... -->` stub (RUBRIC.md §4) from its comments when present,
and groups by category + A/B bucket so `/backlog-refinement rca` can turn the
run's bails into rubric edits. Bucket-A bails are flagged loudly: they mean
refinement tagged something `dev: agent` that the loop couldn't finish.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

BAIL_PREFIX = "agent-bail:"
# Bucket-A categories are preventable-by-prep; a loop bail here is a refinement miss.
BUCKET_A = {"agent-bail: stale", "agent-bail: spec-gap", "agent-bail: loop-mechanics"}
RCA_STUB_RE = re.compile(r"<!--\s*agent-loop-rca\s*(.*?)-->", re.DOTALL | re.IGNORECASE)


def run_gh(args: list[str]) -> Any:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout) if result.stdout.strip() else None


def fetch_bailed(since: str | None) -> list[dict[str, Any]]:
    """All issues (any state) with an agent-bail:* label, newest activity first."""
    issues = run_gh([
        "issue", "list", "--state", "all", "--limit", "1000",
        "--json", "number,title,labels,state,updatedAt,url",
    ]) or []
    out = []
    for issue in issues:
        labels = [label["name"] for label in issue.get("labels", [])]
        if not any(name.startswith(BAIL_PREFIX) for name in labels):
            continue
        if since and issue.get("updatedAt", "") < since:
            continue
        issue["_bail_labels"] = [n for n in labels if n.startswith(BAIL_PREFIX)]
        out.append(issue)
    return out


def parse_rca_stub(number: int) -> dict[str, str] | None:
    """Pull the most recent agent-loop-rca stub from an issue's comments."""
    data = run_gh(["issue", "view", str(number), "--json", "comments"]) or {}
    for comment in reversed(data.get("comments", [])):
        match = RCA_STUB_RE.search(comment.get("body", ""))
        if match:
            fields: dict[str, str] = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    fields[key.strip()] = val.strip()
            return fields
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="ISO date/time; only issues updated on/after it")
    parser.add_argument("--json", action="store_true", help="output JSON instead of a report")
    parser.add_argument(
        "--no-stubs", action="store_true",
        help="skip per-issue comment fetch (faster; omits RCA stub fields)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bailed = fetch_bailed(args.since)

    for issue in bailed:
        issue["_rca"] = None if args.no_stubs else parse_rca_stub(issue["number"])

    by_category: dict[str, list[dict[str, Any]]] = {}
    for issue in bailed:
        for label in issue["_bail_labels"]:
            by_category.setdefault(label, []).append(issue)

    if args.json:
        print(json.dumps({
            "total": len(bailed),
            "by_category": {k: [i["number"] for i in v] for k, v in by_category.items()},
            "issues": [
                {k: v for k, v in i.items() if not k.startswith("labels")}
                for i in bailed
            ],
        }, indent=2, default=str))
        return 0

    if not bailed:
        print("No agent-bail:* issues in window. Nothing to RCA.")
        return 0

    print(f"agent-bail issues in window: {len(bailed)}\n")
    bucket_a_hits = []
    for category in sorted(by_category, key=lambda c: (c not in BUCKET_A, c)):
        rows = by_category[category]
        flag = "  ⚠ BUCKET A — refinement miss" if category in BUCKET_A else ""
        print(f"{category}  ({len(rows)}){flag}")
        for issue in rows:
            rca = issue.get("_rca") or {}
            diff = rca.get("what-could-differ", "—")
            print(f"   #{issue['number']:<6} [{issue['state']:<6}] {issue['title'][:60]}")
            if diff != "—":
                print(f"            ↳ {diff}")
            if category in BUCKET_A:
                bucket_a_hits.append(issue["number"])
        print()

    if bucket_a_hits:
        print(
            f"→ {len(bucket_a_hits)} Bucket-A bail(s) {bucket_a_hits}: ask which RUBRIC §2 "
            "transformation / §1 check should have caught these at refinement time."
        )
    print("→ For repeated Bucket-B shapes, sharpen the §3 disqualifier. "
          "Append each lesson to LEARNINGS.md; bump rubric version if criteria changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
