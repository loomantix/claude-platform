"""Unit tests for `scripts/sync-engine.py`.

Covers the sync-engine hardening invariants:
- `resolve_under` path-traversal escapes (lexical-only check)
- `parse_mode` octal/int/None handling + bool rejection
- `substitute` placeholder warnings + missing-required failure
- `write_if_changed` content + mode divergence
- `prune_empty_parents` walk-up behavior with non-empty stop + ENOENT/ENOTEMPTY tolerance
- Manifest validation (malformed entries, strict-boolean `delete`/`create_if_missing`)
- The delete branch's `exists() or is_symlink()` dangling-link path
- The create_if_missing branch's bootstrap + preserve semantics
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
import yaml


# ---------------------------------------------------------------------------
# resolve_under — lexical traversal check
# ---------------------------------------------------------------------------


def test_resolve_under_accepts_normal_child(sync_engine: ModuleType, tmp_path: Path) -> None:
    result = sync_engine.resolve_under(tmp_path, "a/b/c.txt")
    assert result == tmp_path / "a" / "b" / "c.txt"


def test_resolve_under_rejects_dotdot_escape(sync_engine: ModuleType, tmp_path: Path) -> None:
    assert sync_engine.resolve_under(tmp_path, "../outside") is None
    assert sync_engine.resolve_under(tmp_path, "a/../../outside") is None


def test_resolve_under_rejects_absolute_path(sync_engine: ModuleType, tmp_path: Path) -> None:
    assert sync_engine.resolve_under(tmp_path, "/etc/passwd") is None


def test_resolve_under_rejects_path_collapsing_to_parent(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    # `foo/..` normalizes back to the parent itself — must be rejected.
    assert sync_engine.resolve_under(tmp_path, "foo/..") is None
    assert sync_engine.resolve_under(tmp_path, ".") is None


def test_resolve_under_tolerates_dangling_symlink_at_target(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    """Lexical normalization (not Path.resolve()) means a dangling symlink at
    the destination doesn't break the path-bound check — important for
    delete targets that must clean up broken links.
    """
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nope")
    result = sync_engine.resolve_under(tmp_path, "dangling")
    assert result == dangling


# ---------------------------------------------------------------------------
# parse_mode — octal coercion + type strictness
# ---------------------------------------------------------------------------


def test_parse_mode_none_returns_none(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode(None) is None


def test_parse_mode_int_passthrough(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode(0o755) == 0o755
    assert sync_engine.parse_mode(0o644) == 0o644


def test_parse_mode_octal_string(sync_engine: ModuleType) -> None:
    assert sync_engine.parse_mode("0755") == 0o755
    assert sync_engine.parse_mode("755") == 0o755


def test_parse_mode_rejects_bool(sync_engine: ModuleType) -> None:
    # bool subclasses int in Python; without an explicit guard, `True`
    # would become mode 1 and `False` mode 0.
    with pytest.raises(TypeError, match="bool"):
        sync_engine.parse_mode(True)
    with pytest.raises(TypeError, match="bool"):
        sync_engine.parse_mode(False)


def test_parse_mode_rejects_other_types(sync_engine: ModuleType) -> None:
    with pytest.raises(TypeError):
        sync_engine.parse_mode([0o755])
    with pytest.raises(TypeError):
        sync_engine.parse_mode({"mode": 0o755})


def test_parse_mode_rejects_non_octal_string(sync_engine: ModuleType) -> None:
    with pytest.raises(ValueError):
        sync_engine.parse_mode("9999")  # 9 isn't a valid octal digit


def test_parse_mode_rejects_negative_int(sync_engine: ModuleType) -> None:
    # `Path.chmod(-1)` raises OverflowError mid-loop, partially syncing the
    # consumer tree. Reject at the parse boundary so the sync fails before
    # any write happens.
    with pytest.raises(ValueError, match="out of range"):
        sync_engine.parse_mode(-1)


def test_parse_mode_rejects_oversized_int(sync_engine: ModuleType) -> None:
    # Values above 0o7777 are not valid POSIX file modes; reject rather
    # than silently truncate.
    with pytest.raises(ValueError, match="out of range"):
        sync_engine.parse_mode(0o10000)


# ---------------------------------------------------------------------------
# substitute — placeholder warnings + missing-required failure
# ---------------------------------------------------------------------------


def test_substitute_replaces_declared_placeholder(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "hello <<NAME>>, welcome"
    out = sync_engine.substitute(text, {"NAME": "world"}, ["NAME"], "src.md")
    assert out == "hello world, welcome"
    err = capsys.readouterr().err
    assert err == ""  # clean substitution: no warnings


def test_substitute_warns_on_declared_not_in_source(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    out = sync_engine.substitute("no placeholders", {"NAME": "x"}, ["NAME"], "src.md")
    assert out == "no placeholders"
    err = capsys.readouterr().err
    assert "declared substitutions not found in src.md" in err
    assert "NAME" in err


def test_substitute_warns_on_undeclared_placeholder_left_intact(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "hello <<NAME>>, you are <<ROLE>>"
    out = sync_engine.substitute(text, {"NAME": "world"}, ["NAME"], "src.md")
    # <<ROLE>> is left intact since it's not in the declared list.
    assert "<<ROLE>>" in out
    assert "hello world" in out
    err = capsys.readouterr().err
    assert "placeholders in src.md not declared" in err
    assert "ROLE" in err


def test_substitute_exits_on_missing_required_substitution(
    sync_engine: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        sync_engine.substitute("<<REQUIRED>>", {}, ["REQUIRED"], "src.md")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires placeholders missing from .platform-config.yml" in err


def test_substitute_strips_trailing_newlines_from_block_scalar(
    sync_engine: ModuleType,
) -> None:
    # YAML `|` block scalars carry a trailing \n; the engine strips it so
    # the template's explicit blank line after each placeholder controls
    # inter-section spacing.
    out = sync_engine.substitute(
        "before\n<<KEY>>\nafter",
        {"KEY": "value\n\n"},
        ["KEY"],
        "src.md",
    )
    assert out == "before\nvalue\nafter"


# ---------------------------------------------------------------------------
# write_if_changed — content + mode divergence
# ---------------------------------------------------------------------------


def test_write_if_changed_creates_new_file(sync_engine: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "sub" / "out.txt"
    changed = sync_engine.write_if_changed(target, "hello", None)
    assert changed is True
    assert target.read_text() == "hello"


def test_write_if_changed_noop_on_identical_content(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    mtime_before = target.stat().st_mtime_ns
    changed = sync_engine.write_if_changed(target, "hello", None)
    assert changed is False
    assert target.stat().st_mtime_ns == mtime_before  # no rewrite


def test_write_if_changed_rewrites_on_diverged_content(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    changed = sync_engine.write_if_changed(target, "world", None)
    assert changed is True
    assert target.read_text() == "world"


def test_write_if_changed_applies_mode_when_diverged(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o644)
    changed = sync_engine.write_if_changed(target, "#!/bin/sh\n", 0o755)
    # Content unchanged, mode diverged → still reports changed=True.
    assert changed is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_write_if_changed_compares_full_12bit_mode(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    """Regression lock: `parse_mode` accepts the full POSIX 12-bit range
    (setuid + setgid + sticky + rwx*3 = up to `0o7777`). The mode
    comparison in `write_if_changed` must use `stat.S_IMODE` (12-bit),
    NOT `& 0o777` (9-bit) — otherwise a file with mode `0o4755`
    (setuid + rwxr-xr-x) compared against current `0o755` would always
    appear out-of-sync and the engine would re-chmod on every run.
    """
    target = tmp_path / "setuid.sh"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o4755)  # setuid + rwxr-xr-x
    # Content identical, mode matches at the FULL 12-bit level → no change.
    changed = sync_engine.write_if_changed(target, "#!/bin/sh\n", 0o4755)
    assert changed is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o4755


def test_write_if_changed_leaves_mode_when_none(sync_engine: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello")
    target.chmod(0o600)
    sync_engine.write_if_changed(target, "world", None)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # mode untouched


# ---------------------------------------------------------------------------
# prune_empty_parents — walk-up with non-empty stop + ENOENT tolerance
# ---------------------------------------------------------------------------


def test_prune_empty_parents_removes_empty_chain(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    f = nested / "file.txt"
    f.write_text("x")
    f.unlink()  # simulate sync-engine's unlink
    sync_engine.prune_empty_parents(f, tmp_path)
    assert not (tmp_path / "a").exists()


def test_prune_empty_parents_stops_at_non_empty(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    sibling = tmp_path / "a" / "sibling.txt"
    sibling.write_text("keep me")
    f = tmp_path / "a" / "b" / "deleted.txt"
    f.write_text("x")
    f.unlink()
    sync_engine.prune_empty_parents(f, tmp_path)
    # `b` was empty so it's gone; `a` had a sibling so it's preserved.
    assert not (tmp_path / "a" / "b").exists()
    assert (tmp_path / "a").exists()
    assert sibling.exists()


def test_prune_empty_parents_does_not_remove_root(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    f.unlink()
    sync_engine.prune_empty_parents(f, tmp_path)
    assert tmp_path.exists()  # root is the stop condition; never removed


def test_prune_empty_parents_tolerates_concurrent_remove(
    sync_engine: ModuleType, tmp_path: Path
) -> None:
    # Simulate the file's parent dir already being gone (concurrent cleanup).
    nested = tmp_path / "a" / "b"
    f = nested / "ghost.txt"
    # No mkdir — `f.parent` doesn't exist. prune_empty_parents must not raise.
    sync_engine.prune_empty_parents(f, tmp_path)


# ---------------------------------------------------------------------------
# End-to-end main() invocation via direct call
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _run_main(
    sync_engine: ModuleType,
    upstream: Path,
    consumer: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool = False,
) -> int:
    argv = [
        "sync-engine.py",
        "--upstream-repo",
        str(upstream),
        "--consumer-dir",
        str(consumer),
    ]
    if dry_run:
        argv.append("--dry-run")
    monkeypatch.setattr("sys.argv", argv)
    return int(sync_engine.main())


def test_main_copy_target_writes_substituted_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("hello <<NAME>>\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md", "substitutions": ["NAME"]}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"substitutions": {"NAME": "world"}})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "dest.md").read_text() == "hello world\n"


def test_main_delete_target_unlinks_real_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (consumer_dir / "stale.md").write_text("retired content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "stale.md", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not (consumer_dir / "stale.md").exists()


def test_main_delete_target_unlinks_dangling_symlink(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `exists()` alone returns False on a dangling link; the engine pairs
    # it with `is_symlink()` so retired symlinks still get cleaned up.
    dangling = consumer_dir / "dangling"
    dangling.symlink_to(consumer_dir / "absent-target")
    assert dangling.is_symlink()
    assert not dangling.exists()  # confirm it's dangling

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "dangling", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not dangling.is_symlink()


def test_main_delete_refuses_directory(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (consumer_dir / "subdir").mkdir()
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "subdir", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination is a directory" in err


def test_main_delete_is_idempotent_when_already_absent(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "never-existed.md", "delete": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already absent" in out


def test_main_rejects_stringly_typed_delete_flag(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `delete: "true"` would be truthy in Python — must hard-fail.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "x.md", "delete": "true"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`delete` must be a boolean" in err


def test_main_rejects_stringly_typed_create_if_missing(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": "dest.md", "create_if_missing": "true"}
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`create_if_missing` must be a boolean" in err


def test_main_rejects_delete_and_create_if_missing_together(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "x.md", "delete": True, "create_if_missing": True}
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_main_rejects_bare_scalar_target(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": ["just a string, not a mapping"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "malformed target entry" in err


def test_main_rejects_dot_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "."}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1


def test_main_rejects_destination_escaping_consumer_root(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "../escape.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination escapes" in err


def test_main_rejects_source_escaping_upstream_root(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "../etc/passwd", "destination": "x.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "source escapes upstream repo" in err


def test_main_rejects_mode_on_delete_target(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "x.md", "delete": True, "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`mode` is not valid on a delete target" in err


def test_main_skip_targets_by_source(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("x")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md"}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"skip_targets": ["src.md"]})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert not (consumer_dir / "dest.md").exists()
    assert "skip" in capsys.readouterr().out


def test_main_skip_delete_target_by_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A consumer that wants to OPT OUT of a retirement (keep the
    # upstream-flagged-for-deletion file) puts the destination into
    # `skip_targets`. The file must stay on disk and `skipped` counts up.
    (consumer_dir / "kept.md").write_text("consumer wants to keep this\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "kept.md", "delete": True}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"skip_targets": ["kept.md"]})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "kept.md").read_text() == "consumer wants to keep this\n"
    out = capsys.readouterr().out
    assert "skip kept.md" in out


def test_main_create_if_missing_bootstraps_first_time(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("initial content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / "out.md").read_text() == "initial content"


def test_main_create_if_missing_preserves_consumer_edits(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("upstream content")
    (consumer_dir / "out.md").write_text("CONSUMER EDIT")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    # Consumer's edit must survive — that's the whole point of create_if_missing.
    assert (consumer_dir / "out.md").read_text() == "CONSUMER EDIT"
    assert "preserved" in capsys.readouterr().out


def test_main_create_if_missing_preserves_dangling_symlink(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dangling symlink counts as "present" for create_if_missing, just
    # like in the delete branch — symmetry between the two boolean branches.
    (upstream_repo / "src.md").write_text("upstream")
    dangling = consumer_dir / "out.md"
    dangling.symlink_to(consumer_dir / "absent")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert dangling.is_symlink()  # untouched


def test_main_create_if_missing_refuses_directory(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("upstream")
    (consumer_dir / "out.md").mkdir()
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "out.md", "create_if_missing": True}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination is a directory" in err


def test_main_missing_required_substitution_exits_1(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "src.md").write_text("hello <<NAME>>")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md", "substitutions": ["NAME"]}]},
    )

    # substitute() calls sys.exit(1) on missing required — that bubbles up
    # through main().
    with pytest.raises(SystemExit) as exc:
        _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert exc.value.code == 1


def test_main_applies_mode_to_copied_file(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "script.sh").write_text("#!/bin/sh\necho hi\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "script.sh", "destination": "out.sh", "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert stat.S_IMODE((consumer_dir / "out.sh").stat().st_mode) == 0o755


def test_main_dry_run_does_not_write(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "src.md").write_text("hello")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": "dest.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, dry_run=True)
    assert rc == 0
    assert not (consumer_dir / "dest.md").exists()
    out = capsys.readouterr().out
    assert "would write dest.md" in out


def test_main_dry_run_reports_mode_only_diff(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (upstream_repo / "script.sh").write_text("#!/bin/sh\n")
    (consumer_dir / "out.sh").write_text("#!/bin/sh\n")
    # Initial mode 0o600 (owner-only) so this test fixture stays under
    # CodeQL's overly-permissive-mode rule. The test's invariant is
    # mode-divergence detection — any initial mode that differs from
    # the target's 0o755 manifest entry exercises the dry-run reporter.
    os.chmod(consumer_dir / "out.sh", 0o600)
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "script.sh", "destination": "out.sh", "mode": "0755"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write out.sh (mode)" in out
    assert stat.S_IMODE((consumer_dir / "out.sh").stat().st_mode) == 0o600  # not actually changed


def test_main_missing_source_file_returns_1(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "missing.md", "destination": "dest.md"}]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "source missing in upstream" in err


def test_main_rejects_top_level_targets_not_a_list(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": {"src.md": "dest.md"}},  # mapping, not list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`targets` must be a list" in err


# ---------------------------------------------------------------------------
# glob_to_regex + path_matches_any — pattern matcher semantics
# ---------------------------------------------------------------------------


def test_glob_to_regex_literal_path(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".github/copilot-instructions.md")
    assert pat.match(".github/copilot-instructions.md")
    assert not pat.match(".github/copilot-instructions.md.template")
    assert not pat.match("prefix/.github/copilot-instructions.md")


def test_glob_to_regex_single_star_does_not_cross_slash(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/*")
    assert pat.match(".claude/skills/grill")
    # `*` must NOT match across `/` segments — otherwise an allowlist of
    # `.claude/skills/*` would cover `.claude/skills/grill/SKILL.md` too.
    assert not pat.match(".claude/skills/grill/SKILL.md")


def test_glob_to_regex_double_star_crosses_slashes(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/**")
    assert pat.match(".claude/skills/grill")
    assert pat.match(".claude/skills/grill/SKILL.md")
    assert pat.match(".claude/skills/grill/scripts/run.sh")
    # Must not bleed past the prefix.
    assert not pat.match(".claude/agents/foo.md")


def test_glob_to_regex_double_star_in_middle(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".claude/skills/**/SKILL.md")
    assert pat.match(".claude/skills/grill/SKILL.md")
    assert pat.match(".claude/skills/issues/scripts/SKILL.md")
    # `**` matches zero segments too — direct child should match.
    assert pat.match(".claude/skills/SKILL.md")
    assert not pat.match(".claude/skills/grill/run.sh")


def test_glob_to_regex_question_mark(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex("Dockerfile.?")
    assert pat.match("Dockerfile.a")
    assert pat.match("Dockerfile.1")
    assert not pat.match("Dockerfile.ab")
    assert not pat.match("Dockerfile.")  # `?` requires exactly one char


def test_glob_to_regex_anchored_at_both_ends(sync_engine: ModuleType) -> None:
    pat = sync_engine.glob_to_regex(".github/workflows/sync.yml")
    # Must not match suffix or prefix injection.
    assert not pat.match("foo/.github/workflows/sync.yml")
    assert not pat.match(".github/workflows/sync.yml.bak")


def test_glob_to_regex_escapes_regex_metachars(sync_engine: ModuleType) -> None:
    # The pattern contains `.` (regex any-char) and `+` (regex quantifier).
    # Both must be treated as literal.
    pat = sync_engine.glob_to_regex("a.b+c")
    assert pat.match("a.b+c")
    assert not pat.match("axbc")  # `.` literal, not any-char
    assert not pat.match("a.bbc")  # `+` literal, not quantifier


def test_path_matches_any_empty_list_returns_false(sync_engine: ModuleType) -> None:
    assert sync_engine.path_matches_any("any/path.md", []) is False


def test_path_matches_any_matches_on_any_pattern(sync_engine: ModuleType) -> None:
    patterns = [
        sync_engine.glob_to_regex(".claude/**"),
        sync_engine.glob_to_regex(".codex/**"),
    ]
    assert sync_engine.path_matches_any(".claude/skills/grill/SKILL.md", patterns)
    assert sync_engine.path_matches_any(".codex/skills/grill/SKILL.md", patterns)
    assert not sync_engine.path_matches_any(".github/workflows/release.yml", patterns)


# ---------------------------------------------------------------------------
# allowed_destinations + SENSITIVE_DELETE_PATTERNS — main() enforcement
# ---------------------------------------------------------------------------


def test_main_allowlist_match_permits_write(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (upstream_repo / "skill.md").write_text("skill content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "skill.md", "destination": ".claude/skills/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "skill content\n"


def test_main_allowlist_refuses_out_of_list_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The red-team scenario: an upstream-authored manifest tries to overwrite
    # the consumer's release workflow. The allowlist (consumer-side opt-in)
    # refuses before any filesystem change.
    (upstream_repo / "template.md").write_text("malicious payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "template.md",
                    "destination": ".github/workflows/release.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", ".github/copilot-instructions.md"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert ".github/workflows/release.yml" in err
    assert not (consumer_dir / ".github" / "workflows" / "release.yml").exists()


def test_main_allowlist_absent_warns_and_proceeds_migration(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Migration semantics: when `allowed_destinations` is absent from the
    # consumer config, the engine warns but does NOT refuse — otherwise
    # every consumer's first post-deployment sync would break before they
    # had a chance to ship their allowlist. The warning is the signal for
    # the consumer to add the field.
    (upstream_repo / "src.md").write_text("content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/skills/foo.md"}]},
    )
    # Default consumer_dir fixture has empty .platform-config.yml (no
    # `allowed_destinations` key) — exactly the pre-migration state.

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "content\n"
    err = capsys.readouterr().err
    assert "`allowed_destinations` not set" in err
    assert "fail-closed" in err  # migration pointer


def test_main_empty_allowlist_refuses_everything(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An empty list is a real value, not the same as the key being absent.
    # It expresses "this consumer is locked — refuse any upstream write."
    (upstream_repo / "src.md").write_text("content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/foo.md"}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {"allowed_destinations": []})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err


def test_main_allowlist_rejects_non_list_type(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ".claude/**"},  # string, not list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`allowed_destinations` must be a list of strings" in err


def test_main_allowlist_rejects_non_string_element(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", 42]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`allowed_destinations` must be a list of strings" in err


def test_main_sensitive_delete_refused_even_when_allowlisted(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The red-team scenario: a consumer legitimately syncs CI workflows
    # (so `.github/workflows/**` IS in their allowlist), but the engine
    # must still refuse to delete one — the allowlist permits writes, the
    # engine-level constant prohibits delete entries against guardrails.
    workflow_path = consumer_dir / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: CI\non: push\n")

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": ".github/workflows/ci.yml", "delete": True}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert workflow_path.exists()  # untouched


def test_main_sensitive_copy_still_allowed_when_in_allowlist(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sensitive-path block applies to `delete: true` only. Copying a
    # workflow file into `.github/workflows/` from upstream is a normal
    # sync operation (every consumer syncs their CI workflows this way).
    (upstream_repo / "ci.yml.template").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "ci.yml.template",
                    "destination": ".github/workflows/ci.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".github" / "workflows" / "ci.yml").read_text() == "name: CI\non: push\n"


def test_main_sensitive_delete_lockfiles_and_dockerfile(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Coverage for the non-workflow entries in SENSITIVE_DELETE_PATTERNS:
    # package.json, pnpm-lock.yaml, prisma/schema.prisma, Dockerfile.
    for path in ("package.json", "pnpm-lock.yaml", "Dockerfile"):
        (consumer_dir / path).write_text("placeholder")

    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "package.json", "delete": True},
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["package.json", "pnpm-lock.yaml", "Dockerfile"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert "package.json" in err
    assert (consumer_dir / "package.json").exists()


def test_main_allowlist_applies_to_delete_target_too(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Deletion targets a path the consumer never allowlisted — refuse via
    # the allowlist check (NOT the sensitive-path check, since `.docs/old.md`
    # isn't in SENSITIVE_DELETE_PATTERNS). This proves the allowlist gates
    # both writes AND deletes uniformly.
    (consumer_dir / ".docs").mkdir()
    (consumer_dir / ".docs" / "old.md").write_text("dead content")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": ".docs/old.md", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},  # .docs not in list
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert (consumer_dir / ".docs" / "old.md").exists()


def test_main_allowlist_applies_to_create_if_missing_target_too(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # create_if_missing still writes the file on first sync — the allowlist
    # must gate it too. Otherwise a manifest entry like
    # `{source: prompt.txt.template, destination: .env, create_if_missing: true}`
    # would bootstrap a destination path the consumer never opted in to.
    (upstream_repo / "tmpl.txt").write_text("bootstrap\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "tmpl.txt",
                    "destination": ".env",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert not (consumer_dir / ".env").exists()


def test_main_allowlist_dual_prefix_for_dual_upstream_consumer(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Consumers that subscribe to both claude-platform and codex-platform
    # need both prefixes in their allowlist. A single sync run still hits
    # one upstream at a time; this test exercises the dual-prefix
    # allowlist's behavior against a claude-style target.
    (upstream_repo / "src.md").write_text("claude content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo.md"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**", ".codex/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").exists()


def test_main_allowlist_checked_before_source_read(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Defense-in-depth: even if `source` is missing in upstream, the
    # allowlist check fires first so the operator gets a policy-violation
    # message instead of a confusing "source missing in upstream" — the
    # latter would suggest the right fix is to add the file rather than to
    # refuse the manifest entry.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "never-existed.md",
                    "destination": ".github/workflows/release.yml",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
    assert "source missing in upstream" not in err


# ---------------------------------------------------------------------------
# Adversarial path forms — destinations that exploit normalization seams
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_destination",
    [
        "./.github/workflows/release.yml",
        ".github/./workflows/release.yml",
        ".github//workflows/release.yml",
        "foo/../.github/workflows/release.yml",
        ".github/workflows/../workflows/release.yml",
    ],
)
def test_main_refuses_non_canonical_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_destination: str,
) -> None:
    # `resolve_under` collapses `./`, `//`, and `foo/../` so the on-disk
    # write target is canonical — but allowlist + sensitive-delete patterns
    # match the manifest's `destination` string. A non-canonical destination
    # would otherwise resolve to a guarded path on disk while bypassing the
    # anchored pattern matchers. The engine refuses outright.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": bad_destination}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "canonical posix form" in err
    assert not (consumer_dir / ".github" / "workflows" / "release.yml").exists()


def test_main_refuses_non_canonical_delete_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The delete variant of the same bypass attack: an allowlist that
    # legitimately covers `.github/workflows/**` still must not allow
    # `./.github/workflows/ci.yml` to slip past the sensitive-delete
    # check via its raw-string mismatch.
    workflow = consumer_dir / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"destination": "./.github/workflows/ci.yml", "delete": True}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "canonical posix form" in err
    assert workflow.exists()


def test_main_refuses_control_char_in_destination(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `[^/]*` in the glob compiler matches newlines, so an allowlist of
    # `.claude/skills/*` would otherwise accept `.claude/skills/foo\nbar`
    # as a valid destination — a file that sync-diff review by eye
    # could easily miss.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo\nbar"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".claude/skills/*"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be a non-empty printable path string" in err


def test_main_sensitive_delete_case_insensitive(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # On case-insensitive filesystems (macOS APFS, NTFS), `dockerfile`
    # resolves to the same on-disk file as `Dockerfile`. The sensitive-
    # delete regexes are compiled with `re.IGNORECASE` so the lowercase
    # spelling is refused too, even though fleet sync runs on case-
    # sensitive Linux today (defense-in-depth for self-hosted runners).
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": "dockerfile", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": ["dockerfile", "Dockerfile"]},
    )
    (consumer_dir / "dockerfile").write_text("FROM scratch\n")

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err


def test_main_sensitive_delete_blocks_github_actions(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `.github/actions/**` was added to SENSITIVE_DELETE_PATTERNS so a
    # manifest entry can't remove a composite action that a still-extant
    # workflow depends on — equivalent to removing the workflow itself.
    action_path = consumer_dir / ".github" / "actions" / "build" / "action.yml"
    action_path.parent.mkdir(parents=True)
    action_path.write_text("name: build\nruns: { using: composite }\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "destination": ".github/actions/build/action.yml",
                    "delete": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/actions/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err
    assert action_path.exists()


def test_main_sensitive_delete_blocks_codeowners(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # CODEOWNERS deletion bypasses required-reviewer gates; treat it as
    # a guardrail equivalent to a CI workflow.
    (consumer_dir / ".github").mkdir()
    (consumer_dir / ".github" / "CODEOWNERS").write_text("* @platform\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"destination": ".github/CODEOWNERS", "delete": True}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/CODEOWNERS"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to delete sensitive path" in err


# ---------------------------------------------------------------------------
# Fail-open semantics — distinguishing missing key from null value
# ---------------------------------------------------------------------------


def test_main_allowlist_null_value_is_config_error(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `allowed_destinations:` with no value parses to None. That's almost
    # certainly a mid-edit accident — the consumer thinks they've turned
    # on the gate, but the engine would silently treat it as fail-open.
    # Hard-fail so the operator sees the problem rather than discovering
    # weeks later that their allowlist was never enforced.
    (upstream_repo / "src.md").write_text("payload\n")
    (upstream_repo / "scripts" / "sync-targets.yml").write_text(
        "targets:\n  - source: src.md\n    destination: .claude/foo.md\n"
    )
    (consumer_dir / ".platform-config.yml").write_text("allowed_destinations:\n")

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "present but null" in err


def test_main_fail_open_warning_uses_github_annotation(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Fail-open warning must surface in the GitHub PR UI via the
    # `::warning::` annotation prefix — otherwise a green-checkmark
    # build buries the migration prompt in stderr where nobody looks.
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml", {"targets": []}
    )
    # Consumer .platform-config.yml has no `allowed_destinations` key.

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    err = capsys.readouterr().err
    assert "::warning" in err
    assert "allowed_destinations" in err


# ---------------------------------------------------------------------------
# Skip + allowlist coexistence and OR-semantics across patterns
# ---------------------------------------------------------------------------


def test_main_skip_target_short_circuits_allowlist(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Consumer opt-out via `skip_targets` fires BEFORE the allowlist
    # check — a skipped target's destination need not be in
    # `allowed_destinations`. This protects consumers that locally
    # diverge from an upstream-managed file (e.g., the agent-loop
    # skill on platform) without forcing them to allowlist a path
    # they've explicitly opted out of.
    (upstream_repo / "skill.md").write_text("upstream content\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "skill.md",
                    "destination": ".claude/skills/local/SKILL.md",
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "skip_targets": [".claude/skills/local/SKILL.md"],
            "allowed_destinations": [".claude/skills/permitted/**"],
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "skip" in out.lower()
    assert not (consumer_dir / ".claude" / "skills" / "local" / "SKILL.md").exists()


def test_main_allowlist_matches_second_pattern_in_list(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Allowlist semantics are OR (any pattern matches). Cover the
    # second-pattern-only case end-to-end through enforcement, in case a
    # future refactor accidentally turns the iteration into AND or only
    # consults the first pattern.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {"source": "src.md", "destination": ".claude/skills/foo.md"}
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {
            "allowed_destinations": [
                ".github/copilot-instructions.md",  # doesn't match
                ".claude/**",  # matches
            ]
        },
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".claude" / "skills" / "foo.md").read_text() == "payload\n"


def test_main_create_if_missing_sensitive_path_allowed_for_copy(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sensitive-path block applies to `delete: true` only. A
    # `create_if_missing` bootstrap of `.github/workflows/ci.yml` (which
    # IS a sensitive path) must succeed on the first sync — otherwise
    # net-new consumers couldn't onboard their CI workflow via sync.
    (upstream_repo / "ci.yml.template").write_text("name: CI\non: push\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {
            "targets": [
                {
                    "source": "ci.yml.template",
                    "destination": ".github/workflows/ci.yml",
                    "create_if_missing": True,
                }
            ]
        },
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [".github/workflows/**"]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 0
    assert (consumer_dir / ".github" / "workflows" / "ci.yml").read_text() == "name: CI\non: push\n"


def test_main_allowlist_empty_string_pattern_matches_only_empty(
    sync_engine: ModuleType,
    upstream_repo: Path,
    consumer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Empty-string allowlist entry compiles to `\A\Z` (matches only the
    # empty string). It never matches any real destination — refuses
    # everything. Pin this so a future maintainer who "fixes" the
    # empty-prefix edge case with a `pattern or ".*"` fallback doesn't
    # silently convert empty entries into wildcards.
    (upstream_repo / "src.md").write_text("payload\n")
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "src.md", "destination": ".claude/foo.md"}]},
    )
    _write_yaml(
        consumer_dir / ".platform-config.yml",
        {"allowed_destinations": [""]},
    )

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "destination not in consumer's `allowed_destinations`" in err
