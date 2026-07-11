---
name: agent-loop
description: Autonomous issue implementation loop with strict issue allowlisting, one linked worktree per issue, configurable setup and local review hooks, fresh-base validation, and publication only after deterministic local Claude and Codex reviews. Use when Claude should implement a bounded GitHub issue queue without hosted AI reviewers.
argument-hint: '[iterations] [--iterations N] [--issues N,N,...] [--resume] [--dry-run]'
---

# Agent Loop

Run isolated issue workers and publish one reviewed pull request per issue. The
wrapper owns selection, claiming, worktrees, local reviews, base integration,
push, and PR creation. A worker only implements, validates, refactors, and
commits locally — by default it is the Claude CLI, but any command can be
substituted via `worker_hook`.

## Usage

```bash
.claude/skills/agent-loop/scripts/agent-loop.sh --issues 5105,5106 --iterations 2

.claude/skills/agent-loop/scripts/agent-loop.sh --issues 5105,5106 --dry-run
```

| Option             | Behavior                                                                                                                                                                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issues N,N,...` | Restrict selection to exactly these issue numbers. Never fall through to unrelated ready work.                                                                                |
| `--iterations N`   | Process at most `N` issues. A legacy numeric first argument remains accepted.                                                                                                 |
| `--resume`         | Permit an eligible issue already assigned only to the current user.                                                                                                           |
| `--dry-run`        | Show selections, dependency decisions, worktree/branch paths, hooks, and publication without claiming, fetching, creating worktrees, running hooks, pushing, or creating PRs. |

Omitting `--issues` retains the ready-queue behavior for backward
compatibility. Use an allowlist for every scoped or retrospective-driven run.

Collection branches and worker-side publication are removed. Every selected
issue gets a unique `agent-loop/issue-<N>-<run>` branch and linked worktree.

## Required Consumer Files

- `agent-loop-instructions.md`: repository conventions and worker safety rules.
- `.claude/skills/agent-loop/prompt.txt`: prompt containing `{ISSUE_ID}`.
  Require a local commit and forbid push/PR creation.
- `.claude/skills/agent-loop/agent-loop.config`: hook and base configuration.
- `.claude/skills/issues/scripts/ready.py`: ready-queue provider (synced with
  the `/issues` skill).

These consumer files are bootstrapped with `create_if_missing: true`; merge
template changes manually into existing consumers.

## Config Interface

The config is parsed as literal `key = value` lines and is never sourced.
Unknown or duplicate keys fail closed. Hook values are shell commands executed
with the issue worktree as the current directory.

| Key                                              | Purpose                                                                                                                                |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `base_branch`                                    | Integration branch; env `AGENT_LOOP_BASE_BRANCH` overrides it.                                                                         |
| `setup_hook`                                     | Isolated bootstrap, such as `pnpm install --frozen-lockfile`. Never symlink mutable dependency directories.                            |
| `validation_hook`                                | Bounded validation after the worker, after each review, and after fresh-base integration.                                              |
| `claude_review_hook`                             | Required fresh local Claude deep review. It must fix confirmed findings, validate, commit fixes, and never push.                       |
| `codex_review_hook`                              | Required local Codex review against `$AGENT_LOOP_REVIEW_BASE`. It must fix confirmed findings, validate, commit fixes, and never push. |
| `worker_hook`                                    | Optional worker command override. Default is the Claude CLI in headless, auto-approving mode.                                          |
| `worker_model`, `worker_fallback_model`          | Primary and capacity-fallback models for the default worker.                                                                           |
| `worker_retries`                                 | Retries after clean capacity/timeout failures. Default `1`.                                                                            |
| `worker_timeout_seconds`, `hook_timeout_seconds` | Bounded execution time.                                                                                                                |
| `retry_on_timeout`, `retry_delay_seconds`        | Timeout retry policy.                                                                                                                  |
| `dependency_gate`                                | `ready` (legacy) or `merged-to-base`.                                                                                                  |
| `branch_prefix`, `worktree_root`, `log_root`     | Isolated path/ref controls.                                                                                                            |
| `log_max_kb`, `output_max_lines`                 | Bound captured logs and displayed failure tails.                                                                                       |

Hooks receive `AGENT_LOOP_ISSUE_ID`, `AGENT_LOOP_BASE_BRANCH`,
`AGENT_LOOP_BRANCH`, `AGENT_LOOP_WORKTREE`, `AGENT_LOOP_LOG_DIR`, and
`AGENT_LOOP_PROMPT`. Review hooks also receive `AGENT_LOOP_REVIEW_BASE` after a
fresh fetch.

For a non-mutating consumer smoke test from an upstream development worktree,
set `AGENT_LOOP_PROJECT_DIR=/path/to/consumer` and pass `--dry-run`. Do not use
that override for a mutating run; execute the consumer's synced script instead.

Do not put secrets, credentials, PHI, customer identifiers, or user data in
config values or hook output. The wrapper deliberately uses a generic PR body
and never copies issue bodies, model logs, or findings into GitHub.

## Default Worker and the Invocation Lock

When `worker_hook` is unset, the wrapper runs the Claude CLI in
`--permission-mode bypassPermissions --print` mode against the issue prompt.
That is the only `claude` invocation in the script, and it is bracketed by
`# claude-cli-invocations:start` / `:end` markers. The upstream CI gate
`.claude/lint-claude-cli-invocations.py` hashes the locked region and refuses
to pass unless the hash is listed for this path in
`.claude/claude-cli-invocations.allowlist`. Any change to the flags, model
handling, or prompt wiring rotates the hash and must be re-approved by a
byte-level review of the region in the same PR — the diff is the audit trail.
A consumer that sets `worker_hook` supplies its own runner and the Claude CLI
is not required on `PATH`.

## Deterministic Phase Order

1. Select and dependency-gate an eligible issue.
2. Claim it, detecting assignment races.
3. Create a unique worktree and branch from `origin/<base>`.
4. Run the isolated setup hook.
5. Run the worker and require a clean local commit.
6. Validate, then run the fresh Claude deep-review hook and validate again.
7. Fetch the base, run the Codex-review hook against that fresh ref, and validate.
8. Fetch and merge the base again, inspect a bounded diff, and revalidate.
9. Confirm no worker/hook pushed the branch; only then push and open the PR.

Do not invoke Gemini, Copilot, `reviewit`, or any GitHub-hosted AI reviewer.

## Dependency Gate

With `dependency_gate = merged-to-base`, parse `Blocked by #N`, `Depends on #N`,
`Blocked by PR #N`, and `Depends on PR #N`. A PR dependency passes only when
GitHub reports it merged to the configured base and its merge commit is an
ancestor of the current `origin/<base>`. An issue dependency passes only when
one of its closing PRs meets the same condition. Closed issues alone do not
pass. `dependency_gate = ready` (the default) preserves the legacy ready-queue
semantics.

## Failure and Recovery

On any non-zero worker exit, inspect whether the worktree is dirty or contains
new commits. Preserve all changed or committed work and stop with recovery
commands. Retry capacity/timeouts only when the worktree is unchanged. Review,
setup, integration, and validation failures also preserve the worktree. Never
reset, reuse, clean, or delete a dirty recovery worktree.

Successful publication removes the clean linked worktree but retains the local
branch. Interrupted runs preserve the active worktree.

## Migration From the Collection-Branch Loop

The previous loop pushed every iteration to a shared collection branch and
opened one summary PR at the end. That model is gone:

- The removed `[collection-branch]` positional now errors. Scope a run with
  `--issues N,N,...` instead. The numeric `[iterations]` positional still works,
  and `--iterations N` is its explicit form.
- Each issue now gets its own branch, worktree, and PR — there is no summary PR.
- `agent-loop.config`, `prompt.txt`, and `agent-loop-instructions.md` are
  `create_if_missing` targets, so existing consumers keep their old copies. They
  must be migrated by hand: add the `setup_hook`, `validation_hook`,
  `claude_review_hook`, and `codex_review_hook` keys (both review hooks are
  required for a mutating run), and rewrite the prompt/instructions so the worker
  commits locally and never pushes or opens a PR.

## Test Guidance

Use focused commands and bounded output. For Vitest 4, target a test with:

```bash
pnpm --filter frontend test:run TestName
```

Do not insert `--` before `TestName`; that can run the full suite.

## Source of Truth

This directory is upstream-owned and synced to consumers. Change reusable
mechanics here, not in a consumer's synced copy.
