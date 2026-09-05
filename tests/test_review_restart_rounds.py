"""Restarted runs retain PR-wide attestation identities and bounded budgets."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / ".codex/skills/critique/scripts/local-review-handoff.py"
    )
    spec = importlib.util.spec_from_file_location("restart_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_verify_head", lambda *args: None)
    monkeypatch.setattr(
        module,
        "_run_records",
        lambda rows: [
            {
                "comment_id": 20,
                "run_id": "d" * 64,
                "base": "b" * 40,
                "tier": "deep",
                "max_rounds": 4,
            }
        ],
    )
    monkeypatch.setattr(module, "_run_end", lambda *args: None)
    return module


def marker(engine: str, round_number: int) -> str:
    return (
        f"<!-- local-review-pass:v3 engine={engine} round={round_number} "
        f"base={'b' * 40} head={'a' * 40} result-sha256={'c' * 64} -->"
    )


def test_restart_authorizes_fresh_identity_not_occupied_round(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [{"id": 10, "body": marker("claude", 4)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        engine="claude",
        round=5,
    )
    handoff._authorize_pass(args)
    result = json.loads(capsys.readouterr().out)
    assert result["round"] == 5
    assert result["run_round"] == 1
    args.round = 4
    with pytest.raises(handoff.HandoffError, match="predates"):
        handoff._authorize_pass(args)


def test_restart_still_enforces_cap_and_no_skips(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"id": 10, "body": marker("claude", 4)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40, engine="codex", round=6
    )
    with pytest.raises(handoff.HandoffError, match="skip"):
        handoff._authorize_pass(args)
    rows.append({"id": 21, "body": marker("claude", 5)})
    handoff._authorize_pass(args)
    args.round = 9
    with pytest.raises(handoff.HandoffError, match="exceeds the deep cap"):
        handoff._authorize_pass(args)


def test_first_run_and_duplicate_identity(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    args = SimpleNamespace(
        repo="example/repo", pr=7, base="b" * 40, head="a" * 40, engine="codex", round=1
    )
    handoff._authorize_pass(args)
    assert json.loads(capsys.readouterr().out)["run_round"] == 1
    rows.append({"id": 21, "body": marker("codex", 1)})
    with pytest.raises(handoff.HandoffError, match="already completed"):
        handoff._authorize_pass(args)


def test_offset_includes_changed_attestations_but_not_current_run(
    handoff: ModuleType,
) -> None:
    changed = (
        f"<!-- local-review-complete:v3 engine=claude round=7 "
        f"base={'b' * 40} before={'e' * 40} head={'a' * 40} "
        f"classification=material fingerprints=example result-sha256={'c' * 64} -->"
    )
    assert (
        handoff._round_offset(
            [
                {"id": 10, "body": changed},
                {"id": 21, "body": marker("codex", 8)},
            ],
            20,
        )
        == 7
    )


def test_start_run_returns_first_round(
    handoff: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "authorization.txt"
    authorization.write_text("Explicit review authorization.\n")
    rows = [{"id": 10, "body": marker("codex", 3)}]
    monkeypatch.setattr(handoff, "_issue_comments", lambda *args: rows)
    monkeypatch.setattr(handoff, "_run_records", lambda rows: [])
    monkeypatch.setattr(handoff, "_post_issue_comment", lambda *args: (20, False))
    args = SimpleNamespace(
        repo="example/repo",
        pr=7,
        base="b" * 40,
        head="a" * 40,
        tier="deep",
        restart=False,
        authorization_file=str(authorization),
    )
    handoff._start_run(args)
    assert json.loads(capsys.readouterr().out)["first_round"] == 4
