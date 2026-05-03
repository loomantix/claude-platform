---
name: copilot-review
description: Address GitHub Copilot code review comments on a PR systematically
argument-hint: PR number (e.g., 1206)
---

# Copilot Review Resolver

You are helping a developer address GitHub Copilot code review comments on a pull request. Follow a systematic approach: fetch comments, create an isolated worktree, analyze each comment, address or defer appropriately, reply to confirm resolution, and iterate until complete.

## Core Principles

- **Analyze before acting**: Understand the Copilot comment fully before making changes
- **Quality over speed**: Make thoughtful fixes that address the root cause
- **Scope discipline**: Defer scope-creep issues to separate GitHub issues
- **Clear communication**: Reply to each comment explaining how it was addressed
- **Use TodoWrite**: Track all comments and their resolution status

---

## Phase 0: Initialization

**Goal**: Validate the PR and set up the working environment

**Arguments**: $ARGUMENTS (PR number)

**Actions**:

1. **Validate PR number** from arguments
   - If missing, ask user for the PR number

2. **Fetch PR details**:

   ```bash
   gh pr view <pr-number> --json number,title,headRefName,baseRefName,state
   ```

3. **Verify PR is open and ready for review**
   - If closed or merged, notify user and exit

4. **Create todo list** with all phases

---

## Phase 1: Fetch Copilot Comments

**Goal**: Load and categorize all Copilot code review comments

**IMPORTANT — jq quoting pitfalls**:

- Complex `--jq` expressions passed directly to `gh api` are fragile because of shell/YAML quoting (especially with `!=`, embedded `\n`, and nested quotes)
- `!=` is a valid jq operator; the issues come from how the shell parses `gh api --jq` arguments, not from jq itself
- Prefer simple `--jq` filters (or none), save raw JSON to a temp file first, then run richer `jq` queries against that file
- The temp file approach also enables reuse across multiple queries without extra API calls

**Actions**:

1. **Fetch all PR comments to a temp file** (single API call, reused for all queries):

   ```bash
   gh api --paginate repos/{owner}/{repo}/pulls/<pr-number>/comments > /tmp/pr-<pr-number>-comments.json
   ```

2. **Extract Copilot top-level comments** (those without `in_reply_to_id`):

   ```bash
   jq '[.[] | select((.user.login | test("copilot"; "i")) and (.in_reply_to_id == null or .in_reply_to_id == 0)) | {id, path, line, body: (.body | split("\n")[0][:120])}]' /tmp/pr-<pr-number>-comments.json
   ```

3. **Extract reply target IDs** (which comments already have human replies):

   ```bash
   jq '[.[] | select(.in_reply_to_id > 0) | .in_reply_to_id] | unique' /tmp/pr-<pr-number>-comments.json
   ```

4. **Compute unaddressed comments** (Copilot comments whose ID is not in the replied set):

   ```bash
   jq '[.[] | select((.user.login | test("copilot"; "i")) and (.in_reply_to_id == null or .in_reply_to_id == 0)) | {id, path, line, body: (.body | split("\n")[0][:120])}] as $all | [.[] | select(.in_reply_to_id > 0) | .in_reply_to_id] | unique as $replied | $all | map(select(.id as $cid | $replied | index($cid) | not))' /tmp/pr-<pr-number>-comments.json
   ```

5. **Read full body** of each unaddressed comment when needed:

   ```bash
   jq '.[] | select(.id == <comment-id>) | .body' /tmp/pr-<pr-number>-comments.json
   ```

6. **Categorize each unaddressed comment** after reading the file:
   - **Actionable**: Issues that should be fixed in this PR
   - **Scope-creep**: Issues that are valid but outside the PR's scope
   - **Invalid/False-positive**: Comments that don't apply or are incorrect

7. **Present summary to user**:
   - Total Copilot comments found
   - Number unaddressed (no human reply yet)
   - Brief preview of each unaddressed comment (file:line + first line of body)

8. **Ask user for confirmation** before proceeding

---

## Phase 2: Worktree Setup

**Goal**: Create an isolated environment for making changes

**Actions**:

1. **Get PR branch name** from Phase 0 data (`headRefName`)

2. **Check if worktree already exists**:

   ```bash
   git worktree list | grep "copilot-review-<pr-number>"
   ```

3. **Create or reset worktree**:

   **If worktree doesn't exist**:

   ```bash
   git fetch origin <branch-name>
   git worktree add worktrees/copilot-review-<pr-number> origin/<branch-name>
   ```

   **If worktree exists**:

   ```bash
   git -C worktrees/copilot-review-<pr-number> fetch origin
   git -C worktrees/copilot-review-<pr-number> checkout <branch-name>
   git -C worktrees/copilot-review-<pr-number> reset --hard origin/<branch-name>
   ```

4. **Verify worktree is ready**:

   ```bash
   git -C worktrees/copilot-review-<pr-number> status
   git -C worktrees/copilot-review-<pr-number> log --oneline -3
   ```

5. **Set worktree path for subsequent operations**:
   - All file reads/edits should use the worktree path: `worktrees/copilot-review-<pr-number>/`
   - All git commands should use: `git -C worktrees/copilot-review-<pr-number>`

---

## Phase 3: Comment Resolution Loop

**Goal**: Address each unaddressed Copilot comment systematically

**For each unaddressed comment, perform the following cycle**:

### Step 3.1: Analyze Comment

1. **Read the file** at the path specified in the comment
2. **Understand the context**:
   - What is Copilot pointing out?
   - Is the concern valid?
   - Is this within the PR's scope?

3. **Classify the resolution approach**:
   - **Fix**: Apply a code change to address the issue
   - **Defer**: Valid concern but out of scope - create a GitHub issue
   - **Dismiss**: False positive or incorrect suggestion - explain why

### Step 3.2: Execute Resolution

**If Fix**:

1. Make the necessary code changes
2. Run relevant linting: `just lint-fix`
3. Run relevant tests if applicable
4. Prepare a clear explanation of what was changed

**If Defer**:

1. Create a GitHub issue:
   ```bash
   gh issue create --title "<issue-title>" \
     --label "tech-debt,from-copilot-review" \
     --body "## Context\n\nThis issue was identified during Copilot code review of PR #<pr-number>.\n\n## Original Comment\n\n<copilot-comment-body>\n\n## Recommendation\n\n<suggested-approach>\n\n## Related\n\n- PR #<pr-number>"
   ```
2. Note the issue number for the reply

**If Dismiss**:

1. Prepare a clear explanation of why this is a false positive or doesn't apply

### Step 3.3: Record the Resolution (no reply yet)

**Do NOT post a reply here.** The reply needs to reference the real commit SHA, which doesn't exist until after Phase 4's push. Posting now means the SHA is a placeholder the model substitutes with garbage (or skips the reply entirely). Replies are posted in **Phase 4 step 6** using the real SHA.

1. **Build a resolution row in memory** for this comment:

   ```text
   {
     "comment_id": <numeric id from GitHub>,
     "path": "<file path>",
     "line": <line number or null>,
     "resolution": "fix" | "defer" | "dismiss",
     "explanation": "<one-line: what changed / why deferred / why dismissed>",
     "defer_issue_url": "<URL or null — only set on defer>"
   }
   ```

2. **Accumulate rows** in an in-memory list as you iterate the comment loop. **Do not** try to append-write the JSON file row-by-row — naïve append produces invalid JSON. Phase 3 finishes by writing the full array once.

### Step 3.4: Update Progress

1. Mark the comment as resolved in TodoWrite (the resolution is recorded; only the reply post is deferred).
2. Move to the next unaddressed comment.
3. Repeat Steps 3.1-3.4 until all comments are processed.

### Step 3.5: Persist resolutions before Phase 4

After the loop completes, write the accumulated array once:

```text
Write(file_path="/tmp/copilot-review-<pr-number>-resolutions.json",
      content=<json-array-of-all-resolution-rows>)
```

Phase 4 step 6 reads this file and posts one reply per row.

---

## Phase 4: Commit, Push, and Post Replies

**Goal**: Save changes, push to the PR branch, verify the push landed, then post one reply per recorded resolution using the real commit SHA.

**Actions**:

1. **Verify changes** (in worktree):

   ```bash
   git -C worktrees/copilot-review-<pr-number> status
   git -C worktrees/copilot-review-<pr-number> diff
   ```

2. **Run quality checks** (in worktree):

   ```bash
   cd worktrees/copilot-review-<pr-number> && just lint-fix
   cd worktrees/copilot-review-<pr-number> && just typecheck
   ```

3. **Commit changes** (only if there are code changes from "fix" resolutions):

   ```bash
   cd worktrees/copilot-review-<pr-number> && git add -A && git commit -m "fix(review): address copilot feedback"
   ```

4. **Push to remote and capture the real SHA**:

   ```bash
   git -C worktrees/copilot-review-<pr-number> push origin <branch-name>
   PUSHED_SHA=$(git -C worktrees/copilot-review-<pr-number> rev-parse HEAD)
   PUSHED_SHA_SHORT=$(git -C worktrees/copilot-review-<pr-number> rev-parse --short=8 HEAD)
   ```

   If push is rejected (remote has new commits):

   ```bash
   git -C worktrees/copilot-review-<pr-number> pull --rebase origin <branch-name>
   git -C worktrees/copilot-review-<pr-number> push origin <branch-name>
   PUSHED_SHA=$(git -C worktrees/copilot-review-<pr-number> rev-parse HEAD)
   PUSHED_SHA_SHORT=$(git -C worktrees/copilot-review-<pr-number> rev-parse --short=8 HEAD)
   ```

5. **Verify the push landed on the PR head** before posting replies — GitHub's PR API is eventually consistent, so `headRefOid` can lag the actual ref by a few seconds:

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

6. **Post replies now that the real SHA is in hand.** Read `/tmp/copilot-review-<pr-number>-resolutions.json` and post one reply per row, building each body with `${PUSHED_SHA_SHORT}` substituted inline.

   Reply body templates:

   For **Fix**:

   ```
   Fixed in `${PUSHED_SHA_SHORT}`.

   <explanation from resolutions.json>
   ```

   For **Defer**:

   ```
   Deferred — tracking in <defer_issue_url>.

   <explanation: why this is being deferred rather than fixed in this PR>
   ```

   For **Dismiss**:

   ```
   Dismissing — false positive.

   <explanation: why the reviewer's reasoning doesn't apply>
   ```

   Post each reply via:

   ```bash
   gh api -X POST repos/{owner}/{repo}/pulls/<pr-number>/comments/<comment_id>/replies \
     -f body="<assembled body>"
   ```

   If any single reply POST fails, log it and continue with the rest — partial reply coverage is better than none. Surface the count of failed-reply POSTs in the Phase 7 completion summary.

---

## Phase 5: Re-trigger Copilot Review

**Goal**: Get fresh Copilot feedback on the updated code

**Actions**:

1. **Check if Copilot auto-reviews on push**
   - Wait 30-60 seconds for automatic review

2. **If no automatic review**, trigger manually via GraphQL.

   **IMPORTANT**: Copilot is a Bot, not a User — `gh pr edit --add-reviewer` and the REST `requested_reviewers` endpoint do **not** work for Copilot. Use the GraphQL `requestReviews` mutation with `botIds`:

   ```bash
   PR_NODE=$(gh pr view <pr-number> --json id --jq '.id')
   gh api graphql \
     -f query='mutation($prId:ID!,$botIds:[ID!]){requestReviews(input:{pullRequestId:$prId,botIds:$botIds,union:true}){pullRequest{id}}}' \
     -f prId="$PR_NODE" \
     -f botIds='BOT_kgDOCnlnWA'
   ```

   Copilot bot node id is `BOT_kgDOCnlnWA` (constant). Verify with `gh api repos/{owner}/{repo}/pulls/<n>/requested_reviewers --jq '.users[].login'` → expected `Copilot`. The mutation is idempotent — safe to call across iterations.

3. **Wait for the new Copilot review** to appear (filter by Copilot's login, then take the most recent):

   ```bash
   gh api repos/{owner}/{repo}/pulls/<pr-number>/reviews \
     --jq '[.[] | select(.user.login | test("copilot"; "i"))] | last | {id, submitted_at, user: .user.login}'
   ```

4. **Fetch new comments** and check for any new unaddressed items

---

## Phase 6: Iteration Decision

**Goal**: Determine if another resolution cycle is needed

**Actions**:

1. **Count new unaddressed comments** from the latest Copilot review

2. **If new comments exist**:
   - Present summary to user
   - Ask: "Found X new Copilot comments. Continue resolving?"
   - If yes, return to Phase 3
   - If no, proceed to Phase 7

3. **If no new comments**:
   - Proceed to Phase 7

---

## Phase 7: Completion Summary

**Goal**: Summarize all work done and provide next steps

**Actions**:

1. **Generate summary report**:
   - Total comments addressed
   - Comments fixed with code changes
   - Comments deferred to GitHub issues (with issue links)
   - Comments dismissed with reasons
   - Commits created

2. **Cleanup worktree** (optional, ask user):

   ```bash
   git worktree remove worktrees/copilot-review-<pr-number>
   ```

3. **Provide next steps**:
   - Review the PR to ensure all changes are correct
   - Merge the PR when ready
   - Follow up on any deferred GitHub issues

---

## Posting Inline PR Review Comments (API Reference)

GitHub's PR comment API (`POST repos/{owner}/{repo}/pulls/{pull_number}/comments`) uses a `oneOf` schema. You must use **exactly one** of these patterns:

### Line-level comment (single line)

```bash
jq -n \
  --arg body "$BODY" \
  --arg commit_id "$HEAD_SHA" \
  --arg path "$FILE_PATH" \
  --argjson line $LINE_NUM \
  --arg side "RIGHT" \
  '{body: $body, commit_id: $commit_id, path: $path, line: $line, side: $side}' \
| gh api -X POST "repos/{owner}/{repo}/pulls/{pull_number}/comments" --input -
```

### Line-level comment (multi-line range)

```bash
jq -n \
  --arg body "$BODY" \
  --arg commit_id "$HEAD_SHA" \
  --arg path "$FILE_PATH" \
  --argjson line $END_LINE \
  --argjson start_line $START_LINE \
  --arg side "RIGHT" \
  '{body: $body, commit_id: $commit_id, path: $path, line: $line, start_line: $start_line, side: $side, start_side: "RIGHT"}' \
| gh api -X POST "repos/{owner}/{repo}/pulls/{pull_number}/comments" --input -
```

### File-level comment (no line anchor)

```bash
jq -n \
  --arg body "$BODY" \
  --arg commit_id "$HEAD_SHA" \
  --arg path "$FILE_PATH" \
  --arg subject_type "file" \
  '{body: $body, commit_id: $commit_id, path: $path, subject_type: $subject_type}' \
| gh api -X POST "repos/{owner}/{repo}/pulls/{pull_number}/comments" --input -
```

### Important constraints

- **`line` must be within the PR diff** for that file. If the target line isn't in a diff hunk, the API returns 422. Fall back to `subject_type: "file"`.
- **`commit_id` must match the PR's current HEAD** when creating or re-posting a review comment. After a force-push, existing inline comments become "Outdated" and remain anchored to the old commit SHA. You can still edit their body text via `PATCH /repos/{owner}/{repo}/pulls/comments/{comment_id}`, but you cannot change their diff anchor; to re-anchor to the new HEAD you must delete and recreate the comment.
- **Do NOT mix schemas**: `subject_type` cannot be combined with `line`/`start_line`/`side`. The API uses `oneOf` — pick one pattern.
- **Do NOT use `position`** (deprecated) or include `subject_type: "line"` explicitly (not a valid creation param, only returned in responses).

### Re-posting comments after rebase/force-push

After a force-push, all existing inline comments become "outdated" (anchored to the old commit SHA). To re-anchor comments to the new commit:

1. Fetch existing comments: `gh api --paginate repos/{owner}/{repo}/pulls/{pull_number}/comments`
2. Filter to your comments (by `user.login` or body pattern)
3. Save `{path, body, original_line, original_start_line, subject_type}` from each
4. Delete old comments: `gh api -X DELETE repos/{owner}/{repo}/pulls/comments/{id}`
5. Re-post against new HEAD using the patterns above
6. Use `grep -n` on actual files to find correct line numbers for the new commit
7. If a target line isn't in the diff, fall back to file-level

---

## Error Handling

**If GitHub API fails**:

- Retry once after 5 seconds
- If still failing, report the error and ask user to check authentication

**If worktree creation fails**:

- Check if branch exists: `git branch -r | grep <branch-name>`
- Try fetching: `git fetch origin <branch-name>`
- Report specific error to user

**If reply posting fails**:

- The comment may be part of a review thread (not standalone)
- Fallback: Post a new PR comment referencing the original:
  ```bash
  gh pr comment <pr-number> --body "Re: Copilot comment on <file>:<line>\n\n<reply-message>"
  ```

---

## Response Style

- Keep updates concise and actionable
- Use checkmarks to show progress: "Fixed comment on file.ts:42"
- Show clear before/after for code changes
- Link to created issues when deferring
- Present user decision points clearly

---
