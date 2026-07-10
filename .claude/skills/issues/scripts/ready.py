#!/usr/bin/env python3
"""List open GitHub issues with no open blockers, sorted by priority.

An issue is excluded from the ready list when any of these hold:
  - Label `status: blocked` (hard exclude) — blocked on an external dependency
  - Label `status: on-staging` (hard exclude) — fix merged to a staging/
    integration branch, awaiting release/promotion; done-but-pending, not
    actionable. An opt-in convention: repos that never apply it see no matching
    issues, so the exclusion is a harmless no-op there.
  - Body refs matching `Blocked by #N` or `Depends on #N` where #N is still open
  - It is the target of a closing reference from an OPEN pull request, or from a
    pull request MERGED within the last ADDRESSED_PR_WINDOW_DAYS days. This keeps
    issues already fixed as a side-item of a multi-issue PR (or by an in-review
    PR) out of the queue. It also covers promotion-flow repos, where a closing
    keyword on a merge to a non-default branch records the link but never
    auto-closes the issue, so a done-on-integration issue would otherwise linger.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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

# GitHub's closing keywords, used only as a per-PR fallback when a PR has no
# populated `closingIssuesReferences` (e.g. a link GitHub didn't auto-resolve).
# Mirrors the keyword set GitHub itself honors. A bare `#N` is same-repo.
CLOSING_KEYWORD_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+#(\d+)\b"
)

# How far back a MERGED PR still counts as having "addressed" an issue.
ADDRESSED_PR_WINDOW_DAYS = 30

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


def _current_repo() -> str | None:
    """Return the current repo as `owner/name`, or None if it can't be resolved."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ref_repo(ref: dict[str, Any]) -> str | None:
    repo = ref.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    return f"{owner}/{name}" if owner and name else None


def _pr_list(extra_args: list[str]) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "gh", "pr", "list",
            "--limit", "1000",
            "--json", "number,body,closingIssuesReferences",
            *extra_args,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh pr list failed")
    return json.loads(result.stdout)


def fetch_addressed_numbers(window_days: int = ADDRESSED_PR_WINDOW_DAYS) -> set[int]:
    """Issue numbers already addressed by an open or recently-merged PR.

    Authoritative source is `PullRequest.closingIssuesReferences` — the link set
    GitHub uses for auto-close — read in a single batched query per PR state, so
    latency does not scale with issue count. For any PR whose link set is empty
    we fall back to closing keywords (`fixes #N`, …) in its body, which catches
    links GitHub never auto-resolved (notably on non-default-branch merges).

    References are filtered to the current repo so a cross-repo PR closing its
    own `#N` can't shadow an unrelated local issue with the same number.

    Degrades to an empty set (with a stderr warning) if the PR API is
    unreachable: a transient PR-list failure must not stop ready issues from
    listing.
    """
    since = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()
    repo = _current_repo()
    try:
        prs = _pr_list(["--state", "open"])
        prs += _pr_list(["--state", "merged", "--search", f"merged:>={since}"])
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"warning: could not check PR-addressed issues ({exc}); excluding none\n"
        )
        return set()

    addressed: set[int] = set()
    for pr in prs:
        refs = [
            ref for ref in (pr.get("closingIssuesReferences") or [])
            if repo is None or _ref_repo(ref) in (None, repo)
        ]
        if refs:
            addressed.update(ref["number"] for ref in refs)
        else:
            addressed.update(
                int(m.group(1)) for m in CLOSING_KEYWORD_RE.finditer(pr.get("body") or "")
            )
    return addressed


def label_names(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


# Labels that hard-exclude an issue from the ready queue regardless of blockers
# or priority. These mark "not actionable now" lifecycle states, not urgency:
#   - status: blocked     -> blocked on an external dependency
#   - status: on-staging  -> fix merged to a staging/integration branch, awaiting
#                            release/promotion (done, pending); re-surfacing it
#                            just wastes an agent iteration rediscovering it is
#                            already shipped
#   - agent-bail:*        -> explicitly excluded by refinement or a prior loop
#                            run, even if a stale `dev: agent` label remains
# All are opt-in conventions — a repo that never applies them has no matching
# issues, so this exclusion is a harmless no-op there.
HARD_EXCLUDE_LABELS = frozenset({"status: blocked", "status: on-staging"})
BAIL_LABEL_PREFIX = "agent-bail:"


def is_hard_excluded(labels: list[str]) -> bool:
    """True if any label marks the issue not-actionable (blocked or already shipped)."""
    return any(
        label in HARD_EXCLUDE_LABELS or label.startswith(BAIL_LABEL_PREFIX)
        for label in labels
    )


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
    addressed = fetch_addressed_numbers()

    ready: list[dict[str, Any]] = []
    for issue in issues:
        if args.unassigned and issue.get("assignees"):
            continue
        labels = label_names(issue)
        if is_hard_excluded(labels):
            continue
        blockers = parse_blockers(issue.get("body"))
        if blockers & open_nums:
            continue
        if issue["number"] in addressed:
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
