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
    extra_args: list[str] | None = None,
) -> int:
    argv = [
        "sync-engine.py",
        "--upstream-repo",
        str(upstream),
        "--consumer-dir",
        str(consumer),
    ]
    if extra_args:
        argv.extend(extra_args)
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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})  # no `substitutions` block

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, ["--dry-run"])
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
    os.chmod(consumer_dir / "out.sh", 0o644)
    _write_yaml(
        upstream_repo / "scripts" / "sync-targets.yml",
        {"targets": [{"source": "script.sh", "destination": "out.sh", "mode": "0755"}]},
    )
    _write_yaml(consumer_dir / ".platform-config.yml", {})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch, ["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write out.sh (mode)" in out
    assert stat.S_IMODE((consumer_dir / "out.sh").stat().st_mode) == 0o644  # not actually changed


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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

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
    _write_yaml(consumer_dir / ".platform-config.yml", {})

    rc = _run_main(sync_engine, upstream_repo, consumer_dir, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "`targets` must be a list" in err
