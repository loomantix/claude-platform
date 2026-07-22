---
name: codex-review
description: Independent second-opinion review of a PR or local diff via the Codex CLI. Read-only by design — Codex acts as a fresh adversarial reviewer (a different model family from the Claude review chain) and reports findings to verify against the code. Pairs with /deepgrill as the cross-review step; `verify` arg lets Codex run tests/build.
argument-hint: (optional "<pr-number>" and/or "verify" — verify lets Codex run tests/build; default is a read-only review of the local diff)
---

# /codex-review — independent Codex cross-review

You are getting a **second opinion** on a change from the [Codex CLI](https://github.com/openai/codex), run locally. Codex is a different model family from the Claude review chain (`/grill`, `/deepgrill`), so it catches a genuinely independent set of issues — the value is the disagreement, not the agreement. The canonical use is the cross-review step **after `/deepgrill`** and before merge: Claude-side agents grill the change, then Codex looks at the same diff cold.

Codex runs **read-only by default** — it can read the tree and reason, but cannot modify files, so it is a safe reviewer. This skill never lets Codex edit code. Findings come back to _you_; you verify each against the source and fix only the confirmed ones.

## When to use

- After `/deepgrill` on a high-risk change (auth, crypto, migrations, sync-propagating files), as the independent second pass before merge.
- Standalone, when you want a fresh cold read of a PR or local diff.
- Skip on docs/config-only changesets — there is nothing for an adversarial reviewer to find.

## Phase 0: Pre-flight

1. **Codex must be installed and authenticated.** Check:

   ```bash
   command -v codex && codex --version
   ```

   If it is missing, stop and tell the user to install and authenticate the Codex CLI (`npm i -g @openai/codex`, then `codex login`) — this skill cannot proceed without it. Authentication is machine-level, so once it is set up any session can use it.

2. **Resolve scope and the diff range.** This is the step people get wrong in a worktree — auto-diff resolves the wrong branch, so compute the range explicitly.
   - `$ARGUMENTS` may contain a PR number and/or the word `verify`. Parse both (order-independent, case-insensitive).
   - **PR number given:** fetch and check out the PR's head (`gh pr checkout <n>` in a clean tree, or fetch its branch), and read its base with `gh pr view <n> --json baseRefName`.
   - **No PR number:** review the local branch against its base (`origin/main` or the repo's default branch).
   - Compute the merge base and capture the authoritative tracked and untracked scope:

     ```bash
     BASE=origin/<base-branch>
     git fetch origin <base-branch> --quiet
     MB=$(git merge-base "$BASE" HEAD)
     git diff --stat "$MB"         # committed, staged, and unstaged tracked changes
     git status --short
     git ls-files --others --exclude-standard
     ```

   - If the changeset is docs/config-only, say so and exit — nothing to review.

## Phase 1: Build the review prompt

Write a tight, scoped prompt. A vague "review this" wastes the run; name the files and the riskiest failure modes. Include:

- One line on what the repo is, and — **if the repo is or may become public — an instruction to never print secrets, ARNs, account ids, or hostnames** in its output.
- 2–3 lines on what the change does.
- The tracked diff to read (`git diff <MB>`) plus the untracked paths from `git ls-files --others --exclude-standard`, and an instruction to **read the actual source, not just the diff**.
- The 3–4 riskiest things about this specific change ("attack these").
- The output contract: **only high-confidence material findings** (correctness, security, data-loss); for each, `file:line`, severity, concrete issue, concrete fix; "no material findings" if clean; be terse.

## Phase 2: Run Codex (read-only, streaming)

Run the dedicated non-interactive reviewer. **Flags verified against `codex-cli 0.145.0`** — the CLI surface drifts, so check both `codex exec --help` and `codex exec review --help` after an upgrade. Use `codex exec review`, not the top-level `codex review`, because the `exec` form exposes model, output-file, and automation controls. There is no `--ask-for-approval` or `--full-auto` flag on this subcommand. The flags that matter, and four traps:

- `-c 'sandbox_mode="read-only"'` — `exec review` no longer exposes `--sandbox`; set the equivalent config explicitly so Codex cannot touch the tree. (Modes: `read-only` · `workspace-write` · `danger-full-access`.)
- `--skip-git-repo-check` — lets it run in a worktree / subdir without complaining.
- `-o <file>` (`--output-last-message`) — writes **only** Codex's final message (the findings) to its own file. Without it you have to dig the findings out of the bottom of a huge stream that also echoes the prompt, `AGENTS.md`, and every file Codex auto-read.
- **Trap 1 — never pipe through `tail`/`head`.** They buffer until the process exits, so a multi-minute run looks hung with zero output. Redirect straight to a file.
- **Trap 2 — contention.** Many parallel Codex runs (across sessions) rate-limit each other and slow down. Prefer one at a time.
- **Trap 3 — scope selectors conflict with custom prompts.** In 0.145.0, `--base`, `--commit`, and `--uncommitted` cannot be combined with `[PROMPT]`, despite the generated usage line showing both. The prompt already names the authoritative merge-base scope, so do not add a selector. Keep `</dev/null` so generic `exec` stdin-append behavior cannot block or contaminate the prompt.
- **Trap 4 — `pkill -f` matches the shell doing the killing.** A command such as `pkill -f "codex exec"` also matches the shell running it. Instead:
  - **Never put a kill and a relaunch in one bash invocation.** Kill, confirm, then start the new run separately.
  - Kill the exact PID printed by the launcher below. Use `pgrep -f "[c]odex exec review"` only to identify a lost PID.
  - **Check who else is running Codex first** (`pgrep -af codex`). A developer or another agent session may have a long Codex turn open on the same machine; a broad `-f codex` pattern takes theirs down with yours.

```bash
RUN_ID="$(date +%s)-$$"
CODEX_FINDINGS="/tmp/codex-findings-${RUN_ID}.md"
CODEX_LOG="/tmp/codex-full-${RUN_ID}.out"
CODEX_STATUS="/tmp/codex-status-${RUN_ID}.txt"
(
  status=0
  child_pid=""
  terminate_child() {
    [[ -z "$child_pid" ]] || kill -TERM "$child_pid" 2>/dev/null || true
  }
  trap terminate_child HUP INT TERM
  codex exec review \
    -c 'sandbox_mode="read-only"' \
    --skip-git-repo-check \
    -o "$CODEX_FINDINGS" "$REVIEW_PROMPT" \
    </dev/null >"$CODEX_LOG" 2>&1 &
  child_pid=$!
  wait "$child_pid" || status=$?
  trap - HUP INT TERM
  printf '%s\n' "$status" >"$CODEX_STATUS"
  exit "$status"
) &
CODEX_PID=$!
printf 'pid=%s\nfindings=%s\nlog=%s\nstatus=%s\n' \
  "$CODEX_PID" "$CODEX_FINDINGS" "$CODEX_LOG" "$CODEX_STATUS"
```

Run it in the background and retain the four printed values. Tail the log for liveness. The status file appears only after exit and contains Codex's exit code; use it as the completion check because 0.145.0 does not consistently print the old `tokens used` marker. Read the findings file only after a zero status. Codex may load its own review skills, so a broad prompt can still trigger a thorough, slow pass.

**Runtime defaults (why it is slow).** Codex commonly defaults to a frontier model at high reasoning effort. For a faster pass, add `-c model_reasoning_effort=medium` (or `-m <model>`), and for a small diff tell Codex to do one focused pass and read only the named files.

### `verify` mode (opt-in)

If `$ARGUMENTS` contains `verify`, the user wants Codex to also **run the tests/build** to confirm findings. Use the same launcher, but replace the sandbox config with:

```bash
-c 'sandbox_mode="workspace-write"'
```

`workspace-write` lets Codex write within the repo (run tests, build) but it cannot escape the working directory or reach arbitrary network. **Never use `--dangerously-bypass-approvals-and-sandbox` (`--yolo`, = `danger-full-access` + no approvals) for a review** — it removes the sandbox entirely (full write + network + command execution), defeating the point of a read-only reviewer. Reserve yolo for a deliberate _fix_ workflow, never this skill.

## Phase 3: Relay and verify the findings

When the status file appears with exit code zero, read the findings file — it holds just Codex's final message, no need to dig through the stream. Treat the findings as a **second opinion, not a verdict**:

- For each finding, verify it against the actual source before acting — Codex can be confidently wrong, just like any reviewer.
- Present a deduplicated list to the user with `file:line`, severity, and Codex's suggested fix, plus your own one-line take (confirm / dispute, with evidence).
- If this followed `/deepgrill`, call out where Codex **disagreed with or added to** the Claude-side findings — that delta is the whole reason to run it.

## Phase 4: Disposition

Fix only **confirmed** findings (default: fix now, in this PR). Dismiss false positives with a one-line rationale. For a finding that needs a human/scope/legal decision (risk acceptance, prod-data assumptions, an architectural rework), fix what you safely can and **flag the rest for the user** rather than guessing. Re-run the relevant gates after any fix.

This skill **does not merge and does not push** — it produces verified findings and (optionally) fixes; the push/merge is the developer's call.

## Output

End with:

```
✅ /codex-review complete (mode: <read-only | verify>).
- Scope: <PR #N | local branch> vs <base>  (tracked diff from <MB> plus listed untracked files)
- Codex findings: <total> (<confirmed>/<disputed>/<needs-human-decision>)
- Fixed: <count>  ·  Dismissed: <count>  ·  Flagged for you: <count>
- Findings: /tmp/codex-findings-<run-id>.md  ·  full log: /tmp/codex-full-<run-id>.out
```

If run as a cross-review after `/deepgrill`, add one line on the Claude-vs-Codex delta (what Codex caught that the Claude agents did not, or vice versa).

## Source of truth

This skill lives upstream at `.claude/skills/codex-review/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten — make changes upstream.
