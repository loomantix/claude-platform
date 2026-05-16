---
name: agent-loop
description: Autonomous Claude relay loop on top of /issues — claim a ready issue, spawn a fresh Claude session to work it, push results to a collection branch, repeat. Opens a summary PR at the end.
argument-hint: '[iterations] [collection-branch] [--resume]'
---

# /agent-loop

Run an autonomous Claude relay over the `/issues` ready queue. Each iteration claims an issue, spawns a fresh Claude session in `--permission-mode bypassPermissions`, lets it work, then pushes the result to a shared collection branch. After the loop, opens an `agent-loop: <branch>` PR with the closed-issues + commit-log summary.

## Usage

```bash
.claude/skills/agent-loop/scripts/agent-loop.sh [iterations] [collection-branch] [--resume]
```

Defaults: 10 iterations, auto-generated collection branch (`agent-loop-<timestamp>-<rand>`), ready-queue-only.

| Args                      | Behavior                                                        |
| ------------------------- | --------------------------------------------------------------- |
| `5`                       | 5 iterations, auto-generated branch, ready-only                 |
| `5 wasm-plugins`          | 5 iterations, named collection branch                           |
| `5 wasm-plugins --resume` | also pick up issues already assigned to `@me` (orphan-recovery) |
| `--help`                  | print the script header                                         |

## Prerequisites (per-repo, one-time)

1. **`agent-loop-instructions.md` at the repo root** — repo-specific agent instructions (codebase conventions, build commands, test invocation, deployment quirks). The Claude prompt that points Claude here is **consumer-owned** in `.claude/skills/agent-loop/prompt.txt`, bootstrapped from `prompt.txt.template` on first sync (`create_if_missing: true`). The default content — `Read @agent-loop-instructions.md and follow the instructions. Your assigned issue is #{ISSUE_ID}. Run 'gh issue view {ISSUE_ID}' to see the full description, then complete it.` — is the value the script falls back to when `prompt.txt` is absent, empty, or unreadable. Edit `prompt.txt` to customize what Claude is told before reading your instructions file. If `agent-loop-instructions.md` itself is missing, the script exits before claiming work.

   Both files are bootstrapped via `create_if_missing: true` (their respective templates live in `.claude/skills/agent-loop/`). After first creation, customize them for your repo; subsequent syncs leave them alone.

2. **`dev: agent` label + a triaged backlog** in the consumer's GitHub repo. The script picks **only** issues carrying `dev: agent` — without the label, an issue is invisible to the loop. This is a positive filter, not an exclusion: the operator must walk the backlog once and tag the agent-shaped subset, which keeps the loop from wandering into design / cross-repo / device-gated work. Create the label once per repo, then triage:

   ```bash
   gh label create "dev: agent" --description "Suitable for autonomous AI agent completion" --color 8B5CF6
   ```

   The rubric for tagging (used by Loomantix consumers): bounded scope; verifiable success via tests / CI / deterministic check; no design / copy / strategy decisions; no platform credential gates (Play Console, App Store Connect, EAS); no physical device gate; no unresolved upstream blocker. If an issue fails any of those, leave it untagged.

3. **`gh`, `jq`, `xxd`, `python3`, `claude`** on `PATH`. The script hard-fails if any are missing.
4. **`/issues` skill synced** — the script invokes `.claude/skills/issues/scripts/ready.py --json` to enumerate the queue. Without it the script exits at startup.

## Behavior per iteration

1. Sync the worktree with `origin/<collection-branch>` — fetches and fast-forwards. If the remote was force-pushed, cherry-picks the local commits onto the new tip (with a pre-reset SHA snapshot so a failed cherry-pick restores the original chain rather than leaving partial replay). Genuine merge conflicts fail loud; the eventual push surfaces persistent ones via the `PUSH_FAILURES` counter.
2. Pick a work item: with `--resume`, prefer any open `dev: agent` issue already assigned to `@me`; otherwise the first dependency-free row from `ready.py --agent --json`. The `dev: agent` label is required on both paths — an empty queue means the backlog hasn't been triaged yet, not that there's no work to do.
3. Claim by adding `@me` as assignee. Re-fetch immediately afterward — if there are >1 assignees, a parallel worker raced; release and try the next row.
4. Spawn `claude --chrome --permission-mode bypassPermissions --print "$PROMPT" --output-format stream-json` and stream the events through `jq` for colored display. The Claude PID is tracked so `Ctrl-C` interrupts the loop cleanly. **Prompt source:** the prompt is read from the consumer-owned `.claude/skills/agent-loop/prompt.txt` (bootstrapped from the upstream `prompt.txt.template` on first sync via `create_if_missing: true`); the script falls back to a baked-in literal when that file is absent, empty, or unreadable. **Security posture:** the `claude` invocation block is bracketed by `# claude-cli-invocations:start` / `:end` markers and locked in `.claude/claude-cli-invocations.allowlist` (allowlist entries bind to a specific file path, not just the hash); the upstream CI lint at `.claude/lint-claude-cli-invocations.py` fails on any unrecognized hash and rejects sensitive tokens outside any marker. **Adding or rotating an allowlist entry requires byte-level review of the new locked region in the same PR** — the hash by itself is opaque, the diff is the audit trail. The prompt template itself is covered by `.claude/lint-skill-content.py`'s weaponization rules (fetch-and-execute, exfil sinks, off-allowlist URLs, etc.).
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
