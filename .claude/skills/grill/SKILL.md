---
name: grill
description: Pre-push critical pass — invokes adversarial sub-agents on local changes. Lean default runs code-reviewer + silent-failure-hunter; `deep` arg restores the full agent matrix (type-design, comments, tests, security).
argument-hint: (optional "deep" — runs full agent matrix; default is the 2 highest-signal agents)
---

# /grill — pre-push critical review

You are running a deliberately adversarial pass on the developer's local changes after `/refactorpass` has cleaned them up but **before** the push and PR creation. The goal: catch issues that bot reviewers (`/reviewit` post-push) and human reviewers will flag, fix them now while it's still local, and require explicit user verification before the push.

This skill is the deliberate adversarial pass between "I think this is ready" and `git push` — every push to origin should have been grilled.

## Mode resolution

Read `$ARGUMENTS`. If it equals `deep` (case-insensitive, ignore surrounding whitespace), run in **deep mode** (full agent matrix per Phase 1's table). Otherwise run in **lean mode** (default — only the two highest-signal agents). Deep mode is normally invoked by `/deepgrill`, not directly.

## Core principles

- **Adversarial stance.** Unlike `/simplify` (which is constructive — find consolidations and simplifications) and `/refactorpass` (the wrapper that runs `/simplify`), `/grill` is critical. Look for what's wrong, what's missing, what could break, what reviewers will catch.
- **Skip on docs-only.** Same triviality heuristic as `/refactorpass` and `/reviewit`. Theatre is bad.
- **User verification is required.** Findings are presented to the user with explicit per-finding choices. The skill does not silently apply fixes or auto-dismiss.

---

## Phase 0: Pre-flight + triviality

1. **Triviality detection** (same heuristic as `/refactorpass` Phase 0 step 5):

   ```bash
   git diff --name-only "$(git merge-base @{u} HEAD)..HEAD" 2>/dev/null \
     || git diff --name-only HEAD
   ```

   Classify by extension. If only docs/config files changed (`.md`, `.txt`, `.yml`, `.yaml`, `.json`, `.toml`, `.gitignore`, `LICENSE`, `CHANGELOG`, `README`, `docs/**`, `*.fixture.*`):

   ```
   🟢 Skipping grill — docs/config-only changeset. Nothing for adversarial sub-agents to find.
   ```

   Exit cleanly. The user can push directly.

2. **Snapshot HEAD**:

   ```bash
   GRILL_HEAD=$(git rev-parse HEAD)
   ```

---

## Phase 1: Pick relevant adversarial sub-agents

### Lean mode (default)

Run **at most two** agents:

| Signal in diff                                                               | Agent                                     |
| ---------------------------------------------------------------------------- | ----------------------------------------- |
| Always (source code present)                                                 | `pr-review-toolkit:code-reviewer`         |
| Try/catch, error-handling, fallback logic, async paths, swallowed exceptions | `pr-review-toolkit:silent-failure-hunter` |

Skip the others — they're available on `/deepgrill`. If the diff has none of the silent-failure signals, run only `code-reviewer`. If you're tempted to run more agents because the change "feels load-bearing," that's the signal to suggest `/deepgrill` to the user instead and exit.

### Deep mode (`/deepgrill` invocation)

Run the full agent matrix:

| Signal in diff                                                                                 | Agent                                     |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------- |
| Always (source code present)                                                                   | `pr-review-toolkit:code-reviewer`         |
| Try/catch, error-handling, fallback logic, async paths                                         | `pr-review-toolkit:silent-failure-hunter` |
| New types, modified type definitions, generics changes                                         | `pr-review-toolkit:type-design-analyzer`  |
| New large doc comments / docstrings, JSDoc on exports                                          | `pr-review-toolkit:comment-analyzer`      |
| Auth, crypto, regulated-data handling (PHI/PII/PCI/secrets), input validation, SQL composition | Built-in `/security-review` skill         |
| New tests, modified test scope on a fix/feature PR                                             | `pr-review-toolkit:pr-test-analyzer`      |

Pick the signals present in the diff — don't run every agent on every PR. Two to five agents is typical in deep mode.

---

## Phase 2: Run agents in parallel

Use the Agent tool with explicit `subagent_type`. Pass the diff as input so the agent doesn't have to discover scope. Run them in a single message (parallel execution).

Example:

```
Agent(
  description="grill: silent-failure scan",
  subagent_type="pr-review-toolkit:silent-failure-hunter",
  prompt="Review the following committed-but-unpushed changes for silent failures, swallowed exceptions, fallback logic that masks bugs, missing error propagation. Diff:\n\n<paste git diff @{u}...HEAD>\n\nReport findings as a numbered list with severity (critical/suggestion/nitpick) and file:line references. Under 300 words.",
)
Agent(
  description="grill: code-quality scan",
  subagent_type="pr-review-toolkit:code-reviewer",
  prompt="Review the following committed-but-unpushed changes against project conventions in CLAUDE.md. Diff:\n\n<paste>\n\nReport findings as a numbered list with severity and file:line references. Under 300 words.",
)
# (more agents as appropriate)
```

Agents will return their findings in their final result messages.

---

## Phase 3: Aggregate findings

Combine outputs into a single deduplicated list. Two findings are duplicates if they reference the same file + line range (±5 lines) or describe the same issue semantically. Sort by severity (critical → suggestion → nitpick).

Present a compact table to the user:

```
## /grill findings — N critical, M suggestion, P nitpick

| # | Severity   | File:line              | Agent              | Finding                          |
|---|------------|------------------------|--------------------|----------------------------------|
| 1 | critical   | apps/foo/bar.ts:42    | silent-failure     | Catch swallows error w/o logging |
| 2 | suggestion | apps/foo/bar.ts:88    | code-reviewer      | Missing return type on export    |
| 3 | nitpick    | docs/X.md:12          | comment-analyzer   | Stale code reference             |
```

If zero findings: skip to Phase 5. The user can push.

---

## Phase 4: User verification (interactive)

For each finding, ask the user to choose:

- **F (fix now)**: Claude applies the fix. The fix becomes a new commit (`fix: address /grill finding N — <summary>`).
- **D (defer)**: tracked in PR description after push. Claude appends a "Pre-push grill — deferred" section to the PR body when `/reviewit` runs.
- **I (ignore)**: dismissed as a false positive. **Critical findings cannot be ignored without an explicit one-line rationale** that gets recorded in the commit / PR description.

Process findings one at a time, lowest-numbered first. Do not batch — the user is verifying each.

After all findings handled, ask:

> "Ready to push? After push and PR creation, run `/reviewit <pr-number>`. [Y/n]"

If N: stop here. The user takes over (maybe wants to keep iterating manually).

---

## Phase 5: Summary

```
✅ /grill complete (mode: <lean | deep>).
- Agents run: <list>
- Findings: <total> (<critical>/<suggestion>/<nitpick>)
- Fixed: <count>  ·  Deferred: <count>  ·  Ignored: <count>
- New commits: <list>
- HEAD: <SHA>

Next:
  git push
  gh pr create --title "..." --body "..."
  /reviewit <pr-number>          # lean (Gemini + Copilot, 2 iters)
  /reviewit <pr-number> deep     # deep (Gemini + Copilot, 4 iters w/ early-exit + final /deepgrill)
```

If lean mode was used and the changeset touches load-bearing surfaces (auth, crypto, schema migrations, sync mechanism, sync-propagating files under `.claude/skills/**` or `scripts/sync*`), append:

```
ℹ️  This change touches load-bearing surfaces. Consider re-running via /deepgrill
    for the full agent matrix (type-design, comments, tests, security) before push.
```

If any findings were ignored without rationale, refuse to print the "ready to push" line — surface what's missing.

---

## What this skill does NOT do

- **Does not push.** That's the developer's call after they've verified.
- **Does not invoke `/reviewit`.** Three reviewers post-push is a separate concern.
- **Does not silently apply fixes.** Every "fix now" requires the user choice.

---

## Source of truth

This skill lives upstream at `.claude/skills/grill/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten — make changes upstream.
