---
name: refactorpass
description: Pre-push refactor pass — runs /simplify once on local changes and commits the result
argument-hint: (none — always single-pass)
---

# Refactor pass — pre-push refactor wrapper

You are running a refactor pass on the current branch's local changes BEFORE the developer pushes to origin. This is the right time to refactor: the code is fresh, nothing is public, scope-creep concerns don't apply, and consolidation/simplification opportunities are cheapest to act on.

The skill wraps Claude Code's built-in `/simplify` and adds two things `/simplify` doesn't do on its own: triviality skip on docs/config-only changesets, and a single commit with a clear refactor-pass message.

## Core principles

- **Refactor freely.** Unlike post-PR review where scope creep is a real cost, here every consolidation lands as a normal commit in the eventual PR's history. Three similar lines → helper. Repeated 5-line block → extracted function. Dead code → deleted.
- **One pass only.** A second `/simplify` pass on the same changeset has been validated empirically to add negligible value over the first; dropped to keep token cost honest.
- **Commit the result.** A single `refactor: /simplify pass — ...` commit. Skipped cleanly if `/simplify` made no changes.

---

## Phase 0: Pre-flight

1. **Check git state**:

   ```bash
   git rev-parse --abbrev-ref HEAD              # branch name
   git rev-parse HEAD                            # current HEAD SHA
   git status --porcelain                        # any uncommitted changes
   ```

2. **If there are uncommitted changes**: ask the user whether to commit them first or include them in the refactor pass. Don't silently sweep them into a refactor commit — the user may have intended them to be a separate logical commit.

3. **If branch is `main` / `master` / `staging`**: refuse. Refactor passes belong on feature branches. Ask the user to `git switch -c feat/...` first.

4. **If HEAD is identical to upstream** (`git rev-parse @{u}` matches HEAD when an upstream exists): the branch has nothing local to refactor. Tell the user, exit cleanly.

5. **Triviality detection — skip /simplify if there's no code to refactor.** Compute the changed-file list against the base branch (or `@{u}` if upstream is set) and classify:

   ```bash
   git diff --name-only "$(git merge-base @{u} HEAD)..HEAD" 2>/dev/null \
     || git diff --name-only HEAD     # fallback when no upstream is set
   ```

   Classify each file as:
   - **Source code**: `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`, `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`
   - **Docs / config / fixtures**: `.md`, `.txt`, `.yml`, `.yaml`, `.json`, `.toml`, `.gitignore`, `.gitattributes`, `LICENSE`, `CHANGELOG`, `README`, `.env.example`, files under `docs/`, `*.fixture.*`, snapshot files
   - **Other**: anything not in either bucket (treat as source for safety)

   **If the changeset contains zero source-code files**, skip Phase 1 entirely. /simplify on docs/config-only changes is theatre — there's nothing to refactor. Jump straight to Phase 2 (summary).

   For mixed changesets (some source, some docs), proceed to Phase 1 — /simplify will only act on the source files anyway.

---

## Phase 1: Single `/simplify` pass

1. **Capture pre-snapshot**:

   ```bash
   git rev-parse HEAD > /tmp/refactorpass-pre-sha
   git diff HEAD > /tmp/refactorpass-pre-diff
   ```

2. **Invoke `/simplify`** via the Skill tool: `Skill(skill="simplify")`. Let it run — it operates on recently-modified code by default.

3. **Capture post-snapshot**:

   ```bash
   git rev-parse HEAD > /tmp/refactorpass-post-sha
   git diff HEAD > /tmp/refactorpass-post-diff
   ```

4. **Decide**:
   - If `pre-diff` and `post-diff` are byte-identical AND HEAD SHA is unchanged: `/simplify` made no changes. Nothing to commit.
   - If HEAD moved (`/simplify` committed on its own): nothing more to do — `/simplify` already committed.
   - If working tree diff differs but HEAD did NOT move: stage and commit. **Check for staged changes first** — `git diff --cached --quiet` returns non-zero only if there are staged changes. If there's nothing to commit (e.g., `/simplify` reverted its own changes during the run), skip the commit step.

     ```bash
     git add -A
     if ! git diff --cached --quiet; then
       git commit -m "refactor: /simplify pass — <one-line summary>"
     fi
     ```

     Write the one-line summary based on the files touched. Examples: `"consolidate config-loader error paths into helper"`, `"remove unused imports across api worker module"`, `"extract repeated input-validation block into shared util"`. If the changes touched many unrelated areas, use `"multiple cleanup spots — see diff"`.

---

## Phase 2: Summary

Tell the user:

```
✅ Refactor pass complete.
- Changes made: <yes/no>
- New commit: <"refactor: /simplify pass — ..." or "(none)">
- HEAD: <SHA>

Next: `git push`.
After push and PR creation: invoke `/reviewit <pr-number>` (or `/reviewit <pr> deep` for the 4-iter chain that ends with a final `/deepgrill`) to orchestrate AI review.
```

---

## What this skill does NOT do

- **Does not push.** That's the developer's call (or the parent Claude session's call).
- **Does not open the PR.** Same reason.
- **Does not run `/review`, `/security-review`, Gemini, or Copilot.** Those are post-push concerns handled by `/reviewit`.
- **Does not respect `copilot-instructions.md`.** `/simplify` has its own prompt that's refactor-positive — that's the whole point of running it pre-push, before the bot reviewers' scope-control rules kick in.

---

## Source of truth

This skill lives upstream at `.claude/skills/refactorpass/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten on the next sync — make all changes upstream.
