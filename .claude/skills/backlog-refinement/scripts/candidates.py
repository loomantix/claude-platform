#!/usr/bin/env python3
"""List the backlog-refinement queue: open issues not yet assessed for agent-readiness.

An issue is "un-refined" when it carries neither `agent: refined` nor any
`agent-bail:*` label and is not already `dev: agent`. Those are the issues
`/backlog-refinement refine` should process. Epics and obvious tracking issues
are surfaced separately so the operator can see them without them polluting the
work queue.

Mirrors the gh-invocation conventions of `../../issues/scripts/ready.py`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

REFINED_LABEL = "agent: refined"
READY_LABEL = "dev: agent"
BAIL_PREFIX = "agent-bail:"
# Surfaced but never auto-queued — these read as coordination, not bounded work.
EPIC_TITLE_MARKERS = ("epic:", "extractable as @")
LABEL_PREFIXES_TO_SHOW = ("area:", "dev:", "agent-bail:", "agent:", "status:", "priority:")


def fetch_open_issues() -> list[dict[str, Any]]:
    """Every open issue with the fields refinement triage needs."""
    cmd = [
        "gh", "issue", "list",
        "--state", "open",
        "--limit", "1000",
        "--json", "number,title,labels,assignees,url",
    ]
    # 60s timeout matches ready.py: a hung GitHub API shouldn't stall callers.
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


def label_names(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


def classify(issue: dict[str, Any]) -> str:
    """One of: ready | excluded | epic | unrefined."""
    labels = label_names(issue)
    if READY_LABEL in labels:
        return "ready"
    if any(name.startswith(BAIL_PREFIX) for name in labels):
        return "excluded"
    if REFINED_LABEL in labels:
        # Assessed but neither ready nor bailed — treat as excluded-without-reason.
        return "excluded"
    title = issue["title"].lower()
    if any(marker in title for marker in EPIC_TITLE_MARKERS):
        return "epic"
    return "unrefined"


def format_row(issue: dict[str, Any]) -> str:
    display = [n for n in label_names(issue) if n.startswith(LABEL_PREFIXES_TO_SHOW)]
    label_str = " ".join(f"[{n}]" for n in display)
    assignees = issue.get("assignees") or []
    assignee = f"@{assignees[0]['login']}" if assignees else "unassigned"
    title = issue["title"]
    if len(title) > 68:
        title = title[:65] + "..."
    return f"#{issue['number']:<6} {label_str:<48} ({assignee:<15}) {title}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="output JSON instead of a table")
    parser.add_argument("--limit", type=int, default=40, help="max rows to print (default 40)")
    parser.add_argument(
        "--include-refined", action="store_true",
        help="also list issues already assessed (ready / excluded)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = fetch_open_issues()

    buckets: dict[str, list[dict[str, Any]]] = {
        "unrefined": [], "ready": [], "excluded": [], "epic": [],
    }
    for issue in issues:
        buckets[classify(issue)].append(issue)

    for items in buckets.values():
        items.sort(key=lambda i: i["number"])

    if args.json:
        # The work queue is `unrefined` (+ epics for visibility); counts for the rest.
        print(json.dumps({
            "counts": {k: len(v) for k, v in buckets.items()},
            "unrefined": buckets["unrefined"][: args.limit],
            "epic": buckets["epic"],
        }, indent=2))
        return 0

    c = {k: len(v) for k, v in buckets.items()}
    print(
        f"Open: {len(issues)}  |  ready (dev:agent): {c['ready']}  |  "
        f"excluded (agent-bail:*): {c['excluded']}  |  epics: {c['epic']}  |  "
        f"UN-REFINED: {c['unrefined']}"
    )

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        print(f"\n{title} ({len(rows)}):")
        for issue in rows[: args.limit]:
            print(format_row(issue))

    section("Un-refined — refinement queue", buckets["unrefined"])
    section("Epics / coordination (review manually, do not auto-queue)", buckets["epic"])
    if args.include_refined:
        section("Ready (dev: agent)", buckets["ready"])
        section("Excluded (agent-bail:*)", buckets["excluded"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
