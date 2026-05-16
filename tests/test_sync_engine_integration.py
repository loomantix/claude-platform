"""Dry-run snapshot tests for `scripts/sync-engine.py`.

Runs the engine as a subprocess (the same shape the consumer sync
workflow uses), captures stdout, and asserts on the plan it printed.
A change to the plan format here is a breaking change for consumers
that parse it — those consumers will need a coordinated sync-v1 bump.

These tests complement `test_sync_engine.py` (which directly imports
the engine for fast unit-level coverage) by validating the full
argv → exit-code → stdout/stderr surface that production drives.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_ENGINE = REPO_ROOT / "scripts" / "sync-engine.py"


def _write_yaml(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _run_engine(
    upstream: Path,
    consumer: Path,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(SYNC_ENGINE),
        "--upstream-repo", str(upstream),
        "--consumer-dir", str(consumer),
    ]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, capture_output=True, text=True)


def _setup_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A canonical fixture exercising copy, delete, create_if_missing, mode, skip."""
    upstream = tmp_path / "upstream"
    consumer = tmp_path / "consumer"
    (upstream / "scripts").mkdir(parents=True)
    consumer.mkdir()

    (upstream / "agents" / "foo.md").parent.mkdir()
    (upstream / "agents" / "foo.md").write_text("agent foo for <<NAME>>\n")

    (upstream / "skills" / "bar.md").parent.mkdir()
    (upstream / "skills" / "bar.md").write_text("skill bar\n")

    (upstream / "templates" / "boot.md.template").parent.mkdir()
    (upstream / "templates" / "boot.md.template").write_text("bootstrap content\n")

    (upstream / "script.sh").write_text("#!/bin/sh\necho ok\n")

    _write_yaml(
        upstream / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "agents/foo.md",
                    "destination": "agents/foo.md",
                    "substitutions": ["NAME"],
                },
                {"source": "skills/bar.md", "destination": "skills/bar.md"},
                {
                    "source": "templates/boot.md.template",
                    "destination": "boot.md",
                    "create_if_missing": True,
                },
                {"source": "script.sh", "destination": "script.sh", "mode": "0755"},
                {"destination": "retired.md", "delete": True},
                {"source": "skills/bar.md", "destination": "skipped.md"},
            ]
        },
    )

    # Pre-existing state in the consumer:
    # - retired.md exists (delete branch will remove it)
    # - boot.md does NOT exist (create_if_missing branch will bootstrap)
    # - other targets don't yet exist (will be written)
    (consumer / "retired.md").write_text("stale\n")

    _write_yaml(
        consumer / ".platform-config.yml",
        {
            "substitutions": {"NAME": "alice"},
            "skip_targets": ["skipped.md"],
        },
    )
    return upstream, consumer


def test_dry_run_does_not_modify_consumer(tmp_path: Path) -> None:
    upstream, consumer = _setup_fixture(tmp_path)
    snapshot_before = sorted(p.name for p in consumer.iterdir())

    result = _run_engine(upstream, consumer, dry_run=True)
    assert result.returncode == 0, f"stderr={result.stderr}"

    snapshot_after = sorted(p.name for p in consumer.iterdir())
    assert snapshot_before == snapshot_after  # nothing added or removed
    # retired.md is still there (dry-run shouldn't unlink it)
    assert (consumer / "retired.md").read_text() == "stale\n"


def test_dry_run_reports_each_planned_action(tmp_path: Path) -> None:
    upstream, consumer = _setup_fixture(tmp_path)
    result = _run_engine(upstream, consumer, dry_run=True)
    assert result.returncode == 0

    out = result.stdout
    # Each non-skipped target must surface in the plan with a recognizable
    # verb. The consumer sync workflow gates on the printed counts (see
    # sync-from-upstream.yml.template), so the contract is: counts must
    # match what was planned.
    assert "(dry run — no files will be written)" in out
    assert "would write agents/foo.md" in out
    assert "would write skills/bar.md" in out
    assert "would write boot.md" in out  # create_if_missing first-sync
    assert "would write script.sh" in out
    assert "would remove retired.md" in out
    assert "skip skills/bar.md" in out  # skip_targets matches source
    # Summary line shape:
    assert "Done:" in out
    assert "written" in out and "removed" in out and "unchanged" in out and "skipped" in out


def test_real_run_applies_all_planned_actions(tmp_path: Path) -> None:
    upstream, consumer = _setup_fixture(tmp_path)

    result = _run_engine(upstream, consumer, dry_run=False)
    assert result.returncode == 0, f"stderr={result.stderr}"

    # Substitution applied
    assert (consumer / "agents" / "foo.md").read_text() == "agent foo for alice\n"
    # Verbatim copy
    assert (consumer / "skills" / "bar.md").read_text() == "skill bar\n"
    # Bootstrap created
    assert (consumer / "boot.md").read_text() == "bootstrap content\n"
    # Mode applied
    import stat
    assert stat.S_IMODE((consumer / "script.sh").stat().st_mode) == 0o755
    # Retired file removed
    assert not (consumer / "retired.md").exists()
    # Skipped source did NOT write to its destination
    assert not (consumer / "skipped.md").exists()


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    """After the first sync, a second invocation should report all unchanged.

    Catches a regression where write_if_changed loses idempotence (e.g.,
    forgetting the existing == content short-circuit), which would cause
    every sync to churn the consumer's working tree and flood PRs with
    no-op commits.
    """
    upstream, consumer = _setup_fixture(tmp_path)
    first = _run_engine(upstream, consumer)
    assert first.returncode == 0

    second = _run_engine(upstream, consumer)
    assert second.returncode == 0
    # Every non-skipped, non-deleted target should be "unchanged" the
    # second time around. The delete branch reports "already absent"
    # which also counts as unchanged in the summary.
    out = second.stdout
    assert "0 written" in out
    # Skipped count must match the original skip in the fixture.
    assert "1 skipped" in out


def test_create_if_missing_preserves_consumer_edits_across_syncs(tmp_path: Path) -> None:
    """The bootstrap-then-leave-alone contract for `create_if_missing` is the
    sole reason the agent-loop instructions template exists (consumers
    customize their TODO list after the first sync). Lock the contract.
    """
    upstream, consumer = _setup_fixture(tmp_path)
    _run_engine(upstream, consumer)
    # Consumer edits the bootstrapped file.
    (consumer / "boot.md").write_text("CONSUMER CUSTOMIZATION\n")
    # Upstream template content changes — must NOT propagate.
    (upstream / "templates" / "boot.md.template").write_text("upstream changed\n")
    result = _run_engine(upstream, consumer)
    assert result.returncode == 0
    assert (consumer / "boot.md").read_text() == "CONSUMER CUSTOMIZATION\n"
    assert "preserved boot.md (create_if_missing)" in result.stdout


def test_engine_exits_2_on_missing_config(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    consumer = tmp_path / "consumer"
    (upstream / "scripts").mkdir(parents=True)
    consumer.mkdir()
    _write_yaml(upstream / "scripts" / "sync-targets.yml", {"targets": []})
    # No .platform-config.yml in consumer.

    result = _run_engine(upstream, consumer)
    assert result.returncode == 2
    assert "missing required file" in result.stderr


def test_engine_exits_2_on_missing_targets_file(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    consumer = tmp_path / "consumer"
    (upstream / "scripts").mkdir(parents=True)
    consumer.mkdir()
    _write_yaml(consumer / ".platform-config.yml", {})
    # No sync-targets.yml in upstream.

    result = _run_engine(upstream, consumer)
    assert result.returncode == 2
    assert "missing required file" in result.stderr
