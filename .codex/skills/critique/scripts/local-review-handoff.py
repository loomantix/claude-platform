#!/usr/bin/env python3
"""Post and verify deterministic cross-engine local-review handoffs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast


CURRENT_ACTOR: str | None = None
# Engine identities this handoff surface accepts. Must stay in step with the
# marker grammar below and with the engines the review-ledger helper accepts.
ENGINES = ("codex", "claude", "gemini")
ENGINE_LABELS = {"codex": "Codex", "claude": "Claude", "gemini": "Gemini"}
TIER_CAPS = {"lean": 2, "deep": 4}
HANDOFF_V1_RE = re.compile(
    r"^<!-- local-review-handoff:v1 "
    r"from=(?P<from_engine>codex|claude|gemini) "
    r"to=(?P<to_engine>codex|claude|gemini) "
    r"round=(?P<round>[1-9][0-9]*) "
    r"base=(?P<base>[0-9a-f]{40}) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"outcome=(?P<outcome>clean|minor|material|blocked) "
    r"content-sha256=(?P<content_sha>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)
RUN_V1_RE = re.compile(
    r"^<!-- local-review-run:v1 "
    r"id=(?P<run_id>[0-9a-f]{64}) "
    r"tier=(?P<tier>lean|deep) "
    r"max-rounds=(?P<max_rounds>[1-4]) "
    r"base=(?P<base>[0-9a-f]{40}) "
    r"start-head=(?P<start_head>[0-9a-f]{40}) "
    r"supersedes=(?P<supersedes>none|[1-9][0-9]*) "
    r"content-sha256=(?P<content_sha>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)
RUN_END_V1_RE = re.compile(
    r"^<!-- local-review-run-end:v1 "
    r"id=(?P<run_id>[0-9a-f]{64}) "
    r"outcome=(?P<outcome>converged|exhausted|aborted) "
    r"head=(?P<head>[0-9a-f]{40}) -->$",
    re.MULTILINE,
)
PASS_V3_RE = re.compile(
    r"^<!-- local-review-pass:v3 "
    r"engine=(?P<engine>codex|claude|gemini|antigravity) "
    r"round=(?P<round>[1-9][0-9]*) base=[0-9a-f]{40} "
    r"head=[0-9a-f]{40} result-sha256=[0-9a-f]{64} -->$",
    re.MULTILINE,
)
COMPLETE_V3_RE = re.compile(
    r"^<!-- local-review-complete:v3 "
    r"engine=(?P<engine>codex|claude|gemini|antigravity) "
    r"round=(?P<round>[1-9][0-9]*) base=[0-9a-f]{40} "
    r"before=[0-9a-f]{40} head=[0-9a-f]{40} "
    r"classification=(?:minor|material) fingerprints=[A-Za-z0-9._:/,-]* "
    r"result-sha256=[0-9a-f]{64} -->$",
    re.MULTILINE,
)


class HandoffError(RuntimeError):
    """A fail-closed handoff validation or mutation error."""


def _fail(message: str) -> NoReturn:
    raise HandoffError(message)


def _run_gh(args: list[str], payload: dict[str, Any] | None = None) -> str:
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic returned"
        _fail(f"GitHub operation failed: {detail}")
    return result.stdout


def _json_output(args: list[str], payload: dict[str, Any] | None = None) -> Any:
    try:
        return json.loads(_run_gh(args, payload))
    except json.JSONDecodeError as error:
        raise HandoffError("GitHub returned invalid JSON") from error


def _current_actor() -> str:
    global CURRENT_ACTOR
    if CURRENT_ACTOR is None:
        actor = _run_gh(["api", "user", "--jq", ".login"]).strip()
        if not actor:
            _fail("could not resolve the authenticated GitHub actor")
        CURRENT_ACTOR = actor
    return CURRENT_ACTOR


def _flatten_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        _fail("GitHub returned malformed PR-comment pagination")
    rows: list[dict[str, Any]] = []
    for page in value:
        if any(not isinstance(row, dict) for row in page):
            _fail("GitHub returned malformed PR-comment rows")
        rows.extend(cast(list[dict[str, Any]], page))
    return rows


def _issue_comments(repo: str, pr: int) -> list[dict[str, Any]]:
    rows = _flatten_pages(
        _json_output(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{pr}/comments?per_page=100",
            ]
        )
    )
    actor = _current_actor()
    return [
        row
        for row in rows
        if isinstance(row.get("user"), dict) and row["user"].get("login") == actor
    ]


def _verify_head(repo: str, pr: int, expected_head: str) -> None:
    actual = _run_gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        ]
    ).strip()
    if actual != expected_head:
        _fail(
            f"PR head mismatch: expected {expected_head}, found {actual or '<empty>'}"
        )


def _read_context(path_value: str | None) -> str:
    if path_value is None:
        return ""
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        _fail("handoff context must be a regular non-symlink file")
    try:
        context = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise HandoffError("handoff context must be valid UTF-8") from error
    if "\x00" in context:
        _fail("handoff context contains NUL")
    if "<!-- local-review" in context:
        _fail("handoff context must not contain a local-review marker")
    return context


def _post_issue_comment(repo: str, pr: int, marker: str, body: str) -> tuple[int, bool]:
    comment_id = _matching_body(_issue_comments(repo, pr), marker, body)
    replayed = comment_id is not None
    if comment_id is None:
        try:
            response = _json_output(
                ["api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments"],
                {"body": body},
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("id"), int
            ):
                _fail("GitHub accepted the mutation but returned no comment ID")
            comment_id = cast(int, response["id"])
        except HandoffError:
            comment_id = _matching_body(_issue_comments(repo, pr), marker, body)
            if comment_id is None:
                raise
            replayed = True
    _verify_issue_comment(repo, comment_id, body)
    if _matching_body(_issue_comments(repo, pr), marker, body) != comment_id:
        _fail("comment idempotency key did not resolve to the posted comment")
    return comment_id, replayed


def _canonical_digest(payload: dict[str, Any]) -> str:
    """Hash a marker payload under the one canonicalization every marker uses.

    Every replayed marker verifies by recomputing this digest, so the JSON
    canonicalization has to be identical for all marker families. Keeping it in
    one place means an `ensure_ascii` or separator change cannot silently apply
    to one family and not another.
    """
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_digest(
    *,
    tier: str,
    max_rounds: int,
    base: str,
    start_head: str,
    supersedes: int | None,
    content: str,
) -> str:
    return _canonical_digest(
        {
            "base": base,
            "content": content,
            "max_rounds": max_rounds,
            "start_head": start_head,
            "supersedes": supersedes,
            "tier": tier,
        }
    )


def _run_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        body = row.get("body")
        comment_id = row.get("id")
        if not isinstance(body, str) or not isinstance(comment_id, int):
            continue
        if not body.startswith("<!-- local-review-run:v1"):
            continue
        matches = list(RUN_V1_RE.finditer(body))
        if (
            len(matches) != 1
            or matches[0].start() != 0
            or not body[matches[0].end() :].startswith("\n")
        ):
            _fail("local-review run marker is malformed")
        marker = matches[0]
        content = body[marker.end() + 1 :]
        supersedes_text = marker.group("supersedes")
        supersedes = None if supersedes_text == "none" else int(supersedes_text)
        max_rounds = int(marker.group("max_rounds"))
        if max_rounds != TIER_CAPS[marker.group("tier")]:
            _fail("local-review run tier and cap disagree")
        expected = _run_digest(
            tier=marker.group("tier"),
            max_rounds=max_rounds,
            base=marker.group("base"),
            start_head=marker.group("start_head"),
            supersedes=supersedes,
            content=content,
        )
        if expected != marker.group("run_id") or expected != marker.group(
            "content_sha"
        ):
            _fail("local-review run content digest is invalid")
        records.append(
            {
                "base": marker.group("base"),
                "body": body,
                "comment_id": comment_id,
                "content": content,
                "max_rounds": max_rounds,
                "run_id": marker.group("run_id"),
                "start_head": marker.group("start_head"),
                "supersedes": supersedes,
                "tier": marker.group("tier"),
            }
        )
    records.sort(key=lambda record: cast(int, record["comment_id"]))
    canonical: list[dict[str, Any]] = []
    aliases: dict[int, int] = {}
    by_run_id: dict[str, dict[str, Any]] = {}
    for record in records:
        prior = by_run_id.get(cast(str, record["run_id"]))
        if prior is None:
            by_run_id[cast(str, record["run_id"])] = record
            canonical.append(record)
            continue
        if prior["body"] != record["body"]:
            _fail("duplicate local-review run id has conflicting content")
        aliases[cast(int, record["comment_id"])] = cast(int, prior["comment_id"])
    records = canonical
    for record in records:
        supersedes = record["supersedes"]
        if isinstance(supersedes, int) and supersedes in aliases:
            record["supersedes"] = aliases[supersedes]
    for index, record in enumerate(records):
        expected_parent: int | None = (
            None if index == 0 else cast(int, records[index - 1]["comment_id"])
        )
        if record["supersedes"] != expected_parent:
            _fail("local-review run supersession chain is incomplete or forked")
    return records


def _run_end(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for row in rows:
        body = row.get("body")
        comment_id = row.get("id")
        if not isinstance(body, str) or not isinstance(comment_id, int):
            continue
        marker = RUN_END_V1_RE.fullmatch(body)
        if marker is not None and marker.group("run_id") == run_id:
            matches.append(
                {
                    "comment_id": comment_id,
                    "head": marker.group("head"),
                    "outcome": marker.group("outcome"),
                }
            )
    if len(matches) > 1:
        _fail("local-review run has more than one terminal marker")
    return matches[0] if matches else None


def _start_run(args: argparse.Namespace) -> None:
    rows = _issue_comments(args.repo, args.pr)
    records = _run_records(rows)
    previous = records[-1] if records else None
    content = _read_context(args.authorization_file).strip()
    if not content:
        _fail("review-run authorization must not be empty")
    max_rounds = TIER_CAPS[args.tier]
    previous_end = (
        None if previous is None else _run_end(rows, cast(str, previous["run_id"]))
    )
    if (
        previous is not None
        and previous_end is None
        and not args.restart
        and previous["tier"] == args.tier
        and previous["base"] == args.base
        and previous["start_head"] == args.head
        and previous["content"] == content
    ):
        _verify_head(args.repo, args.pr, args.head)
        print(
            json.dumps(
                {
                    "comment_id": previous["comment_id"],
                    "first_round": _round_offset(
                        rows, cast(int, previous["comment_id"])
                    )
                    + 1,
                    "max_rounds": max_rounds,
                    "replayed": True,
                    "run_id": previous["run_id"],
                    "tier": args.tier,
                    "verified": True,
                },
                sort_keys=True,
            )
        )
        return
    if previous is not None and not args.restart:
        _fail("a local-review run already exists; an explicit restart is required")
    if previous is not None and previous_end is None:
        _fail("the previous local-review run must be ended before an explicit restart")
    supersedes = None if previous is None else cast(int, previous["comment_id"])
    run_id = _run_digest(
        tier=args.tier,
        max_rounds=max_rounds,
        base=args.base,
        start_head=args.head,
        supersedes=supersedes,
        content=content,
    )
    marker = (
        f"<!-- local-review-run:v1 id={run_id} tier={args.tier} "
        f"max-rounds={max_rounds} base={args.base} start-head={args.head} "
        f"supersedes={supersedes if supersedes is not None else 'none'} "
        f"content-sha256={run_id} -->"
    )
    body = f"{marker}\n{content}"
    _verify_head(args.repo, args.pr, args.head)
    comment_id, replayed = _post_issue_comment(args.repo, args.pr, marker, body)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "max_rounds": max_rounds,
                "first_round": _round_offset(rows, comment_id) + 1,
                "replayed": replayed,
                "run_id": run_id,
                "tier": args.tier,
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _round_offset(rows: list[dict[str, Any]], start_comment_id: int) -> int:
    """Reserve historical PR-wide identities without spending the new run budget."""
    return max(
        (
            int(marker.group("round"))
            for row in rows
            if isinstance(row.get("id"), int)
            and cast(int, row["id"]) < start_comment_id
            and isinstance(row.get("body"), str)
            for pattern in (PASS_V3_RE, COMPLETE_V3_RE)
            for marker in pattern.finditer(row["body"])
        ),
        default=0,
    )


def _authorize_pass(args: argparse.Namespace) -> None:
    rows = _issue_comments(args.repo, args.pr)
    records = _run_records(rows)
    if not records:
        _fail("no authenticated local-review run exists")
    run = records[-1]
    if _run_end(rows, cast(str, run["run_id"])) is not None:
        _fail("the current local-review run has ended")
    if run["base"] != args.base:
        _fail("local-review run base does not match the requested pass")
    start_comment_id = cast(int, run["comment_id"])
    offset = _round_offset(rows, start_comment_id)
    run_round = args.round - offset
    if run_round < 1:
        _fail("requested round predates this run; use a fresh PR-wide round")
    if run_round > cast(int, run["max_rounds"]):
        _fail(
            f"review round {args.round} exceeds the {run['tier']} cap "
            f"of {run['max_rounds']}"
        )
    existing: set[tuple[str, int]] = set()
    highest_round = offset
    for row in rows:
        if (
            not isinstance(row.get("id"), int)
            or cast(int, row["id"]) <= start_comment_id
        ):
            continue
        body = row.get("body")
        if not isinstance(body, str):
            continue
        for marker in (*PASS_V3_RE.finditer(body), *COMPLETE_V3_RE.finditer(body)):
            engine = marker.group("engine")
            if engine == "antigravity":
                engine = "gemini"
            round_number = int(marker.group("round"))
            existing.add((engine, round_number))
            highest_round = max(highest_round, round_number)
    if (args.engine, args.round) in existing:
        _fail("this engine already completed the requested run round")
    if args.round > highest_round + 1:
        _fail("review passes may not skip a run round")
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "engine": args.engine,
                "head": args.head,
                "max_rounds": run["max_rounds"],
                "round": args.round,
                "run_round": run_round,
                "run_id": run["run_id"],
                "tier": run["tier"],
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _finish_run(args: argparse.Namespace) -> None:
    rows = _issue_comments(args.repo, args.pr)
    records = _run_records(rows)
    if not records:
        _fail("no authenticated local-review run exists")
    run = records[-1]
    existing = _run_end(rows, cast(str, run["run_id"]))
    marker = (
        f"<!-- local-review-run-end:v1 id={run['run_id']} "
        f"outcome={args.outcome} head={args.head} -->"
    )
    if existing is not None:
        if existing["head"] != args.head or existing["outcome"] != args.outcome:
            _fail("local-review run already ended with a different result")
        _verify_head(args.repo, args.pr, args.head)
        print(
            json.dumps(
                {**existing, "replayed": True, "run_id": run["run_id"]}, sort_keys=True
            )
        )
        return
    _verify_head(args.repo, args.pr, args.head)
    comment_id, replayed = _post_issue_comment(args.repo, args.pr, marker, marker)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "head": args.head,
                "outcome": args.outcome,
                "replayed": replayed,
                "run_id": run["run_id"],
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _handoff_content(args: argparse.Namespace, context: str) -> str:
    next_engine = cast(str, args.to_engine)
    label = ENGINE_LABELS[next_engine]
    context = (
        context.strip()
        or "No additional context. Reconstruct the pass from the PR ledger."
    )
    return f"""## Local review handoff: {args.from_engine} to {next_engine}

Start a fresh {label} terminal session in an isolated worktree, then give it this prompt:

```text
Continue review on PR #{args.pr}.

Find and follow the latest authenticated local-review-handoff:v1 comment before
reviewing. Verify that its exact head is still current, load the complete PR
ledger including resolved threads and prior attestations, and continue as the
{next_engine} reviewer against the pinned base. Do not invoke the other review
engine from this session. When this pass ends, inspect every declared
engine's authenticated outcome for the round. If they satisfy the repository's convergence
rule, publish the terminal review result and follow its configured finalization
step without another handoff. Otherwise publish the next authenticated handoff
comment and stop so the user can start the following session.
```

Pinned review state:

- repository: `{args.repo}`
- PR: `#{args.pr}`
- base: `{args.base}`
- head: `{args.head}`
- completed pass: `{args.from_engine}` round `{args.round}` (`{args.outcome}`)
- next reviewer: `{next_engine}`

Pass context: {context}
"""


def _handoff_digest(
    *,
    from_engine: str,
    to_engine: str,
    round_number: int,
    base: str,
    head: str,
    outcome: str,
    content: str,
) -> str:
    return _canonical_digest(
        {
            "base": base,
            "content": content,
            "from_engine": from_engine,
            "head": head,
            "outcome": outcome,
            "round": round_number,
            "to_engine": to_engine,
        }
    )


def _verify_issue_comment(repo: str, comment_id: int, expected_body: str) -> None:
    response = _json_output(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    if (
        not isinstance(response, dict)
        or response.get("body") != expected_body
        or not isinstance(response.get("user"), dict)
        or response["user"].get("login") != _current_actor()
    ):
        _fail(f"could not verify PR comment {comment_id} after posting")


def _matching_body(rows: list[dict[str, Any]], marker: str, body: str) -> int | None:
    matches = [row for row in rows if marker in str(row.get("body", ""))]
    if not matches:
        return None
    if len(matches) != 1:
        _fail("handoff idempotency key is duplicated")
    row = matches[0]
    if row.get("body") != body or not isinstance(row.get("id"), int):
        _fail("handoff idempotency key already exists with conflicting content")
    return cast(int, row["id"])


def _post_handoff(args: argparse.Namespace) -> None:
    if args.from_engine == args.to_engine:
        _fail("review handoff engines must be different")
    content = _handoff_content(args, _read_context(args.context_file))
    digest = _handoff_digest(
        from_engine=args.from_engine,
        to_engine=args.to_engine,
        round_number=args.round,
        base=args.base,
        head=args.head,
        outcome=args.outcome,
        content=content,
    )
    marker = (
        f"<!-- local-review-handoff:v1 from={args.from_engine} "
        f"to={args.to_engine} round={args.round} base={args.base} "
        f"head={args.head} outcome={args.outcome} content-sha256={digest} -->"
    )
    body = f"{marker}\n{content}"
    _verify_head(args.repo, args.pr, args.head)
    comment_id, replayed = _post_issue_comment(args.repo, args.pr, marker, body)
    _verify_head(args.repo, args.pr, args.head)
    print(
        json.dumps(
            {
                "comment_id": comment_id,
                "from_engine": args.from_engine,
                "head": args.head,
                "replayed": replayed,
                "to_engine": args.to_engine,
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _show_handoff(args: argparse.Namespace) -> None:
    candidates: list[tuple[int, str]] = []
    for row in _issue_comments(args.repo, args.pr):
        body = row.get("body")
        comment_id = row.get("id")
        if not isinstance(body, str) or not isinstance(comment_id, int):
            continue
        if body.startswith("<!-- local-review-handoff:v1"):
            candidates.append((comment_id, body))
    if not candidates:
        _fail("no authenticated local-review handoff comment was found")
    comment_id, body = max(candidates, key=lambda candidate: candidate[0])
    matches = list(HANDOFF_V1_RE.finditer(body))
    if len(matches) != 1:
        _fail("latest local-review handoff marker is malformed")
    marker = matches[0]
    if marker.start() != 0 or not body[marker.end() :].startswith("\n"):
        _fail("a local-review handoff marker must start the PR comment")
    content = body[marker.end() + 1 :]
    digest = _handoff_digest(
        from_engine=marker.group("from_engine"),
        to_engine=marker.group("to_engine"),
        round_number=int(marker.group("round")),
        base=marker.group("base"),
        head=marker.group("head"),
        outcome=marker.group("outcome"),
        content=content,
    )
    if digest != marker.group("content_sha"):
        _fail("latest local-review handoff content digest is invalid")
    if marker.group("to_engine") != args.engine:
        _fail(
            f"latest local-review handoff targets {marker.group('to_engine')}, not {args.engine}"
        )
    _verify_head(args.repo, args.pr, marker.group("head"))
    print(
        json.dumps(
            {
                "base": marker.group("base"),
                "body": body,
                "comment_id": comment_id,
                "from_engine": marker.group("from_engine"),
                "head": marker.group("head"),
                "outcome": marker.group("outcome"),
                "round": int(marker.group("round")),
                "to_engine": marker.group("to_engine"),
                "verified": True,
            },
            sort_keys=True,
        )
    )


def _sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError(
            "must be a full lowercase 40-character commit SHA"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    post = commands.add_parser("post-handoff")
    post.add_argument("--repo", required=True)
    post.add_argument("--pr", required=True, type=int)
    post.add_argument("--head", required=True, type=_sha)
    post.add_argument("--base", required=True, type=_sha)
    post.add_argument("--from-engine", required=True, choices=ENGINES)
    post.add_argument("--to-engine", required=True, choices=ENGINES)
    post.add_argument("--round", required=True, type=int)
    post.add_argument(
        "--outcome", required=True, choices=("clean", "minor", "material", "blocked")
    )
    post.add_argument("--context-file")
    post.set_defaults(handler=_post_handoff)

    show = commands.add_parser("show-handoff")
    show.add_argument("--repo", required=True)
    show.add_argument("--pr", required=True, type=int)
    show.add_argument("--engine", required=True, choices=ENGINES)
    show.set_defaults(handler=_show_handoff)

    start = commands.add_parser("start-run")
    start.add_argument("--repo", required=True)
    start.add_argument("--pr", required=True, type=int)
    start.add_argument("--head", required=True, type=_sha)
    start.add_argument("--base", required=True, type=_sha)
    start.add_argument("--tier", required=True, choices=sorted(TIER_CAPS))
    start.add_argument("--authorization-file", required=True)
    start.add_argument("--restart", action="store_true")
    start.set_defaults(handler=_start_run)

    authorize = commands.add_parser("authorize-pass")
    authorize.add_argument("--repo", required=True)
    authorize.add_argument("--pr", required=True, type=int)
    authorize.add_argument("--head", required=True, type=_sha)
    authorize.add_argument("--base", required=True, type=_sha)
    authorize.add_argument("--engine", required=True, choices=ENGINES)
    authorize.add_argument("--round", required=True, type=int)
    authorize.set_defaults(handler=_authorize_pass)

    finish = commands.add_parser("finish-run")
    finish.add_argument("--repo", required=True)
    finish.add_argument("--pr", required=True, type=int)
    finish.add_argument("--head", required=True, type=_sha)
    finish.add_argument(
        "--outcome", required=True, choices=("converged", "exhausted", "aborted")
    )
    finish.set_defaults(handler=_finish_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "round", 1) < 1:
        _fail("round must be positive")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as error:
        print(f"local-review-handoff: {error}", file=sys.stderr)
        raise SystemExit(1) from error
