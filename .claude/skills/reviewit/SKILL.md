---
name: reviewit
description: Post-push AI review orchestrator. Both modes fire Gemini Flash + Copilot at the same iteration watermark with staggered handling (Gemini first, Copilot folded in) — no in-skill /review. Lean caps at 2 iterations. `deep` arg bumps the cap to 4, early-exits when an iteration produced no fix-pushes, and runs a final /deepgrill so fresh agents look at the PR's current state.
argument-hint: PR number (e.g., 42), optionally followed by "deep" (e.g., 42 deep) for the 4-iter chain + final /deepgrill
---

# reviewit — post-push AI review cycle

You are orchestrating the post-push AI review cycle for an open pull request.

**Lean mode (default)**: Two reviewers are fired at the same iteration watermark — Gemini Flash and GitHub Copilot — but handling is staggered. Act on Gemini as soon as it returns, push those fixes, then collect Copilot when it finishes before starting the next iteration. Cap is **2 iterations**.

**Deep mode (`deep` arg)**: Same two reviewers as lean — Gemini Flash + Copilot, same staggered fast/delayed handling. No in-skill `/review`. Two behavioral changes vs lean:

1. **4-iter cap with early-exit on no-fix iters.** If an iteration produced no `fix` resolutions across either the Gemini fast pass or the Copilot delayed pass (everything was deferred or dismissed, or there were no findings), exit the loop — re-firing reviewers on an unchanged HEAD just re-posts the same findings.
2. **Final `/deepgrill` on the PR.** After the loop exits for any reason (clean, early-exit, OR iter cap), invoke `Skill(skill="deepgrill")`. This runs `/refactorpass` + `/grill deep` (the full Claude-side agent matrix) as a separate sub-skill — fresh agents looking at the PR's current state. This replaces the older deep-mode `/review` fire that ran _inside_ Phase 1 and routinely caused the orchestrator to drop out of the polling loop early.

In both modes, the `/grill` pre-push pass already covered Claude-side adversarial agents on the pre-push tree — so post-push the orchestrator delegates Claude-side review to a single post-loop `/deepgrill` (deep mode only) rather than re-running anything inline.

This replaces the older `/review-cycle` skill. Auto-trigger of Gemini and Copilot is intentionally disabled — `/reviewit` is the only path that fires AI review.

## Mode resolution

`$ARGUMENTS` is whitespace-tokenized. The first token is the PR number; if a second token exists and equals `deep` (case-insensitive), set `MODE=deep` and `MAX_ITERS=4`. Otherwise `MODE=lean` and `MAX_ITERS=2`. Surface the resolved mode in the Phase 6 summary.

## Core principles

- **Full auto by default**: once the PR number is provided, do not ask for confirmation between phases. Fix everything fixable, defer what isn't, dismiss false positives. Present the summary at the end.
- **Reviewers are complementary**: each catches things the other misses. Unique findings are the primary value. Overlap is acknowledged in replies but not dwelt on.
- **Deduplicate before acting**: don't fix the same thing twice. For staggered Copilot, dedupe against both reviewer findings and the code already changed by the Gemini fix commit.
- **Reply to every comment** _after_ the push has produced the real commit SHA: for fixes, post the commit SHA; for deferrals, link the tracking issue; for dismissals, record the rationale. Replies happen in Phase 4 step 4, not in Phase 3 — Phase 3 only records resolutions to `/tmp`. The reply step is the most-skipped step in this skill; do not fold it into "commit + push" or treat it as optional.
- **Cap at `MAX_ITERS` review iterations**: each iteration is `(fire reviewers → handle Gemini fast pass → push/reply → handle Copilot → push/reply → loop check)`. After `MAX_ITERS`, stop the loop and proceed to Phase 5.5 (deep) or Phase 6 (lean). The reply step is part of an iteration's completion criteria — an iteration that pushes fixes but doesn't post replies is incomplete. If a PR needs more than the cap, it's signaling something deeper (scope too large, or repeated regressions).
- **No per-iter `/refactorpass`**: review-fix commits push directly. The base was refactor-passed pre-push, and re-running `/simplify` on small surgical fixes has been validated to add negligible value.

---

## Phase 0: Initialization

**Argument**: `$ARGUMENTS` — first token is the PR number; optional second token `deep` enables deep mode (see "Mode resolution" above).

1. **Validate PR number** (numeric, > 0). If missing, ask the user for it. Resolve `MODE` and `MAX_ITERS` per the rule above.

2. **Fetch PR details**:

   ```bash
   gh pr view <pr-number> --json number,title,headRefName,baseRefName,state,files,mergeable
   ```

3. **Check PR is open**. If closed/merged/draft-without-explicit-confirmation, notify and exit.

4. **Confirm the head ref is checked out locally** (`git rev-parse --abbrev-ref HEAD` matches `headRefName`). If not, the skill cannot push fixes — surface and exit.

5. **Triviality detection — prompt to skip the chain on docs/config-only PRs.** Inspect the PR's changed files and classify by extension (same heuristic as `/refactorpass` and `/grill` Phase 0):

   ```bash
   gh pr view <pr-number> --json files --jq '.files[].path'
   ```

   Classify each path. If only docs/config files (`.md`, `.txt`, `.yml`, `.yaml`, `.json`, `.toml`, `.gitignore`, `.gitattributes`, `LICENSE`, `CHANGELOG`, `README`, files under `docs/`, `*.fixture.*`, snapshot files), prompt the user **before** spending any reviewer budget:

   ```
   This PR looks docs/config-only — N files, no source code changes.
   Gemini Flash costs $0.05–$0.20 even on near-empty diffs.

   How to proceed?
     [C] Run the full chain anyway. Pick this if you specifically want
         Gemini's eyes on the doc content.
     [S] Skip everything — just merge.
   ```

   - **C**: proceed to Phase 1 normally.
   - **S**: exit cleanly. Print a summary noting nothing was run.

   For mixed changesets (some source, some docs), run the full chain (Phase 1 onward) without prompting — source files justify the spend.

6. **TodoWrite**: create tasks per iteration for "fire reviewers", "parse + dedup (Gemini fast pass)", "address findings (record resolutions)", "commit + push fixes", "post replies with real SHA", "poll Copilot", "parse + dedup (Copilot delayed pass)", "address + push + reply", "loop check". In deep mode, add a final "run /deepgrill on the PR" task. The "post replies" tasks are their own line items — don't fold them into "commit + push" or they get skipped.

---

## Phase 1: Fire reviewers and handle the fast pass

This phase fires once at the start of each iteration.

### Pre-checks

Capture the current HEAD SHA and iteration start timestamp **before** firing the reviewers. These are the watermarks used in Phase 1's polling step to distinguish "review of the current commit" from "stale review from a prior iteration":

```bash
ITERATION_HEAD=$(gh pr view <pr-number> --json headRefOid --jq '.headRefOid')
ITERATION_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Capture the current state of comments / reviews so polling can detect _new_ posts (not pre-existing ones from earlier iterations or runs):

```bash
# Fetch existing comments to detect already-posted reviews
gh api --paginate repos/{owner}/{repo}/pulls/<pr-number>/comments \
  > /tmp/pr-<pr-number>-comments.json
gh api --paginate repos/{owner}/{repo}/issues/<pr-number>/comments \
  > /tmp/pr-<pr-number>-issue-comments.json
gh api repos/{owner}/{repo}/pulls/<pr-number>/reviews \
  > /tmp/pr-<pr-number>-reviews.json
```

### Fire Gemini (Flash by default)

```bash
gh workflow run "Gemini Code Review" \
  --repo <owner>/<repo> \
  -F pr_number=<pr-number> \
  -F tier=flash
```

**Pass `-F tier=flash` explicitly.** The workflow defaults `tier` to `pro` when omitted (intentional for UI clickers, unintended for CLI/API callers). Pro is $1–$8 per review; Flash is $0.05–$0.20. Only override to `pro` if the user has explicitly asked for a deep review on a high-stakes PR (security/auth, schema migrations, large refactors) AND confirmed the cost.

### Fire Copilot

Copilot is a Bot, not a User — `gh pr edit --add-reviewer` and REST `requested_reviewers` don't work. Use the GraphQL `requestReviews` mutation's `botIds` field:

```bash
PR_NODE=$(gh pr view <pr-number> --json id --jq '.id')
gh api graphql \
  -f query='mutation($prId:ID!,$botIds:[ID!]){requestReviews(input:{pullRequestId:$prId,botIds:$botIds,union:true}){pullRequest{id}}}' \
  -f prId="$PR_NODE" \
  -f botIds='BOT_kgDOCnlnWA'
```

Copilot bot node id is `BOT_kgDOCnlnWA` (constant). Verify with `gh api repos/{owner}/{repo}/pulls/<n>/requested_reviewers --jq '.users[].login'` → expected `Copilot`. The mutation is idempotent — safe to call across iterations. If Copilot already reviewed the current HEAD (`commit_id == ITERATION_HEAD` from the polling step below), skip re-requesting.

### Wait for Gemini (the fast reviewer)

Do not wait for Copilot before acting on Gemini. Copilot is usually much slower, so blocking the entire iteration on it leaves fast Gemini findings idle. The efficient sequence is:

1. Fire Gemini and Copilot at the same `ITERATION_HEAD`.
2. Wait for Gemini.
3. Deduplicate, fix, push, and reply to Gemini findings.
4. Then poll Copilot for the original `ITERATION_HEAD`, dedupe against already-fixed issues, and handle any remaining findings.
5. Only after Copilot is handled or explicitly timed out should the next iteration begin.

Each reviewer has a different completion signal. **The polling check must validate that the review pertains to `ITERATION_HEAD` (current HEAD), not a stale review from a prior iteration on the same PR.**

- **Gemini**: posts an issue comment with `<!-- GEMINI_REVIEW -->` marker, plus inline comments with `<!-- GEMINI_INLINE -->` marker. Poll every 30s, timeout 10 min:

  ```bash
  # Re-fetch then filter: a Gemini comment whose `updated_at` is at-or-after
  # ITERATION_STARTED_AT — this is "newly posted/updated for this iteration"
  # rather than a leftover from a previous run.
  gh api --paginate repos/{owner}/{repo}/issues/<pr-number>/comments \
    > /tmp/pr-<pr-number>-issue-comments.json
  jq --arg t "$ITERATION_STARTED_AT" \
    '[.[] | select((.body | contains("<!-- GEMINI_REVIEW -->")) and (.updated_at >= $t))] | length' \
    /tmp/pr-<pr-number>-issue-comments.json
  ```

  Length ≥ 1 → Gemini has posted for this iteration.

If Gemini times out (no finding at `ITERATION_HEAD` within the 10 min budget), stop cleanly with a resume command rather than waiting on Copilot first. The cycle expects Gemini to be the fast lane; if Gemini didn't fire at all (workflow misconfiguration, missing API key, etc.), surface that to the user before sinking time into Copilot polling.

### Poll Copilot after the fast-pass push

After the Gemini fix commit is pushed and replies are posted, poll Copilot for the original `ITERATION_HEAD`. It is expected that Copilot may be reviewing the pre-fix head; use that output as delayed feedback for the same iteration, not as permission to skip it.

- **Copilot**: posts findings via one of three modes — check **both** the reviews endpoint and the inline-comments endpoint, treat either signal as completion. Poll every 30s, timeout 10 min when `--wait` is in effect; otherwise poll briefly and stop with a resume command if it is still pending:

  | Mode                        | `pulls/<n>/reviews` row? | `pulls/<n>/comments` rows? |
  | --------------------------- | ------------------------ | -------------------------- |
  | Review with findings        | yes (`state: COMMENTED`) | yes (one per finding)      |
  | **Findings without review** | **no**                   | **yes**                    |
  | Approved with no findings   | yes (`state: APPROVED`)  | no                         |

  Mode 2 is real and observed in production. Polling only the reviews endpoint times out in this case while Copilot has already posted findings inline.

  ```bash
  # Re-fetch BOTH endpoints — review row OR inline comments at ITERATION_HEAD
  # are independent signals that Copilot has finished.
  gh api repos/{owner}/{repo}/pulls/<pr-number>/reviews \
    > /tmp/pr-<pr-number>-reviews.json
  gh api --paginate repos/{owner}/{repo}/pulls/<pr-number>/comments \
    > /tmp/pr-<pr-number>-comments.json

  # Signal 1: top-level Copilot review at ITERATION_HEAD.
  COPILOT_REVIEW=$(jq --arg sha "$ITERATION_HEAD" \
    '[.[] | select((.user.login | test("copilot"; "i")) and (.commit_id == $sha))] | length' \
    /tmp/pr-<pr-number>-reviews.json)

  # Signal 2: Copilot inline comments at ITERATION_HEAD created since the
  # iteration started (filters out leftovers from earlier rounds on the
  # same PR). The `commit_id` field on review comments matches the head
  # SHA the comment was posted against, so the watermark is the same as
  # for reviews.
  COPILOT_INLINE=$(jq --arg sha "$ITERATION_HEAD" --arg t "$ITERATION_STARTED_AT" \
    '[.[] | select((.user.login | test("copilot"; "i")) and (.commit_id == $sha) and (.created_at >= $t))] | length' \
    /tmp/pr-<pr-number>-comments.json)

  if [ "$COPILOT_REVIEW" -ge 1 ] || [ "$COPILOT_INLINE" -ge 1 ]; then
    # Copilot is done for this iteration — proceed to delayed Copilot parsing.
    :
  fi
  ```

  Either signal ≥ 1 → Copilot has finished for this iteration. **Do not** count reviews or comments of prior commits as completion — those are stale.

  Note: 0 inline comments ≠ missing review. A Copilot review on the current commit with `state: APPROVED` and no inline comments is a clean pass — surface the review body so the user sees it.

If Copilot is still pending after the non-`--wait` poll budget, stop cleanly and report the resume command. Do not start the next iteration while Copilot for the previous `ITERATION_HEAD` is still pending unless the user explicitly tells you to abandon it. If Copilot times out in `--wait` mode, log it and proceed with the already-handled fast-pass findings. Note the missing reviewer in the final summary.

---

## Phase 2: Parse, categorize, deduplicate

Run this phase twice per iteration:

- **Gemini fast pass**: parse Gemini findings (inline + summary), dedupe within them, then proceed through fixes/replies.
- **Delayed Copilot pass**: after the fast-pass push, parse Copilot findings for the original `ITERATION_HEAD`, dedupe them against the Gemini findings and current code, then fix/reply only what remains.

For Copilot findings already fixed by the Gemini commit, do not edit code again. Reply with the Gemini commit SHA and record the comment id as handled.

### Refresh comment fixtures

```bash
gh api --paginate repos/{owner}/{repo}/pulls/<pr-number>/comments \
  > /tmp/pr-<pr-number>-comments.json
gh api --paginate repos/{owner}/{repo}/issues/<pr-number>/comments \
  > /tmp/pr-<pr-number>-issue-comments.json
```

### Parse Copilot findings

```bash
jq '[.[] | select((.user.login | test("copilot"; "i"))
                  and (.in_reply_to_id == null or .in_reply_to_id == 0))
       | {id, path, line, body}]' \
  /tmp/pr-<pr-number>-comments.json
```

For each: classify severity (critical / suggestion / nitpick / question), category (architecture / correctness / security / performance / maintainability / testing), record file path, summarize.

### Parse Gemini findings

Gemini posts in TWO places:

1. **Summary issue comment** — `<!-- GEMINI_REVIEW -->` marker, full list with counts. The canonical list for dedup math.

   ```bash
   jq -r '.[] | select(.body | contains("<!-- GEMINI_REVIEW -->")) | .body' \
     /tmp/pr-<pr-number>-issue-comments.json
   ```

2. **Inline review comments** — `<!-- GEMINI_INLINE -->` marker on each, posted by `github-actions[bot]`. The reply targets for per-line findings.

   ```bash
   jq '[.[] | select((.body | contains("<!-- GEMINI_INLINE -->"))
                     and (.in_reply_to_id == null))
          | {id, path, line, body}]' \
     /tmp/pr-<pr-number>-comments.json
   ```

Severity emoji markers in Gemini bodies: 🔴 critical, 🟡 suggestion, 🟢 nitpick, 💡 question.

### Deduplicate across reviewers

Two findings are duplicates if they:

- Reference the same file AND same line range (±5 lines), OR
- Reference the same file AND describe the same issue (semantic match)

For each group, classify as `pair_overlap` (Gemini + Copilot both caught it) or `unique` (only one reviewer).

In the staggered flow, Gemini's findings are dedupe-classified during the Gemini fast pass; Copilot's findings are dedupe-classified during the delayed pass against the already-handled Gemini set (so an "overlap" in the Copilot pass means "Gemini already addressed this — just reply with the Gemini fix SHA").

### Present comparison

```
## Review Comparison — PR #<number>, iteration <N>

| Severity        | Gemini | Copilot | Pair | Unique |
|-----------------|--------|---------|------|--------|
| Critical        |        |         |      |        |
| Suggestions     |        |         |      |        |
| Nitpicks        |        |         |      |        |
| Questions       |        |         |      |        |
| **Total**       |        |         |      |        |

### Unique to Gemini
1. [severity] file:line — summary

### Unique to Copilot
...

### Overlapping
...
```

Proceed immediately to address findings — no confirmation needed.

---

## Phase 3: Address findings (no replies yet)

For each deduplicated finding in the active pass, ordered by severity (critical first):

1. **Read the file** at the referenced path.
2. **Classify resolution**:
   - **Fix**: apply the code change.
   - **Defer**: create a GitHub issue (label: `from-ai-review`). Capture the issue URL.
   - **Dismiss**: false positive — write down the rationale.
3. **Execute resolution** — Edit / Write / `gh issue create` as needed.
4. **Record the resolution to `/tmp/pr-<pr-number>-iter-<N>-<pass>-resolutions.json`** as **one row per reply target** (= one row per original reviewer comment). Use `gemini` for the Gemini fast pass and `copilot` for the delayed Copilot pass. Phase 4 step 5 reads this file and posts exactly one reply per row. **Do not post any reply yet** — the SHA isn't known until after the push.

   **File format**: a single JSON array `[{...}, {...}, ...]`. Build the full array in-memory while iterating findings, then call `Write(file_path, content=<json-array>)` once at the end of Phase 3. Don't try to "append" rows to a JSON file in place — naïve append produces invalid JSON. Phase 4 reads with `jq -c '.[]'` and iterates row-by-row.

   For cross-reviewer overlapping findings (the same underlying issue caught by multiple reviewers), write **one row per reviewer**, all sharing the same `overlap_group_id`, `resolution`, and `explanation` — but each with its own `finding_id`/`reviewer` and an `also_flagged_by` listing the OTHER reviewers in the group. That way every original commenter gets a reply, and each reply correctly attributes the others.

   Each row must include enough context to post the right kind of reply at the right endpoint. The schema below uses `<angle-bracket placeholders>` for illustration — the on-disk file contains real values (numbers, strings, `null`s) and is valid JSON:

   ```text
   {
     "finding_id": <numeric comment id from GitHub, or null for gemini-summary findings>,
     "reviewer": "copilot" | "gemini-inline" | "gemini-summary",
     "path": "<file path>",
     "line": <line number or null>,
     "resolution": "fix" | "defer" | "dismiss",
     "explanation": "<one-line description of what changed / why dismissed>",
     "defer_issue_url": "<gh issue url, only for defer; null otherwise>",
     "also_flagged_by": ["<other reviewer names in this overlap group>"],
     "overlap_group_id": "<stable id shared by all rows in the same dedup group, or null for unique findings>",
     "finding_text": "<for gemini-summary rows: the exact bullet/paragraph from the Gemini summary that this row replies to. Captured at Phase 3 write time so Phase 4 can quote it without re-parsing the summary. null for inline rows.>"
   }
   ```

   Reviewer values:
   - `copilot` — Copilot inline review comment (`user.login` matches `copilot`)
   - `gemini-inline` — `github-actions[bot]` comment with `<!-- GEMINI_INLINE -->` marker
   - `gemini-summary` — finding present only in the Gemini summary `<!-- GEMINI_REVIEW -->` issue comment, no inline equivalent. `finding_id` is null; `finding_text` is required (used to quote the original finding in the new top-level issue comment Phase 4 will post).

---

## Phase 4: Commit, push, and post replies

Run this phase after each pass that produced code changes or reply-worthy resolutions: once for the Gemini fast pass, and again for the delayed Copilot pass if it has remaining findings. If a pass has only dismissals or already-fixed findings (or no findings at all), skip the commit when there are no file changes, but still post the replies — with the prior Gemini-commit SHA for "already fixed" cases, or no SHA for dismissals.

1. **Stage and commit**:

   ```bash
   git add <changed-files>
   git commit -m "fix: address AI review feedback (iteration <N>) on PR #<pr-number>"
   ```

2. **Push**:

   ```bash
   git push
   PUSHED_SHA=$(git rev-parse HEAD)
   PUSHED_SHA_SHORT=$(git rev-parse --short=8 HEAD)
   ```

3. **Verify the push landed on the PR head** before posting replies — otherwise replies will reference a SHA that's not in the PR's commit history. GitHub's PR API is eventually consistent, so the headRefOid can lag the actual ref by a few seconds after a push. Use a short retry loop with backoff, and use string equality (not `grep` regex) to avoid quoting/anchor surprises:

   ```bash
   verify_pr_head() {
     local attempt
     for attempt in 1 2 3 4; do
       local pr_head
       pr_head=$(gh pr view <pr-number> --json headRefOid --jq '.headRefOid')
       if [[ "$pr_head" == "$PUSHED_SHA" ]]; then
         return 0
       fi
       sleep $(( attempt * 2 ))  # 2s, 4s, 6s, 8s — total ~20s ceiling
     done
     echo "PR head ($pr_head) does not match pushed SHA ($PUSHED_SHA) after retries — investigate before replying" >&2
     return 1
   }
   verify_pr_head || exit 1
   ```

4. **Post replies now that the real SHA is in hand.** Read the current pass's `/tmp/pr-<pr-number>-iter-<N>-<pass>-resolutions.json` and post one reply per row. **Do not skip this step** — fixes without replies leave reviewer threads orphaned. Build the body with `${PUSHED_SHA_SHORT}` substituted inline.

   Reply body templates:
   - **Fix** (single reviewer):

     ```
     Fixed in `${PUSHED_SHA_SHORT}`.

     <explanation from resolutions.json>
     ```

   - **Fix** (overlapping — `also_flagged_by` non-empty):

     ```
     Fixed in `${PUSHED_SHA_SHORT}`.

     <explanation>

     Note: also flagged by <comma-joined also_flagged_by>.
     ```

   - **Defer**:

     ```
     Deferred — tracking in <defer_issue_url>.

     <explanation: why this is being deferred rather than fixed in this PR>
     ```

   - **Dismiss**:

     ```
     Dismissing — false positive.

     <explanation: why the reviewer's reasoning doesn't apply>
     ```

   Endpoints (route by `reviewer` field):
   - `copilot`, `gemini-inline` (when `finding_id` is non-null):

     ```bash
     gh api -X POST repos/{owner}/{repo}/pulls/<pr-number>/comments/<finding_id>/replies \
       -f body="<assembled body>"
     ```

   - `gemini-summary` (no inline equivalent, post a top-level issue comment quoting the finding):

     ```bash
     gh api -X POST repos/{owner}/{repo}/issues/<pr-number>/comments \
       -f body="> <quoted summary finding text, prefixed with > on each line>

     <assembled body>"
     ```

   Iterate the resolutions file row-by-row. If any single reply POST fails, log the failure and continue with the rest — partial reply coverage is better than no replies. Surface the count of failed-reply POSTs in the Phase 6 summary so the user can manually follow up.

---

## Phase 5: Loop control

After each iteration (both the Gemini fast pass and the Copilot delayed pass complete), decide whether to continue:

- **Lean mode**:
  1. If iteration count == `MAX_ITERS` (2): exit the loop, proceed to Phase 6.
  2. Else if any reviewer found new findings on the post-fix HEAD (re-fire Phase 1 at the start of the next iteration to detect): start the next iteration.
  3. Else (both reviewers clean): exit the loop, proceed to Phase 6.

- **Deep mode** — same shape, but with an earlier exit:
  1. If iteration count == `MAX_ITERS` (4): exit the loop, proceed to **Phase 5.5**.
  2. Else if this iteration produced ≥1 `fix` resolution across either the Gemini fast pass or the Copilot delayed pass (a commit was pushed): start the next iteration so reviewers can validate the fix.
  3. Else (no fix resolutions in either pass — all findings were defer/dismiss, or there were no findings at all): **early-exit** the loop, proceed to **Phase 5.5**.

The deep-mode early-exit avoids re-firing reviewers on an unchanged HEAD when there's nothing left to fix. Deferred and dismissed findings don't justify another iteration; if you want a second opinion on a defer/dismiss decision, that's what the final `/deepgrill` is for.

---

## Phase 5.5: Final `/deepgrill` (deep mode only)

**Deep mode only.** Lean mode skips this step and goes straight to Phase 6.

Invoke via the Skill tool: `Skill(skill="deepgrill")`.

`/deepgrill` runs `/refactorpass` + `/grill deep` — the full agent matrix (`code-reviewer`, `silent-failure-hunter`, `type-design-analyzer`, `comment-analyzer`, `pr-test-analyzer`, `security-review`). Fresh agents in a separate sub-skill context look at the PR's current state on top of whatever the Gemini+Copilot loop landed. This is the deep-mode replacement for the older Phase 1 `/review` fire — moving the fresh-agent pass _outside_ the polling loop avoids the orchestration trap where `/review`'s self-contained prompt caused control to exit the polling loop early.

> ⚠️ **Do not stop after `/deepgrill` returns.** When control returns from the Skill tool, treat the output as **Phase 5.5's deliverable** and immediately proceed to Phase 6. Do not summarize, do not hand back to the user mid-skill, do not assume the workflow is done. `/reviewit` owns the final summary.

If `/deepgrill` surfaces findings the developer wants to act on, they're handled interactively per `/grill`'s standard Phase 4 verification flow — fix locally, then push. Those follow-up commits are **not** automatically re-routed back through Gemini+Copilot; the developer can re-run `/reviewit <pr>` (lean) on the follow-up commits if they want bot eyes on them.

Capture for the Phase 6 summary:

- whether `/deepgrill` ran (always "yes" in deep mode, "skipped (lean)" otherwise),
- whether `/refactorpass` produced a commit,
- the count of `/grill deep` findings the user chose `fix` / `defer` / `ignore` on, if the sub-skill returned that summary.

If `/deepgrill` fails or is interrupted, record the failure in the Phase 6 summary and continue — the Gemini+Copilot loop result is still valid. Do not auto-retry.

---

## Phase 6: Summary

```
✅ /reviewit complete on PR #<pr-number>  (mode: <lean | deep>)

Iterations: <N> of <MAX_ITERS> max
Final state: <clean | early-exit (no fixes in last iter) | iteration-cap-reached | reviewer-timeout>

Findings addressed:
- Fixed: <count>
- Deferred (issues): <count> — links: ...
- Dismissed: <count>

Replies posted: <count> of <total reply targets>
- Reply targets = inline rows (copilot / gemini-inline) + gemini-summary rows.
- Failed to post: <count> — manual follow-up required on: <comma-joined finding ids or summary-row indices>

Reviewer breakdown:
- Gemini Flash: <total findings, X unique>
- Copilot: <total findings, X unique>

Commits pushed: <list>

Final /deepgrill (deep mode only):
- Status: <ran | failed: reason | skipped (lean)>
- /refactorpass commit: <sha or "none">
- /grill deep findings: <fix count> fixed, <defer count> deferred, <ignore count> ignored

Next: review the diff and merge when ready.
```

If iteration cap was hit in **lean mode**:

```
⚠️  Hit 2-iteration lean cap. Residual findings remain — review the latest
    reviewer comments and either fix manually, merge as-is, or escalate with
    /reviewit <pr> deep for the 4-iter chain + final /deepgrill.
```

If iteration cap was hit in **deep mode**:

```
⚠️  Hit 4-iteration deep cap. Residual findings remain — the final /deepgrill
    still ran on the cap-state PR, so review its output alongside the open
    Gemini/Copilot threads before merging.
```

If deep mode early-exited on no-fixes:

```
ℹ️  Deep loop early-exited after iteration <N> (no fix resolutions in last
    iter — all findings were defer/dismiss). Final /deepgrill ran on the
    converged PR state.
```

If a reviewer timed out:

```
⚠️  <Reviewer> did not respond within 10 min — proceeded with the available
    review streams. Re-invoke /reviewit to retry the missing reviewer if desired.
```

---

## Error handling

- **One reviewer never responds**: proceed after timeout. Note in summary.
- **PR closed mid-cycle**: stop immediately, do not commit.
- **Force-push needed** (e.g., to amend a fix that broke something): use `--force-with-lease` to avoid clobbering.
- **`/deepgrill` fails or is interrupted in Phase 5.5**: record the failure in the Phase 6 summary; do not retry automatically. The Gemini+Copilot loop result is still valid — `/deepgrill` is additive.

---

## Response style

- Concise, actionable.
- Comparison table format for findings.
- Clear before/after for code changes.
- Link created issues for deferrals.

---

## Source of truth

This skill lives upstream at `.claude/skills/reviewit/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten on the next sync — make all changes upstream.
