"""Deterministic integration coverage for the agent-loop wrapper.

Each test drives the real `.claude/skills/agent-loop/scripts/agent-loop.sh`
against a throwaway git repo + bare remote, with `gh`/`ready.py`/`claude`
replaced by tiny stubs on `PATH`. The cases mirror the Codex wrapper's safety
suite: allowlisting, ready/dependency gates, worktree isolation, dry-run, hook
ordering, claim/assignee-identity races, worker failure + recovery,
capacity/timeout retry with model fallback, private bounded logs, fresh-base
publication, and conflict-marker rejection.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_LOOP = REPO_ROOT / ".claude/skills/agent-loop/scripts/agent-loop.sh"


def _run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def consumer(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "consumer"
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()

    _run_git("init", "--bare", str(remote))
    _run_git("init", "-b", "main", str(repo))
    _run_git("config", "user.name", "Test", cwd=repo)
    _run_git("config", "user.email", "test@example.invalid", cwd=repo)
    # Hermetic commits: the worker/hook stubs commit inside worktrees that share
    # this repo's config, so pin signing off regardless of any ambient global
    # `commit.gpgsign = true` (which would otherwise block the throwaway commits).
    _run_git("config", "commit.gpgsign", "false", cwd=repo)

    script = repo / ".claude/skills/agent-loop/scripts/agent-loop.sh"
    ready = repo / ".claude/skills/issues/scripts/ready.py"
    script.parent.mkdir(parents=True)
    ready.parent.mkdir(parents=True)
    shutil.copy2(AGENT_LOOP, script)
    _write_executable(
        ready,
        "#!/usr/bin/env python3\n"
        "import os\n"
        "print(os.environ.get('AGENT_READY_JSON', '[]'))\n",
    )
    (repo / "agent-loop-instructions.md").write_text(
        "# Local-only worker instructions\n", encoding="utf-8"
    )
    (repo / ".claude/skills/agent-loop/prompt.txt").write_text(
        "Implement #{ISSUE_ID}, commit locally, and do not push or open a PR.\n",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=repo)
    _run_git("commit", "-m", "test fixture", cwd=repo)
    _run_git("remote", "add", "origin", str(remote), cwd=repo)
    _run_git("push", "-u", "origin", "main", cwd=repo)
    _run_git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=repo)

    gh = bin_dir / "gh"
    _write_executable(
        gh,
        r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['AGENT_STATE_DIR'])
with (state / 'gh.log').open('a') as handle:
    handle.write(' '.join(args) + '\n')
issues = json.loads(os.environ.get('AGENT_ISSUES_JSON', '{}'))
if args[:3] == ['api', 'user', '--jq']:
    print('tester')
elif args[:2] == ['issue', 'view']:
    number = args[2]
    issue = issues.get(number, {'number': int(number), 'title': 'fixture', 'body': '', 'state': 'OPEN', 'labels': [{'name': 'dev: agent'}], 'assignees': []})
    if args[3:] == ['--json', 'assignees']:
        login = os.environ.get('AGENT_VERIFIED_ASSIGNEE', 'tester')
        print(json.dumps({'assignees': ([{'login': login}] if login else [])}))
    elif 'closedByPullRequestsReferences' in ' '.join(args):
        dep = json.loads(os.environ.get('AGENT_ISSUE_DEPENDENCIES', '{}')).get(number, [])
        for row in dep:
            print('\t'.join(str(value) for value in row))
    elif '--jq' in args and '.assignees | length' in args:
        print(1)
    else:
        print(json.dumps(issue))
elif args[:2] == ['issue', 'edit']:
    pass
elif args[:2] == ['pr', 'view']:
    number = args[2]
    row = json.loads(os.environ.get('AGENT_PRS_JSON', '{}')).get(number)
    if row:
        print('\t'.join(str(value) for value in row))
    else:
        sys.exit(1)
elif args[:2] == ['pr', 'create']:
    if os.environ.get('AGENT_PR_CREATE_FAIL'):
        print('pr create failed (stub)', file=sys.stderr)
        sys.exit(1)
    print('https://example.invalid/pr/1')
else:
    print('unsupported gh invocation: ' + ' '.join(args), file=sys.stderr)
    sys.exit(2)
""",
    )
    return repo, remote, bin_dir, state_dir


def _issue(number: int, body: str = "", *, assigned: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "OPEN",
        "labels": [{"name": "dev: agent"}],
        "assignees": [{"login": "tester"}] if assigned else [],
    }


def _config(tmp_path: Path, **overrides: str | int) -> str:
    values: dict[str, str | int] = {
        "base_branch": "main",
        "setup_hook": "printf 'setup\\n' >> \"$EVENT_LOG\"",
        "validation_hook": "printf 'validate\\n' >> \"$EVENT_LOG\"",
        "claude_review_hook": "printf 'claude\\n' >> \"$EVENT_LOG\"",
        "codex_review_hook": "printf 'codex\\n' >> \"$EVENT_LOG\"",
        "worker_hook": "printf 'worker\\n' >> \"$EVENT_LOG\"; printf 'done\\n' > result.txt; git add result.txt; git commit -m 'fix: worker'",
        "worker_retries": 1,
        "worker_timeout_seconds": 5,
        "hook_timeout_seconds": 10,
        "retry_on_timeout": "true",
        "retry_delay_seconds": 0,
        "dependency_gate": "ready",
        "branch_prefix": "agent-loop",
        "worktree_root": str(tmp_path / "worktrees"),
        "log_root": str(tmp_path / "logs"),
        "log_max_kb": 128,
        "output_max_lines": 10,
    }
    values.update(overrides)
    return "\n".join(f"{key} = {value}" for key, value in values.items()) + "\n"


def _run(
    fixture: tuple[Path, Path, Path, Path],
    args: list[str],
    *,
    issues: list[dict[str, object]],
    config: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    repo, _, bin_dir, state_dir = fixture
    (repo / ".claude/skills/agent-loop/agent-loop.config").write_text(
        config, encoding="utf-8"
    )
    # Hooks run under `bash -lc`, a login shell that re-sources profile files. On
    # a developer box those dotfiles prepend real tool paths (e.g. a genuine
    # `claude` in ~/.local/bin), shadowing the stubs this suite installs in
    # bin_dir. Point HOME at an empty dir so the login shell finds no profile to
    # reorder PATH, keeping the run hermetic here and on CI alike.
    home = bin_dir.parent / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    # The temporary ready.py/gh/claude fixtures are black-box shell dependencies,
    # not coverage targets. pytest-cov exports COV_CORE_* for subprocess
    # collection; letting these standalone stubs auto-start coverage can produce
    # statement data that pytest-cov 6 cannot combine with this repo's branch
    # data.
    for key in [name for name in env if name.startswith("COV_CORE_")]:
        env.pop(key)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "AGENT_STATE_DIR": str(state_dir),
            "AGENT_ISSUES_JSON": json.dumps(
                {str(issue["number"]): issue for issue in issues}
            ),
            "AGENT_READY_JSON": json.dumps(issues),
            "EVENT_LOG": str(state_dir / "events.log"),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / ".claude/skills/agent-loop/scripts/agent-loop.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_script_remains_executable_and_valid_bash() -> None:
    assert stat.S_IMODE(AGENT_LOOP.stat().st_mode) == 0o755
    subprocess.run(["bash", "-n", str(AGENT_LOOP)], check=True)


def test_issue_allowlist_never_selects_unrelated_ready_work(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(1), _issue(2)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "Issue #2" in result.stdout
    assert "Issue #1" not in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log


def test_merged_dependency_gate_requires_commit_on_base(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    base_sha = _run_git("rev-parse", "origin/main", cwd=consumer[0]).stdout.strip()
    blocked = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(2, "Depends on PR #7")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={"AGENT_PRS_JSON": json.dumps({"7": ["CLOSED", "main", base_sha]})},
    )
    assert blocked.returncode == 0
    assert "NOT merged into origin/main" in blocked.stdout

    merged = _run(
        consumer,
        ["--issues", "2", "--dry-run"],
        issues=[_issue(2, "Depends on PR #7")],
        config=_config(tmp_path, dependency_gate="merged-to-base"),
        extra_env={"AGENT_PRS_JSON": json.dumps({"7": ["MERGED", "main", base_sha]})},
    )
    assert merged.returncode == 0, merged.stderr
    assert "merged into origin/main" in merged.stdout


def test_dry_run_shows_plan_without_mutation(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    worktrees = tmp_path / "worktrees"
    result = _run(
        consumer,
        ["--issues", "3", "--dry-run"],
        issues=[_issue(3)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "Setup hook:" in result.stdout
    assert "Review order: Claude deep review -> Codex review" in result.stdout
    assert "Publication:" in result.stdout
    assert "no claim, worktree, hook, push, or PR mutation" in result.stdout
    assert not worktrees.exists()
    assert not (consumer[3] / "events.log").exists()


def test_per_issue_worktrees_and_hook_order(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "4,5", "--iterations", "2"],
        issues=[_issue(4), _issue(5)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    paths = re.findall(r"^   Worktree: (.+)$", result.stdout, re.MULTILINE)
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert all(not Path(path).exists() for path in paths)
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    expected = ["setup", "worker", "validate", "claude", "validate", "codex", "validate", "validate"]
    assert events == expected * 2
    remote_branches = _run_git("for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]).stdout
    assert "issue-4" in remote_branches
    assert "issue-5" in remote_branches


@pytest.mark.parametrize(
    ("worker_hook", "expected"),
    [
        ("printf dirty > dirty.txt; exit 7", "after changing or committing work"),
        ("exit 7", "without recoverable retry conditions"),
    ],
)
def test_worker_failure_preserves_worktree(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    worker_hook: str,
    expected: str,
) -> None:
    result = _run(
        consumer,
        ["--issues", "6"],
        issues=[_issue(6)],
        config=_config(tmp_path, worker_hook=worker_hook, worker_retries=0),
    )
    assert result.returncode != 0
    assert expected in result.stderr
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    worktree = Path(match.group(1))
    assert worktree.exists()
    if "dirty" in worker_hook:
        assert (worktree / "dirty.txt").exists()


def test_capacity_failure_uses_fallback_model(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The default worker is the Claude CLI. Stub it so the primary model reports
    # a capacity failure and the retry switches to worker_fallback_model.
    claude = consumer[2] / "claude"
    _write_executable(
        claude,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$AGENT_STATE_DIR/models.log"
if [[ "$*" == *"--model primary"* ]]; then
  echo 'capacity exhausted' >&2
  exit 9
fi
printf 'done\\n' > result.txt
git add result.txt
git commit -m 'fix: fallback worker'
""",
    )
    result = _run(
        consumer,
        ["--issues", "7"],
        issues=[_issue(7)],
        config=_config(
            tmp_path,
            worker_hook="",
            worker_model="primary",
            worker_fallback_model="fallback",
            worker_retries=1,
        ),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    models = (consumer[3] / "models.log").read_text(encoding="utf-8")
    assert "--model primary" in models
    assert "--model fallback" in models


def test_timeout_retries_only_an_unchanged_worktree(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    retry_mark = tmp_path / "retry-mark"
    worker = (
        'if [ ! -e "$RETRY_MARK" ]; then touch "$RETRY_MARK"; sleep 5; fi; '
        "printf done > result.txt; git add result.txt; git commit -m 'fix: retry worker'"
    )
    result = _run(
        consumer,
        ["--issues", "8"],
        issues=[_issue(8)],
        config=_config(
            tmp_path,
            worker_hook=worker,
            worker_timeout_seconds=1,
            worker_retries=1,
        ),
        extra_env={"RETRY_MARK": str(retry_mark)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Retrying worker" in result.stdout


def test_fresh_base_is_integrated_and_validated_before_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    updater = tmp_path / "advance-base.sh"
    _write_executable(
        updater,
        """#!/usr/bin/env bash
set -e
clone="$AGENT_STATE_DIR/base-clone"
git clone "$REMOTE_PATH" "$clone" >/dev/null 2>&1
git -C "$clone" config user.name Test
git -C "$clone" config user.email test@example.invalid
printf 'fresh\\n' > "$clone/fresh-base.txt"
git -C "$clone" add fresh-base.txt
git -C "$clone" commit -m 'chore: advance base' >/dev/null
git -C "$clone" push origin main >/dev/null
printf 'codex\\n' >> "$EVENT_LOG"
""",
    )
    result = _run(
        consumer,
        ["--issues", "9"],
        issues=[_issue(9)],
        config=_config(tmp_path, codex_review_hook=str(updater)),
        extra_env={"REMOTE_PATH": str(consumer[1])},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _run_git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]
    ).stdout.strip()
    published = _run_git("show", f"{branch}:fresh-base.txt", cwd=consumer[1]).stdout
    assert published == "fresh\n"
    events = (consumer[3] / "events.log").read_text(encoding="utf-8").splitlines()
    assert events[-1] == "validate"


def test_large_worker_writes_survive_and_log_is_bounded(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Regression: the log-size bound must not constrain files the worker writes. A
    # prior `ulimit -f` capped every file the hook wrote and SIGXFSZ-killed (and
    # truncated) legitimate large writes. The worker below writes a repo file and
    # streams stdout both larger than the cap; the file must land intact and the
    # captured log must still be bounded to roughly log_max_kb.
    cap_kb = 64
    worker = (
        "dd if=/dev/zero of=big.bin bs=1024 count=2048 2>/dev/null; "  # 2 MiB file > cap
        "seq 1 200000; "  # ~1.3 MiB of stdout, far over the log cap
        "git add big.bin; git commit -m 'fix: large artifact'"
    )
    result = _run(
        consumer,
        ["--issues", "11"],
        issues=[_issue(11)],
        config=_config(tmp_path, worker_hook=worker, log_max_kb=cap_kb),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    branch = _run_git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]
    ).stdout.strip()
    size = _run_git("cat-file", "-s", f"{branch}:big.bin", cwd=consumer[1]).stdout.strip()
    assert int(size) == 2048 * 1024  # written in full, not truncated at the log cap
    logs = list((tmp_path / "logs").glob("*/worker-attempt-1.log"))
    assert logs, "worker log was not captured"
    assert logs[0].stat().st_size <= cap_kb * 1024 + 8192  # bounded to ~log_max_kb


def test_committed_conflict_markers_block_publication(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Regression: `inspect_publication_diff` runs in an `||` context (set -e off), so
    # the `git diff --check` gate must check its status explicitly. A committed
    # conflict marker in the publication diff must block the PR, not sail through.
    worker = (
        r"printf '<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n' > conflict.txt; "
        "git add conflict.txt; git commit -m 'fix: conflicted'"
    )
    result = _run(
        consumer,
        ["--issues", "12"],
        issues=[_issue(12)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode != 0
    assert "conflict markers or whitespace errors" in result.stderr
    branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]
    ).stdout
    assert "issue-12" not in branches  # never published


def test_issue_branch_has_no_upstream_during_worker(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _run_git("config", "push.default", "upstream", cwd=consumer[0])
    worker = (
        "if git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' "
        ">/dev/null 2>&1; then exit 41; fi; "
        "printf done > result.txt; git add result.txt; "
        "git commit -m 'fix: untracked issue branch'"
    )
    result = _run(
        consumer,
        ["--issues", "13"],
        issues=[_issue(13)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_missing_default_claude_fails_before_claim(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, bin_dir, state_dir = consumer
    no_claude_bin = tmp_path / "no-claude-bin"
    no_claude_bin.mkdir()
    for command in ("bash", "git", "jq", "python3", "timeout"):
        executable = shutil.which(command)
        assert executable is not None
        (no_claude_bin / command).symlink_to(executable)
    (no_claude_bin / "gh").symlink_to(bin_dir / "gh")

    result = _run(
        consumer,
        ["--issues", "14"],
        issues=[_issue(14)],
        config=_config(tmp_path, worker_hook=""),
        extra_env={"PATH": str(no_claude_bin)},
    )
    assert result.returncode != 0
    assert "required command not found for default worker: claude" in result.stderr
    gh_log = state_dir / "gh.log"
    assert not gh_log.exists() or "issue edit" not in gh_log.read_text(encoding="utf-8")
    assert not (tmp_path / "worktrees").exists()


def test_allowlist_does_not_bypass_ready_eligibility(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "15", "--dry-run"],
        issues=[_issue(15, "Blocked by #99")],
        config=_config(tmp_path),
        extra_env={"AGENT_READY_JSON": "[]"},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Allowlisted issue #15 is not ready" in result.stderr
    assert "Issue #15 (" not in result.stdout
    gh_log = (consumer[3] / "gh.log").read_text(encoding="utf-8")
    assert "issue edit" not in gh_log


@pytest.mark.parametrize("assigned", [False, True])
def test_claim_and_resume_revalidate_assignee_identity(
    consumer: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    assigned: bool,
) -> None:
    args = ["--issues", "16"]
    if assigned:
        args.append("--resume")
    result = _run(
        consumer,
        args,
        issues=[_issue(16, assigned=assigned)],
        config=_config(tmp_path),
        extra_env={"AGENT_VERIFIED_ASSIGNEE": "other-user"},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "could not be claimed; skipping" in result.stdout
    assert not list((tmp_path / "worktrees").glob("*"))


def test_persistent_logs_are_owner_only(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    result = _run(
        consumer,
        ["--issues", "17"],
        issues=[_issue(17)],
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    log_dirs = list((tmp_path / "logs").iterdir())
    assert len(log_dirs) == 1
    assert stat.S_IMODE(log_dirs[0].stat().st_mode) == 0o700
    for log_file in log_dirs[0].iterdir():
        assert stat.S_IMODE(log_file.stat().st_mode) & 0o077 == 0


def test_untracked_leftover_does_not_abort_batch_after_publish(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A worker/setup hook can leave a non-ignored untracked file. With
    # `status.showUntrackedFiles=no` the clean-tree gates pass, but a plain
    # `git worktree remove` would still exit non-zero on it — which, post-publish
    # and under `set -e`, would abort the whole batch and fire a bogus recovery
    # banner. The success-path removal must force + tolerate so the run finishes
    # and continues to the next issue.
    _run_git("config", "status.showUntrackedFiles", "no", cwd=consumer[0])
    worker = (
        "printf done > result.txt; git add result.txt; git commit -m 'fix: worker'; "
        "printf scratch > leftover.txt"  # untracked, not ignored
    )
    result = _run(
        consumer,
        ["--issues", "21,22", "--iterations", "2"],
        issues=[_issue(21), _issue(22)],
        config=_config(tmp_path, worker_hook=worker),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Worktree preserved" not in result.stderr
    remote_branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]
    ).stdout
    assert "issue-21" in remote_branches
    assert "issue-22" in remote_branches
    paths = re.findall(r"^   Worktree: (.+)$", result.stdout, re.MULTILINE)
    assert len(paths) == 2
    assert all(not Path(path).exists() for path in paths)  # removed despite leftover


def test_pr_create_failure_reports_orphaned_pushed_branch(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # The push lands the remote branch before `gh pr create` runs. If PR creation
    # fails, the recovery message must name the already-pushed branch so the
    # operator can open the PR or delete it — otherwise a re-run (new RUN_TAG)
    # orphans the first branch and double-PRs the issue.
    result = _run(
        consumer,
        ["--issues", "23"],
        issues=[_issue(23)],
        config=_config(tmp_path),
        extra_env={"AGENT_PR_CREATE_FAIL": "1"},
    )
    assert result.returncode != 0
    assert "gh pr create' failed" in result.stderr
    assert "Pushed origin/agent-loop/issue-23" in result.stderr
    # The push really happened, so the branch exists on the remote.
    remote_branches = _run_git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-loop", cwd=consumer[1]
    ).stdout
    assert "issue-23" in remote_branches


def test_malformed_ready_payload_is_a_hard_error_not_empty_backlog(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # ready.py exiting 0 with a non-array payload must be a hard error, not read
    # as "no work" (which would exit 0 and look like an empty backlog).
    result = _run(
        consumer,
        [],
        issues=[_issue(24)],
        config=_config(tmp_path),
        extra_env={"AGENT_READY_JSON": '{"malformed": true}'},
    )
    assert result.returncode == 1
    assert "issue selection failed" in result.stderr
    assert not (tmp_path / "worktrees").exists()


def test_backstop_recovery_is_accurate_when_no_worktree_exists(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # Force a bare `set -e` abort before the worktree is created: point
    # worktree_root at a regular file so `mkdir -p` fails. The on_exit backstop
    # must fire, and recovery_message must NOT claim a worktree is preserved at a
    # path that was never created.
    worktree_file = tmp_path / "worktrees-as-file"
    worktree_file.write_text("not a directory\n", encoding="utf-8")
    result = _run(
        consumer,
        ["--issues", "25"],
        issues=[_issue(25)],
        config=_config(tmp_path, worktree_root=str(worktree_file)),
    )
    assert result.returncode != 0
    assert "No worktree exists" in result.stderr
    assert "Worktree preserved" not in result.stderr


def test_timeout_with_committed_work_does_not_retry(
    consumer: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    # A timeout that fires AFTER the worker committed must not retry on top of
    # that work — the retry gate is `worktree_has_work`, which a committed change
    # trips regardless of the timeout exit code (124/137).
    worker = (
        "printf done > result.txt; git add result.txt; "
        "git commit -m 'fix: committed then hung'; sleep 5"
    )
    result = _run(
        consumer,
        ["--issues", "26"],
        issues=[_issue(26)],
        config=_config(
            tmp_path,
            worker_hook=worker,
            worker_timeout_seconds=1,
            worker_retries=1,
        ),
    )
    assert result.returncode != 0
    assert "after changing or committing work" in result.stderr
    assert "Retrying worker" not in result.stdout
    match = re.search(r"Worktree preserved: (.+)", result.stderr)
    assert match
    assert Path(match.group(1)).exists()
