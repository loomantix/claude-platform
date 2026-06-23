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
- **Fix-everything-valid bias.** The default for every valid finding is **fix now, in this PR**. Dismiss only what's invalid (wrong, false-positive, based on a misread of the diff, or would make the code worse). Defer only when the fix is a major architectural rework — roughly 300+ lines or a cross-cutting redesign — and in that case file a GitHub issue rather than letting it sit as an undocumented todo. The reason: every valid finding that ships becomes the floor for the next PR in this area. Letting them accrue as "deferred" turns the backlog into review noise and makes future grills more expensive.
- **Skip on docs-only.** Same triviality heuristic as `/refactorpass` and `/reviewit`. Theatre is bad.
- **User verification on outcomes, not on whether to act.** Findings are presented and the user confirms the proposed disposition (fix / dismiss-as-invalid / defer-as-architectural-issue). The default offered for each finding is the fix-everything-valid rule above, not a neutral "what do you want to do?"

---

## Phase 0: Pre-flight + triviality

### 0a. Context-window check (do this BEFORE anything else)

`/grill` spawns adversarial sub-agents (Agent tool, `pr-review-toolkit:*`). Each sub-agent gets its own prompt-cache state derived from this session's context. If this session has been heavily used for feature implementation — long conversation, lots of file edits, dense planning — the cache is already spent on context the sub-agents don't need, and their effective working window shrinks accordingly. Deep grill is hit hardest: it spawns up to six agents in parallel.

Before proceeding, assess honestly:

- Has this session been writing/editing the feature about to be grilled? Long conversation, many tool calls, dense edit history?
- Is the conversation about to brush against auto-compaction territory?

If **either is yes**, STOP and tell the user:

> Your context is heavy from the implementation work. Start a new Claude session and run `/grill` (or `/deepgrill`) there — sub-agents need cache headroom, and a fresh session makes the chain materially cheaper. This matters even more for `/deepgrill`, which spawns up to six agents.

Do not proceed in the current session unless the user explicitly overrides.

### 0b. Standard pre-flight + triviality

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

If the diff touches **customer/tenant-variable behavior** (vendor/third-party integrations, per-tenant config, prompt/output generation, data normalization), recommend `/deepgrill` — the **tenant-coupling lens** that catches one customer's data/config hardcoded into shared logic lives in deep mode (see below), not in the lean two-agent set.

### Deep mode (`/deepgrill` invocation)

Run the full agent matrix:

| Signal in diff                                                                                                                      | Agent                                                                |
| ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Always (source code present)                                                                                                        | `pr-review-toolkit:code-reviewer`                                    |
| Try/catch, error-handling, fallback logic, async paths                                                                              | `pr-review-toolkit:silent-failure-hunter`                            |
| New types, modified type definitions, generics changes                                                                              | `pr-review-toolkit:type-design-analyzer`                             |
| New large doc comments / docstrings, JSDoc on exports                                                                               | `pr-review-toolkit:comment-analyzer`                                 |
| Auth, crypto, regulated-data handling (PHI/PII/PCI/secrets), input validation, SQL composition                                      | Built-in `/security-review` skill                                    |
| New tests, modified test scope on a fix/feature PR                                                                                  | `pr-review-toolkit:pr-test-analyzer`                                 |
| Customer/tenant-variable behavior: vendor/third-party integrations, per-tenant config, prompt/output generation, data normalization | `pr-review-toolkit:code-reviewer` (tenant-coupling pass — see below) |

Pick the signals present in the diff — don't run every agent on every PR. Two to five agents is typical in deep mode.

### Tenant-coupling lens (deep mode)

When the diff touches behavior that varies by customer, run a dedicated `code-reviewer` pass whose only job is to catch **_hardcoding the instance instead of the class_** — a value that varies by customer (one tenant's vocabulary, a customer's config, a specific user-reported string) frozen into shared logic as a literal or special-case branch. This is the defect class the other lenses structurally miss: such a hardcode is correct, type-safe, regulated-data-clean, and testable, yet ships the wrong feature. With only one live customer it is indistinguishable from a working fix in production.

The agent's test for each flagged literal: _would this code still be correct for a second customer with different values?_ If no, the value belongs in config/data with a safe default, not in `src/`. (Illustrative shape: a fix that special-cases one customer by name — `if (tenantId === 'acme')` — or hardcodes a list of one customer's category labels to handle how that customer happens to name things. It generalizes to nothing and patches the symptom instead of the cause.)

```
Agent(
  description="grill: tenant-coupling scan",
  subagent_type="pr-review-toolkit:code-reviewer",
  prompt="Scan this diff ONLY for tenant-coupling: literals or branches that encode one specific customer's data/config/vocabulary into shared logic — e.g. `tenantId === 'acme'`, a hardcoded list of one customer's category/term labels, per-customer special-cases. For each, ask: would this be correct for a SECOND customer with different values? If no, it belongs in config/data with a safe default, not in code. Ignore genuinely universal values (protocol constants, standard enums, framework keys). Report findings with severity (critical/suggestion/nitpick) + file:line and a one-line 'move to config' suggestion. Diff:\n\n<paste git diff @{u}...HEAD>\n\nUnder 250 words.",
)
```

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

Apply the **fix-everything-valid bias** from Core principles. For each finding, propose a disposition and ask the user to confirm or override:

- **Default disposition for valid findings: fix now.** Claude applies the fix in this PR as a new commit (`fix: address /grill finding N — <summary>`). This is the default — do not present "defer" as an equally weighted option for ordinary findings.
- **Dismiss-as-invalid**: propose this only when the finding is wrong, a false positive, based on a misread of the diff, or would make the code worse. Record the dismissal rationale in one line so it's not re-raised in `/reviewit`.
- **Defer-as-architectural-issue**: propose this only when the fix is a major architectural rework (roughly 300+ lines or a cross-cutting redesign). In that case file a GitHub issue **now** (`gh issue create`) capturing the finding + file/line evidence + rationale, and reference the issue number in the PR body. A "deferred" finding without a filed issue is not allowed — the whole point is to keep the implicit todo backlog from growing.

For each finding, present it like:

```
Finding N (severity: …) — apps/foo/bar.ts:42 — silent-failure
  Proposed: FIX (default for valid findings)
  [Enter] confirm  ·  [d] dismiss-as-invalid (requires rationale)  ·  [i] defer-as-architectural (requires issue)
```

Process findings one at a time, lowest-numbered first. Do not batch — the user is verifying each. Override is one keypress; the bias is built into which option is the default, not into removing the user's choice.

After all findings handled, ask:

> "Ready to push? After push and PR creation, run `/reviewit <pr-number>` (preferably in a fresh Claude session — see Phase 5 handoff). [Y/n]"

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

ℹ️  Run /reviewit in a FRESH Claude session.
   This session's context has just absorbed the grill output + any fix commits.
   /reviewit (especially `deep`) drives multiple Gemini/Copilot iterations and a final /deepgrill,
   each of which benefits from cache headroom. A fresh session for /reviewit is materially cheaper.
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
