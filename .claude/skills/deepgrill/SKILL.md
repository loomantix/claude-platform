---
name: deepgrill
description: High-fidelity pre-push review chain — runs /refactorpass + /grill deep (full agent matrix). Use on complex or high-risk changes (auth/crypto, schema migrations, sync-propagating work, large refactors, anything that ships to consumers).
argument-hint: (none — operates on current branch's working tree + unpushed commits)
---

# /deepgrill — pre-push deep chain

You are running the high-fidelity pre-push review chain for a change that warrants more than the lean default. This is the manual escape hatch for the cases where the trimmed defaults aren't enough: load-bearing changes, sync-propagating updates, anything in `auth/`, `crypto/`, schema migrations, large refactors (>20 files), or skills/scripts/workflows that ship to consumer repos.

`/deepgrill` is a thin orchestrator — it runs the standard `/refactorpass` (single-pass) and `/grill deep` (full agent matrix), then emits the right post-push instruction so the developer follows up with `/reviewit <pr> deep`.

## When to use this instead of the default chain

The default (`/refactorpass` → `/grill` → push → `/reviewit <pr>`) is right for ~80% of PRs. Reach for `/deepgrill` when **any** of these apply:

- Touches `.claude/skills/**`, `scripts/sync*`, `.github/workflows/**` (sync-propagating)
- Touches authentication, crypto, secret handling, sensitive-data paths
- Schema migration or data-shape change
- Large refactor (>20 files modified or >500 lines net)
- Bug fix where the prior incident report (or commit graph) shows recurrence in the same area
- User explicitly asked "review this carefully" / "this is high-risk" / similar

If none of those apply, run the default chain instead — `/deepgrill` is ~3× the token cost.

---

## Phase 0: Pre-flight

1. **Verify on a feature branch** (not `main`/`master`/`staging`). If on a protected branch, refuse and ask the user to `git switch -c feat/...`.

2. **Verify there's something to grill** — `git rev-parse @{u}` matching HEAD with no working-tree diff means nothing local to review. Tell the user, exit cleanly.

3. **Triviality detection**: same heuristic as the underlying skills. If the diff is docs/config-only, tell the user `/deepgrill` adds no value over the default skip path and exit.

---

## Phase 1: Refactor pass

Invoke via the Skill tool: `Skill(skill="refactorpass")`.

This runs `/simplify` once and commits the result. Wait for it to return.

> ⚠️ **Do not stop after `/refactorpass` returns.** The sub-skill's prompt is self-contained; when control returns, immediately proceed to Phase 2. The chain is not done until Phase 3.

---

## Phase 2: Deep grill

Invoke via the Skill tool: `Skill(skill="grill", args="deep")`.

This runs the full agent matrix (`code-reviewer`, `silent-failure-hunter`, `type-design-analyzer`, `comment-analyzer`, `pr-test-analyzer`, `security-review`) — picking the agents whose signals appear in the diff. The user verifies findings interactively per `/grill`'s standard Phase 4.

> ⚠️ **Do not stop after `/grill deep` returns.** Same orchestration trap. Proceed to Phase 3.

---

## Phase 3: Hand-off message

Print:

```
✅ /deepgrill complete (refactor pass + deep grill).

Next steps for the deep chain:
  git push
  gh pr create --title "..." --body "..."
  /reviewit <pr-number> deep    # Gemini + Copilot 4-iter loop with early-exit, then a final /deepgrill on the PR
```

Do not push or open the PR — the developer takes the final action so they can compose the PR title/body deliberately.

---

## What this skill does NOT do

- **Does not push.** Same as `/grill` — the developer pushes after their own final review.
- **Does not invoke `/reviewit`.** Post-push review is a separate concern; the deep variant is `/reviewit <pr> deep`.
- **Does not silently override the user's verifications in `/grill`.** Each finding still requires fix/defer/ignore from the user.

---

## Source of truth

This skill lives upstream at `.claude/skills/deepgrill/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten — make changes upstream.
