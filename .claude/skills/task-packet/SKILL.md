---
name: task-packet
description: Execute an implementation Task Packet end-to-end (code, tests, GitHub issue, PR, and closure)
argument-hint: Paste a full Task Packet markdown document
---

# Task Packet Executor

You are given a **Task Packet** in markdown. Your job is to **execute it end-to-end**:

- Implement the described changes.
- Create/update the corresponding GitHub issue(s).
- Ensure tests (including E2E) pass and coverage is maintained.
- Open a PR wired to the issue.
- Report back with a clear summary and next steps for the user.
- Only consider the packet "done" after the user confirms it works and the issue is closed.

---

## Phase 0: Parse & Validate the Task Packet

1. **Parse the argument** as a markdown Task Packet following this structure:
   - `# Task Packet: ...`
   - `## Metadata`
   - `## Context`
   - `## Implementation Checklist`
   - `## Testing & Coverage`
   - `## GitHub Issue & PR`
   - `## Observability & Post-Deploy Validation`
   - `## Done When`

2. **Extract key fields**:
   - Task name
   - Area, type, risk, estimate, target branch
   - Implementation checklist items
   - Test commands and specific test files/specs
   - GitHub issue instructions (existing vs create)
   - PR requirements
   - Acceptance criteria and “Done When” items

3. **Sanity-check**:
   - If _critical_ info is missing (e.g., no indication of tests or no description of desired behavior), ask the user **one concise clarification message** before proceeding.
   - If the packet is slightly under-specified but still executable, make pragmatic assumptions and **call them out explicitly** in your status summary.

---

## Phase 1: Plan & Confirm

1. **Summarize** in 3–6 bullet points:
   - What you’re going to change
   - Expected GitHub artifacts (issue + PR)
   - Tests you’ll run (unit + E2E)
   - Any obvious risks (migrations, infra, breaking changes)

2. If **Risk = high** or the task involves:
   - Database schema changes
   - Infra/cluster changes
   - Changes touching PHI/PII handling in a health context  
     → Ask for explicit confirmation from the user before proceeding.

3. Once confirmed (or if clearly safe/low-risk), begin executing the packet.

---

## Phase 2: Git & Environment Prep

### Worktree Setup (New)

Before creating a branch or preparing the environment, create an isolated worktree for this Task Packet. This prevents conflicts between concurrent tasks.

1. **Determine Worktree Name**
   - Derive `<worktree-slug>` from the Task Packet name.
   - Recommended format: `task-<slug>`  
     Example: `task-calendar-seed-fix`

2. **Create or Reuse Worktree**
   - If the worktree directory does not exist:

     ```bash
     git worktree add worktrees/<worktree-slug> <target-branch>
     ```

   - If it already exists:

     ```bash
     git worktree unlock worktrees/<worktree-slug> || true
     git -C worktrees/<worktree-slug> pull --rebase
     ```

3. **Switch Execution Context**
   - All subsequent steps (branch creation, commits, tests, PR creation) must be performed _inside_ this worktree:

     ```bash
     cd worktrees/<worktree-slug>
     ```

4. **Create Task Branch**
   - Create the branch inside the new worktree, naming it as specified in the packet or derived from the task name (e.g. `feature/<slug-from-task-name>` or `bugfix/<slug-from-task-name>`):

     ```bash
     git switch -c <branch-name>
     ```

5. **Environment**
   - Ensure dev environment is running, using commands from the packet (e.g., your repo's `just dev` recipe, `pnpm dev`, or `docker compose up`).
   - Run baseline tests specified in the packet to confirm the repo starts in a green state.

6. **Todo tracking**
   - Use TodoWrite (if available) to track each top-level checklist item as you complete it.

---

## Phase 3: Implementation

1. **Follow the Implementation Checklist** **in order**:
   - Make high-confidence, minimal, idiomatic changes.
   - Respect existing architecture, patterns, and conventions in this repo.
   - Keep changes scoped tightly to the packet. Avoid “while I’m here” refactors unless they are:
     - Trivial (e.g., typo fix) **and**
     - Clearly beneficial and low-risk.

2. **Commits**
   - Prefer small, logical commits:
     - Implementation
     - Tests
     - Minor fixes/cleanup
   - Keep commit messages clear and imperative.

3. **Observability**
   - When instructed, add or update logs/metrics/traces:
     - Use existing structured logging patterns and the canonical correlation fields your repo already uses (e.g., an `event` name plus per-domain ids).
     - Do not spam logs; prefer focused, useful events.

4. If you discover **inconsistencies** between the Task Packet and the current code:
   - Follow the packet’s intent but adapt to the real code.
   - Note any deviations in the final status summary and in the PR description.

---

## Phase 4: Testing & Coverage

**Non-negotiable**: you must run the tests specified in the Task Packet before calling it done.

1. **Unit / Integration Tests**
   - Implement or update tests in the paths specified.
   - Ensure:
     - Happy path is covered.
     - Important edge cases are covered.
     - At least one failure/exception scenario is covered if relevant.

2. **E2E Tests**
   - Implement or update the E2E specs from the packet (e.g., Playwright).
   - Cover the core user flow(s) described.
   - Run the E2E command(s) listed (e.g., `pnpm exec playwright test` or repo equivalent).

3. **Run All Required Commands**
   - Execute all test commands listed under **“Commands to Run”** in the packet.
   - Fix any failures; rerun until green.

4. **Coverage**
   - If a coverage tool is configured, run it.
   - Ensure:
     - No meaningful drop in coverage in the files you touched.
     - New branches/conditions in core logic are covered.
   - If you can’t avoid a temporary drop, document why in the PR and mention it in your status summary.

---

## Phase 5: GitHub Issue & PR

1. **Issue Handling**
   - If the packet specifies `existing:#<issue-number>`:
     - Use that issue as the primary tracking item.
   - If the packet specifies `create:new`:
     - Create a new GitHub issue with:
       - Title from “Proposed Issue Title”
       - Labels from “Labels”
       - Acceptance criteria from the “Issue Acceptance Criteria”
     - You may use `gh issue create` if available.

2. **Pull Request**
   - Open a PR from the feature branch to the target branch:
     - Title: from the packet or derived from the task name.
     - Description must include:
       - Problem summary
       - Implementation summary
       - Testing summary (commands + key scenarios)
       - Any deviations from the packet
       - `Fixes #<issue-number>` to auto-close the issue.

3. **Linking**
   - Ensure the GitHub issue and PR are properly linked.
   - If auto-linking fails, add a comment on the issue linking to the PR.

---

## Phase 6: Verification & Closure

1. **Local / Dev Validation**
   - Follow the **“Manual Validation Steps”** from the packet.
   - Verify logs/metrics based on the **“Logs / Metrics to Check”** section.

2. **Status Report to User**
   - Post a concise but complete summary including:
     - What changed (high-level)
     - Files or areas heavily touched
     - Tests run and their status
     - Links/identifiers for:
       - GitHub issue
       - PR
       - Any notable logs/dashboards (if applicable)
     - Known limitations or follow-up items

3. **Wait for User Confirmation**
   - Do **not** manually close the issue yourself unless:
     - It is clearly configured to close via `Fixes #<issue-number>`, _and_
     - The user has confirmed it works, or this is a trivial/low-risk change.

4. **After Confirmation**
   - Ensure the GitHub issue is closed (via PR merge or manual action).
   - Update any remaining TodoWrite items as complete.
   - Suggest concrete follow-up issues if the packet uncovered new tech debt.

---

## Response Style

When responding to the user (outside of terminal actions):

- Keep it structured and concise.
- Use checklists and bullet points.
- Focus on:
  - What you did
  - What you tested
  - Where it lives (branch/PR/issue)
  - What you recommend next

Do not restate the entire packet; only report on execution and deviations.
