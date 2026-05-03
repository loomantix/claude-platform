#!/usr/bin/env python3
"""List open GitHub issues with no open blockers, sorted by priority.

Blockers are detected from:
  - Label `status: blocked` (hard exclude)
  - Body refs matching `Blocked by #N` or `Depends on #N` where #N is still open
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    """GitHub-issue priority labels, ordered most-urgent first.

    Centralizes the three-way mapping (label string ↔ ordering ↔ display tag)
    so that adding a new tier (e.g. `BLOCKER = -1`) only requires a single
    enum member rather than edits in three places.
    """

    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3
    UNKNOWN = 99

    @property
    def label(self) -> str:
        """The `priority: <level>` GitHub label for this tier (empty for UNKNOWN)."""
        return f"priority: {self.name.lower()}" if self is not Priority.UNKNOWN else ""

    @property
    def tag(self) -> str:
        """Compact display tag (e.g. `P0`, `P?`)."""
        return f"P{self.value}" if self is not Priority.UNKNOWN else "P?"

    @classmethod
    def from_label(cls, label: str) -> Priority:
        for p in cls:
            if p.label and p.label == label:
                return p
        return cls.UNKNOWN

# Match "Blocked by #123" / "- Depends on #123" at the start of a line.
BLOCKER_RE = re.compile(
    r"(?im)^\s*[-*]?\s*(?:blocked\s+by|depends\s+on)[:\s]+#(\d+)\b"
)

LABEL_PREFIXES_TO_SHOW = ("area:", "dev:", "source:", "status:")


def fetch_issues(extra_args: list[str]) -> list[dict[str, Any]]:
    cmd = [
        "gh", "issue", "list",
        "--state", "open",
        "--limit", "1000",
        "--json", "number,title,body,labels,assignees,url",
        *extra_args,
    ]
    # 60s timeout: a hung GitHub API can otherwise stall callers (e.g.
    # /agent-loop) that depend on this probe to advance.
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


def fetch_all_open_numbers() -> set[int]:
    """Fetch every open issue number (unfiltered).

    Blocker resolution must see *all* open issues, not just the filtered
    subset, so a dependent with e.g. `--agent` doesn't look ready when its
    blocker lacks the `dev: agent` label.
    """
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--state", "open", "--limit", "1000",
            "--json", "number",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return {issue["number"] for issue in json.loads(result.stdout)}


def label_names(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


def parse_blockers(body: str | None) -> set[int]:
    return {int(m.group(1)) for m in BLOCKER_RE.finditer(body or "")}


def priority_score(issue: dict[str, Any]) -> Priority:
    return min(
        (Priority.from_label(name) for name in label_names(issue)),
        default=Priority.UNKNOWN,
    )


def priority_tag(issue: dict[str, Any]) -> str:
    return priority_score(issue).tag


def format_row(issue: dict[str, Any]) -> str:
    display_labels = [
        name for name in label_names(issue)
        if name.startswith(LABEL_PREFIXES_TO_SHOW)
    ]
    label_str = " ".join(f"[{n}]" for n in display_labels)
    assignees = issue.get("assignees") or []
    assignee = f"@{assignees[0]['login']}" if assignees else "unassigned"
    title = issue["title"]
    if len(title) > 70:
        title = title[:67] + "..."
    return f"#{issue['number']:<6} {priority_tag(issue):<3} {label_str:<45} ({assignee:<15}) {title}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mine", action="store_true", help="only issues assigned to me")
    parser.add_argument("--unassigned", action="store_true", help="only unassigned issues")
    parser.add_argument("--agent", action="store_true", help='only issues labeled "dev: agent"')
    parser.add_argument("--priority", choices=["critical", "high", "medium", "low"])
    parser.add_argument("--area", help='e.g. "backend", "frontend", "packages"')
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="output JSON instead of table")
    return parser.parse_args()


def gh_filter_args(args: argparse.Namespace) -> list[str]:
    extra: list[str] = []
    if args.mine:
        extra += ["--assignee", "@me"]
    if args.agent:
        extra += ["--label", "dev: agent"]
    if args.priority:
        extra += ["--label", f"priority: {args.priority}"]
    if args.area:
        extra += ["--label", f"area: {args.area}"]
    return extra


def main() -> int:
    args = parse_args()
    filters = gh_filter_args(args)
    issues = fetch_issues(filters)
    # Blocker resolution must consider *all* open issues, not just the filtered set,
    # otherwise a blocker outside the filter looks "closed" and its dependent appears ready.
    open_nums = fetch_all_open_numbers() if filters else {i["number"] for i in issues}

    ready: list[dict[str, Any]] = []
    for issue in issues:
        if args.unassigned and issue.get("assignees"):
            continue
        labels = label_names(issue)
        if "status: blocked" in labels:
            continue
        blockers = parse_blockers(issue.get("body"))
        if blockers & open_nums:
            continue
        ready.append(issue)

    ready.sort(key=lambda i: (priority_score(i), i["number"]))
    ready = ready[: args.limit]

    if args.json:
        print(json.dumps(ready, indent=2))
        return 0

    if not ready:
        print("No ready issues found.")
        return 0

    print(f"Ready: {len(ready)} issue(s)")
    for issue in ready:
        print(format_row(issue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
