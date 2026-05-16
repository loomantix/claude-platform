"""Unit tests for `scripts/create-signed-commit.py`.

Network-bound code paths (`github_api`, `github_api_optional`, ref
creation) are not tested here — they require a real installation token
and a live GitHub repo. Coverage focuses on:

- `parse_status` against a real git working tree (the porcelain-v1
  format with `-z` is the contract; we drive it through actual `git
  status` invocations rather than mocked output, so any future git
  output drift is caught here).
- `derive_signoff_trailer` slug formatting.
- `with_signoff` idempotency.
- The `--new-branch == --base-branch` self-merge guard in `main()`.
"""
from __future__ import annotations

import os
import subprocess
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest


def _http_error(path: str, code: int, msg: str) -> urllib.error.HTTPError:
    """Build a synthetic GitHub-API HTTPError for tests.

    `hdrs=None` is documented-accepted at runtime but typed as
    `email.message.Message` (not `Message | None`) in typeshed —
    a stdlib stub gap. Scoping the `type: ignore` to this builder keeps
    the four call sites clean.
    """
    return urllib.error.HTTPError(
        url="https://api.github.com" + path,
        code=code,
        msg=msg,
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# ---------------------------------------------------------------------------
# Helpers: drive parse_status through a real git working tree
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """Run a git command in `cwd` and return stdout (raises on failure)."""
    env = os.environ.copy()
    # Force a deterministic identity for commits — the tests don't rely on
    # `git config` being set on the host runner.
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# parse_status
# ---------------------------------------------------------------------------


def test_parse_status_empty_tree(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == []
    assert changes.deletes == []


def test_parse_status_modified_file(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").write_text("modified\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == ["seed.txt"]
    assert changes.deletes == []


def test_parse_status_new_untracked_file_via_uall(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # Without -uall, a new untracked file inside a new untracked dir would
    # be reported as `?? newdir/` (single entry) and the engine would try
    # to read a directory. With -uall (which the engine uses), each file
    # is reported individually.
    nested = git_repo / "newdir" / "sub"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("a\n")
    (nested / "b.txt").write_text("b\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert sorted(changes.upserts) == ["newdir/sub/a.txt", "newdir/sub/b.txt"]
    assert changes.deletes == []


def test_parse_status_deleted_file(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").unlink()
    changes = create_signed_commit.parse_status(git_repo)
    assert changes.upserts == []
    assert changes.deletes == ["seed.txt"]


def test_parse_status_rename_emits_both_upsert_and_delete(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # Set up a tracked file, then rename it via the index so git status
    # reports `R` rather than `D` + `??`. Pure renames need the OLD path
    # marked deleted — without that, the API's base_tree would preserve
    # the old file, turning the rename into a copy.
    (git_repo / "old.txt").write_text("content\n")
    _git("add", "old.txt", cwd=git_repo)
    _git("commit", "-q", "-m", "add", cwd=git_repo)
    _git("mv", "old.txt", "new.txt", cwd=git_repo)
    changes = create_signed_commit.parse_status(git_repo)
    assert "new.txt" in changes.upserts
    assert "old.txt" in changes.deletes


def test_parse_status_handles_paths_with_spaces(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    # `-z` output is NUL-separated and never quotes — verifies the parser
    # doesn't trip on whitespace inside paths.
    spaced = git_repo / "file with spaces.txt"
    spaced.write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "file with spaces.txt" in changes.upserts


def test_parse_status_handles_path_with_special_chars(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    weird = git_repo / "file'with\"quotes.txt"
    weird.write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "file'with\"quotes.txt" in changes.upserts


def test_parse_status_mixed_upsert_and_delete(
    create_signed_commit: ModuleType, git_repo: Path
) -> None:
    (git_repo / "seed.txt").unlink()
    (git_repo / "new.txt").write_text("x\n")
    changes = create_signed_commit.parse_status(git_repo)
    assert "new.txt" in changes.upserts
    assert "seed.txt" in changes.deletes


def test_parse_status_d_entry_trusts_git_code_not_disk_state(
    create_signed_commit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression lock for the inverted-classification bug fixed upstream:
    a `D` entry must be classified as a delete based on the git status
    code alone, NOT by re-checking `.exists()` on disk. The old code
    introduced a TOCTOU window where a concurrent re-create of the path
    between `git status` and the disk check caused the delete to be
    misclassified as an upsert — re-uploading the file to the tree
    instead of removing it.

    The cleanest test of the contract is to mock `run` so we control
    exactly what porcelain output the parser sees, and assert that even
    when the file exists on disk, a `D` code routes to deletes.
    """
    # Real-disk state: the file IS present (the TOCTOU we're guarding
    # against — git said delete, but disk says present).
    (tmp_path / "ghost.txt").write_text("oops still here\n")

    # Mock `run` so the parser sees a synthetic `D` entry regardless of
    # disk state. NUL-separated porcelain v1 with -z.
    def fake_run(*args: str, cwd: Path | None = None) -> str:
        return "D  ghost.txt\0"

    monkeypatch.setattr(create_signed_commit, "run", fake_run)
    changes = create_signed_commit.parse_status(tmp_path)
    assert changes.deletes == ["ghost.txt"]
    assert changes.upserts == []


# ---------------------------------------------------------------------------
# derive_signoff_trailer + with_signoff
# ---------------------------------------------------------------------------


def test_derive_signoff_trailer_uses_bot_suffix(create_signed_commit: ModuleType) -> None:
    out = create_signed_commit.derive_signoff_trailer("loomantix")
    assert out == "Signed-off-by: loomantix[bot] <loomantix[bot]@users.noreply.github.com>"


def test_derive_signoff_trailer_empty_slug_documents_current_behavior(
    create_signed_commit: ModuleType,
) -> None:
    """Pinning current behavior: an empty `--app-slug` produces a
    `[bot] <[bot]@users.noreply.github.com>` trailer. The DCO regex
    accepts it (`.+ <.+@.+>`) but it's an obvious misconfiguration.
    The argparse default is `None` (no trailer); empty-string is only
    reachable from a misconfigured workflow input.

    Recording the behavior as-is rather than tightening the validator
    here — the upstream `actions/create-github-app-token` output is
    never empty in practice, so this is a contract-pinning test
    rather than a hardening one.
    """
    out = create_signed_commit.derive_signoff_trailer("")
    assert out == "Signed-off-by: [bot] <[bot]@users.noreply.github.com>"


def test_with_signoff_appends_when_absent(create_signed_commit: ModuleType) -> None:
    trailer = "Signed-off-by: bot[bot] <bot[bot]@users.noreply.github.com>"
    out = create_signed_commit.with_signoff("feat: do thing", trailer)
    assert out == f"feat: do thing\n\n{trailer}\n"


def test_with_signoff_idempotent_when_present(create_signed_commit: ModuleType) -> None:
    msg = "feat: do thing\n\nSigned-off-by: other <other@example.com>"
    out = create_signed_commit.with_signoff(msg, "Signed-off-by: ignored <i@x>")
    assert out == msg


def test_with_signoff_strips_message_trailing_newlines(
    create_signed_commit: ModuleType,
) -> None:
    # Multiple trailing newlines in the caller's message would otherwise
    # produce double-blank-line drift before the trailer.
    out = create_signed_commit.with_signoff("feat: x\n\n\n", "Signed-off-by: a <a@b>")
    assert out == "feat: x\n\nSigned-off-by: a <a@b>\n"


# ---------------------------------------------------------------------------
# main(): the new_branch == base_branch guard
# ---------------------------------------------------------------------------


def test_main_refuses_new_branch_equals_base_branch(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing `--new-branch == --base-branch` would, at the final force-PATCH,
    fast-forward the base branch onto the sync commit — a force-update of
    `main` from a workflow. Must hard-fail with exit code 2.
    """
    # Set a token so main() reaches the branch-name guard (token check is
    # before the guard).
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token-not-used")
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "main",
            "--message", "test",
            "--consumer-dir", str(tmp_path),
        ],
    )
    rc = create_signed_commit.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to operate" in err
    assert "--new-branch and --base-branch are the same" in err


def test_main_requires_token_env(
    create_signed_commit: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GH_APP_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "create-signed-commit.py",
            "--owner", "loomantix",
            "--repo", "test",
            "--base-branch", "main",
            "--new-branch", "sync/upstream-2026-05-16",
            "--message", "test",
            "--consumer-dir", str(tmp_path),
        ],
    )
    rc = create_signed_commit.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing token" in err


# ---------------------------------------------------------------------------
# _github_request: JSON object shape check
# ---------------------------------------------------------------------------


def test_github_request_rejects_non_object_json(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The GitHub Contents API returns objects, never bare arrays/strings
    on the endpoints this script hits. A non-object response signals a
    spoofed or proxied response; fail-closed rather than feed garbage
    into the rest of the pipeline.
    """
    import urllib.request

    class FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req: object, timeout: int = 30) -> FakeResp:
        # Return a JSON array — valid JSON but not an object.
        return FakeResp(b'["not", "an", "object"]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit._github_request("GET", "/test", "tok", None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "expected JSON object" in err


# ---------------------------------------------------------------------------
# github_api / github_api_optional: HTTPError + 404 handling
# ---------------------------------------------------------------------------


def test_github_api_optional_returns_none_on_404(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 404, "Not Found")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    result = create_signed_commit.github_api_optional("GET", "/test", "tok")
    assert result is None


def test_github_api_optional_exits_on_other_errors(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 500, "Internal Server Error")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit.github_api_optional("GET", "/test", "tok")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "500" in err


def test_github_api_exits_on_http_error(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> object:
        raise _http_error(path, 422, "Unprocessable Entity")

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    with pytest.raises(SystemExit) as exc:
        create_signed_commit.github_api("POST", "/test", "tok", {"foo": "bar"})
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "422" in err


def test_github_api_returns_parsed_body_on_success(
    create_signed_commit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, path: str, token: str, body: object) -> dict[str, object]:
        return {"sha": "abc123", "tree": {"sha": "deadbeef"}}

    monkeypatch.setattr(create_signed_commit, "_github_request", fake_request)
    result = create_signed_commit.github_api("GET", "/test", "tok")
    assert result == {"sha": "abc123", "tree": {"sha": "deadbeef"}}


def test_run_exits_on_command_failure(
    create_signed_commit: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        # `false` exits 1; `run` must surface that as sys.exit(1) with stderr.
        create_signed_commit.run("false")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "command failed" in err


# ---------------------------------------------------------------------------
# main() with mocked _github_request: full flow including ref create + force-PATCH
# ---------------------------------------------------------------------------


class _ApiRecorder:
    """Captures (method, path, body) tuples; returns scripted responses by path-prefix."""

    def __init__(self, responses: list[tuple[str, str, object]]) -> None:
        # responses: ordered list of (method, path_prefix, return_value_or_exception)
        self._responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method: str, path: str, token: str, body: object) -> object:
        self.calls.append((method, path, body))
        if not self._responses:
            raise AssertionError(f"unexpected API call: {method} {path}")
        exp_method, exp_prefix, value = self._responses.pop(0)
        # Explicit `raise` (not `assert`) so mismatches still surface under
        # `python -O` / `PYTHONOPTIMIZE=1`. Bare asserts would silently
        # pass mismatched calls, turning the contract test into a no-op.
        if method != exp_method:
            raise AssertionError(f"expected {exp_method} {exp_prefix}, got {method} {path}")
        if not path.startswith(exp_prefix):
            raise AssertionError(f"expected path prefix {exp_prefix}, got {path}")
        if isinstance(value, Exception):
            raise value
        return value


def _commit_main_argv(
    consumer_dir: Path,
    new_branch: str = "sync/upstream-2026-05-16",
    app_slug: str | None = None,
) -> list[str]:
    argv = [
        "create-signed-commit.py",
        "--owner", "loomantix",
        "--repo", "test",
        "--base-branch", "main",
        "--new-branch", new_branch,
        "--message", "feat: sync from upstream",
        "--consumer-dir", str(consumer_dir),
    ]
    if app_slug:
        argv.extend(["--app-slug", app_slug])
    return argv


def test_main_no_changes_exits_zero_without_api_calls(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty diff means no work to do — main() must return 0 BEFORE
    making any API calls. Otherwise a no-op sync wastes a token round-trip
    and (worse) could force-update the ref onto the unchanged tree.
    """
    recorder = _ApiRecorder([])  # any API call asserts
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 0
    assert recorder.calls == []
    assert "No changes to commit." in capsys.readouterr().out


def test_main_full_flow_creates_new_branch_when_absent(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: with one upsert + one delete in the working tree, main()
    must walk the 5-step API sequence (ref → commit → tree → blobs → ref).
    """
    _git("mv", "seed.txt", "renamed.txt", cwd=git_repo)
    (git_repo / "new.txt").write_text("brand new\n")

    recorder = _ApiRecorder([
        # 1. base-branch ref
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        # 2. base commit
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
        # 3. blob for renamed.txt
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-renamed"}),
        # 4. blob for new.txt
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-new"}),
        # 5. tree
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "new-tree-sha"}),
        # 6. commit
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "new-commit-sha"}),
        # 7. check if new branch exists — 404 = absent
        ("GET", "/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16",
         _http_error("/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16", 404, "Not Found")),
        # 8. POST new ref
        ("POST", "/repos/loomantix/test/git/refs", {"ref": "refs/heads/x"}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo, app_slug="loomantix"))

    rc = create_signed_commit.main()
    assert rc == 0, capsys.readouterr().err

    # Verify the commit's message carried the Signed-off-by trailer.
    commit_call = next(c for c in recorder.calls if c[0] == "POST" and c[1].endswith("/commits"))
    assert isinstance(commit_call[2], dict)
    assert "Signed-off-by: loomantix[bot]" in commit_call[2]["message"]

    # Verify the tree contained both the upsert (with blob sha) and the
    # delete (with sha: null) for the rename.
    tree_call = next(c for c in recorder.calls if c[0] == "POST" and c[1].endswith("/trees"))
    assert isinstance(tree_call[2], dict)
    paths_in_tree = {e["path"]: e for e in tree_call[2]["tree"]}
    assert "renamed.txt" in paths_in_tree and paths_in_tree["renamed.txt"]["sha"] is not None
    assert "new.txt" in paths_in_tree and paths_in_tree["new.txt"]["sha"] is not None
    assert "seed.txt" in paths_in_tree and paths_in_tree["seed.txt"]["sha"] is None


def test_main_force_updates_branch_when_already_exists(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the date-stamped branch already exists from a prior run, the
    script force-PATCHes it. Verify the PATCH is sent with force: true.
    """
    (git_repo / "new.txt").write_text("x\n")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
        ("POST", "/repos/loomantix/test/git/blobs", {"sha": "blob-1"}),
        ("POST", "/repos/loomantix/test/git/trees", {"sha": "new-tree-sha"}),
        ("POST", "/repos/loomantix/test/git/commits", {"sha": "new-commit-sha"}),
        # Branch exists this time — GET returns an object.
        ("GET", "/repos/loomantix/test/git/ref/heads/sync/upstream-2026-05-16",
         {"object": {"sha": "old-sha"}}),
        # PATCH the existing ref with force: true.
        ("PATCH", "/repos/loomantix/test/git/refs/heads/sync/upstream-2026-05-16",
         {"ref": "refs/heads/x"}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 0

    patch_call = next(c for c in recorder.calls if c[0] == "PATCH")
    assert isinstance(patch_call[2], dict)
    assert patch_call[2].get("force") is True
    assert patch_call[2].get("sha") == "new-commit-sha"


def test_main_rejects_non_file_upsert_path(
    create_signed_commit: ModuleType,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even with -uall, an upsert path that resolves to a non-regular file
    (broken symlink, fifo, etc.) must hard-fail rather than silently drop
    the entry from the tree.
    """
    # Create a dangling symlink that git will report as new.
    (git_repo / "dangling").symlink_to(git_repo / "nope")

    recorder = _ApiRecorder([
        ("GET", "/repos/loomantix/test/git/ref/heads/main",
         {"object": {"sha": "base-sha"}}),
        ("GET", "/repos/loomantix/test/git/commits/base-sha",
         {"tree": {"sha": "base-tree-sha"}}),
    ])
    monkeypatch.setattr(create_signed_commit, "_github_request", recorder)
    monkeypatch.setenv("GH_APP_TOKEN", "fake-token")
    monkeypatch.setattr("sys.argv", _commit_main_argv(git_repo))

    rc = create_signed_commit.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a regular file" in err
