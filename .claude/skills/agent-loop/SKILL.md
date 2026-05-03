---
name: agent-loop
description: Autonomous Claude relay loop on top of /issues — claim a ready issue, spawn a fresh Claude session to work it, push results to a collection branch, repeat. Opens a summary PR at the end.
argument-hint: "[iterations] [collection-branch] [--resume]"
---

# /agent-loop

Run an autonomous Claude relay over the `/issues` ready queue. Each iteration claims an issue, spawns a fresh Claude session in `--permission-mode bypassPermissions`, lets it work, then pushes the result to a shared collection branch. After the loop, opens an `agent-loop: <branch>` PR with the closed-issues + commit-log summary.

## Usage

```bash
.claude/skills/agent-loop/scripts/agent-loop.sh [iterations] [collection-branch] [--resume]
```

Defaults: 10 iterations, auto-generated collection branch (`agent-loop-<timestamp>-<rand>`), ready-queue-only.

| Args | Behavior |
|---|---|
| `5` | 5 iterations, auto-generated branch, ready-only |
| `5 wasm-plugins` | 5 iterations, named collection branch |
| `5 wasm-plugins --resume` | also pick up issues already assigned to `@me` (orphan-recovery) |
| `--help` | print the script header |

## Prerequisites (per-repo, one-time)

1. **`agent-loop-instructions.md` at the repo root** — repo-specific agent instructions (codebase conventions, build commands, test invocation, deployment quirks). The Claude prompt is fixed: `Read @agent-loop-instructions.md and follow the instructions. Your assigned issue is #N. Run 'gh issue view N' to see the full description, then complete it.` If the file is missing, the script exits before claiming work.
2. **`dev: human-only` label** in the consumer's GitHub repo — used to keep manual-testing or human-review-required issues out of the autonomous queue. The script's `pick_next_issue` filter excludes issues carrying this label.
3. **`gh`, `jq`, `xxd`, `python3`, `claude`** on `PATH`. The script hard-fails if any are missing.
4. **`/issues` skill synced** — the script invokes `.claude/skills/issues/scripts/ready.py --json` to enumerate the queue. Without it the script exits at startup.

## Behavior per iteration

1. Sync the worktree with `origin/<collection-branch>` — fetches and fast-forwards. If the remote was force-pushed, cherry-picks the local commits onto the new tip (with a pre-reset SHA snapshot so a failed cherry-pick restores the original chain rather than leaving partial replay). Genuine merge conflicts fail loud; the eventual push surfaces persistent ones via the `PUSH_FAILURES` counter.
2. Pick a work item: with `--resume`, prefer any open issue already assigned to `@me`; otherwise the first dependency-free row from `ready.py --json` that isn't labeled `dev: human-only`.
3. Claim by adding `@me` as assignee. Re-fetch immediately afterward — if there are >1 assignees, a parallel worker raced; release and try the next row.
4. Spawn `claude --chrome --permission-mode bypassPermissions --print "Read @agent-loop-instructions.md..." --output-format stream-json` and stream the events through `jq` for colored display. The Claude PID is tracked so `Ctrl-C` interrupts the loop cleanly.
5. Snapshot newly-closed issues since the loop started (used for the final PR body) and push to the collection branch via `push_to_collection` — retry-and-merge with up to 3 attempts.

## After the loop

If any commits accumulated on `origin/<collection-branch>` past `origin/<default-branch>`, opens an `agent-loop: <collection-branch>` PR (or attaches to an existing one) with:

- summary line: `<N> iteration(s), <M> commit(s)`
- `### Closed Issues` — newly-closed issues since loop start
- `### Commit Log` — `git log --oneline <default>..<collection>`

Then removes the worktree.

## Worktree isolation

Each invocation creates `/tmp/agent-loop-<branch>-<pid>` so multiple runs don't collide. The `Ctrl-C` trap attempts a final push of any committed work and then removes the worktree — but if that final push fails (auth, branch protection, force-push race), the worktree is **preserved** at `/tmp/agent-loop-...` so a human can recover the local commits. The post-loop path also preserves the worktree on push failure, before skipping PR creation.

## Default branch

Auto-detected via `git symbolic-ref refs/remotes/origin/HEAD`. Works on consumers using `main`, `staging`, or any other default — no per-repo configuration.

## Source of truth

This skill lives upstream at `.claude/skills/agent-loop/`. SKILL.md and `scripts/agent-loop.sh` are synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten on next sync — make changes upstream.
