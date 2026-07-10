"""Unit coverage for the `/issues ready` query script.

Focus is the PR-addressed exclusion (`fetch_addressed_numbers`) and its
helpers — the GitHub-touching code is exercised by monkeypatching the
two subprocess wrappers so the tests stay hermetic.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest


def _kw(mod: ModuleType, body: str) -> set[int]:
    return {int(m.group(1)) for m in mod.CLOSING_KEYWORD_RE.finditer(body)}


def test_closing_keyword_regex_matches_all_keyword_forms(ready_mod: ModuleType) -> None:
    body = "Fixes #12, closes #34. Resolved #5; fix: #6; closed #7; resolves #8"
    assert _kw(ready_mod, body) == {12, 34, 5, 6, 7, 8}


def test_closing_keyword_regex_is_case_insensitive(ready_mod: ModuleType) -> None:
    assert _kw(ready_mod, "CLOSES #9 / Fix #10") == {9, 10}


def test_closing_keyword_regex_ignores_bare_mentions(ready_mod: ModuleType) -> None:
    # A plain reference is not a closing keyword — must not be excluded.
    assert _kw(ready_mod, "see #99, related to #100, part of #101") == set()


def test_closing_keyword_regex_respects_word_boundary(ready_mod: ModuleType) -> None:
    # "prefix" / "refixes" must not trip the fix/resolve stems.
    assert _kw(ready_mod, "prefix #11 and refixes #12") == set()


def test_blocker_regex_still_parses_dependencies(ready_mod: ModuleType) -> None:
    assert ready_mod.parse_blockers("Blocked by #3\n- Depends on #4") == {3, 4}


def test_hard_exclude_labels_cover_blocked_and_on_staging(ready_mod: ModuleType) -> None:
    assert ready_mod.HARD_EXCLUDE_LABELS == {"status: blocked", "status: on-staging"}


def test_is_hard_excluded_matches_either_state_in_any_position(ready_mod: ModuleType) -> None:
    assert ready_mod.is_hard_excluded(["status: blocked"])
    assert ready_mod.is_hard_excluded(["status: on-staging"])
    # Position-independent: the exclude label can sit among unrelated labels.
    assert ready_mod.is_hard_excluded(["area: backend", "status: on-staging", "dev: agent"])


def test_is_hard_excluded_matches_any_agent_bail_label(ready_mod: ModuleType) -> None:
    # A bail label excludes even when a stale `dev: agent` admission label remains
    # (the removal is two separate gh ops and may not have landed).
    assert ready_mod.is_hard_excluded(["agent-bail: spec-gap"])
    assert ready_mod.is_hard_excluded(["dev: agent", "agent-bail: stale", "agent: refined"])


def test_is_hard_excluded_ignores_actionable_and_lookalike_labels(ready_mod: ModuleType) -> None:
    assert not ready_mod.is_hard_excluded([])
    assert not ready_mod.is_hard_excluded(["dev: agent", "area: backend", "priority: high"])
    # Substring / prefix lookalikes must not trip the exact-match exclusion.
    assert not ready_mod.is_hard_excluded(["status: on-staging-soak", "status: unblocked"])
    # A label that merely mentions "agent" but is not the bail prefix stays actionable.
    assert not ready_mod.is_hard_excluded(["dev: agent", "agent: refined"])


def test_ref_repo_extracts_owner_and_name(ready_mod: ModuleType) -> None:
    ref = {"number": 1, "repository": {"name": "platform", "owner": {"login": "acme"}}}
    assert ready_mod._ref_repo(ref) == "acme/platform"


def test_ref_repo_returns_none_when_incomplete(ready_mod: ModuleType) -> None:
    assert ready_mod._ref_repo({}) is None
    assert ready_mod._ref_repo({"repository": {"name": "x"}}) is None


def _ref(num: int, owner: str = "acme", name: str = "platform") -> dict[str, Any]:
    return {"number": num, "repository": {"name": name, "owner": {"login": owner}}}


def test_fetch_addressed_uses_closing_references_and_falls_back_to_keywords(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_prs = [
        # Authoritative link set wins; body keywords on the same PR are ignored.
        {"number": 100, "body": "fixes #999", "closingIssuesReferences": [_ref(10)]},
        # Empty link set -> fall back to closing keywords in the body.
        {"number": 101, "body": "closes #20 and fixes #21", "closingIssuesReferences": []},
        # Cross-repo closing reference must NOT shadow a local issue #99.
        {"number": 102, "body": "", "closingIssuesReferences": [_ref(99, name="other")]},
    ]
    merged_prs = [{"number": 200, "body": "", "closingIssuesReferences": [_ref(30)]}]

    calls: list[list[str]] = []

    def fake_pr_list(extra_args: list[str]) -> list[dict[str, Any]]:
        calls.append(extra_args)
        return open_prs if "open" in extra_args else merged_prs

    monkeypatch.setattr(ready_mod, "_current_repo", lambda: "acme/platform")
    monkeypatch.setattr(ready_mod, "_pr_list", fake_pr_list)

    assert ready_mod.fetch_addressed_numbers() == {10, 20, 21, 30}
    # Exactly two batched queries (open + merged) — no per-issue fan-out.
    assert len(calls) == 2
    assert calls[0] == ["--state", "open"]
    assert calls[1][:3] == ["--state", "merged", "--search"]
    assert calls[1][3].startswith("merged:>=")


def test_fetch_addressed_degrades_gracefully_on_api_error(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(extra_args: list[str]) -> list[dict[str, Any]]:
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(ready_mod, "_current_repo", lambda: "acme/platform")
    monkeypatch.setattr(ready_mod, "_pr_list", boom)
    # A PR-API failure must not crash the ready query — it excludes nothing.
    assert ready_mod.fetch_addressed_numbers() == set()


def test_fetch_addressed_keeps_refs_when_repo_unknown(
    ready_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    prs = [{"number": 1, "body": "", "closingIssuesReferences": [_ref(42, name="whatever")]}]
    monkeypatch.setattr(ready_mod, "_current_repo", lambda: None)
    monkeypatch.setattr(ready_mod, "_pr_list", lambda extra: prs if "open" in extra else [])
    # When the current repo can't be resolved, fall open rather than drop links.
    assert ready_mod.fetch_addressed_numbers() == {42}


def _issue(num: int, *, labels: list[str] | None = None, body: str = "") -> dict[str, Any]:
    return {
        "number": num,
        "title": f"issue {num}",
        "body": body,
        "labels": [{"name": n} for n in (labels or [])],
        "assignees": [],
    }


def test_main_drops_hard_excluded_and_pr_addressed_from_ready_queue(
    ready_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end wiring: main() applies every exclusion to the ready queue.

    The individual predicates are unit-tested above; this pins the assembly in
    main() so a refactor that unwires `is_hard_excluded` / the PR-addressed set
    can't pass silently. Covers all four drop paths + one issue that survives.
    """
    issues = [
        _issue(1),  # actionable — the only one that should survive
        _issue(2, labels=["status: on-staging"]),  # hard-excluded (already shipped)
        _issue(3, labels=["status: blocked"]),  # hard-excluded (blocked)
        _issue(4, body="Depends on #1"),  # open blocker (#1 is open) -> excluded
        _issue(5),  # addressed by a merged/open PR -> excluded
    ]
    monkeypatch.setattr(sys, "argv", ["ready", "--json"])
    monkeypatch.setattr(ready_mod, "fetch_issues", lambda extra: issues)
    monkeypatch.setattr(ready_mod, "fetch_addressed_numbers", lambda: {5})

    rc = ready_mod.main()

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [issue["number"] for issue in out] == [1]
