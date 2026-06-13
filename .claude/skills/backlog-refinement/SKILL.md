---
name: backlog-refinement
description: Prepare the GitHub backlog for autonomous /agent-loop completion — assess each open issue against the agent-readiness rubric, auto-rewrite agent-shaped issues into agent-ready form (acceptance criteria, file pointers, out-of-scope guardrails) and tag dev:agent, exclude the rest with agent-bail:* reasons, and run the post-loop RCA aggregation that sharpens the rubric from every bail. Use before running /agent-loop, after triaging new issues, or to close the learning loop after a loop run.
argument-hint: 'mode [args]: queue | refine [n|--all|--limit N] | assess <n> | rca [run-window] | help'
---

# /backlog-refinement

Maximize how much of the backlog `/agent-loop` can complete unattended, and **learn from every failure** so the backlog and the loop both get smarter over time.

This skill is one half of a closed loop with `/agent-loop`:

```
/backlog-refinement (prep)  →  dev:agent queue  →  /agent-loop (consume)
        ▲                                                   │
        └──────────  RCA sharpens the rubric  ◀── agent-bail:* on bail
```

The criteria, taxonomy, and templates live in **[`RUBRIC.md`](./RUBRIC.md)** (the single source of truth, shared with `/agent-loop`). The accumulated root-cause analyses live in **[`LEARNINGS.md`](./LEARNINGS.md)**. Both are consumer-owned (bootstrapped once from `RUBRIC.md.template` / `LEARNINGS.md.template`, then customized per repo). **Read `RUBRIC.md` fully before acting** — it defines §1 readiness criteria, §2 make-ready transformations, §3 disqualifiers (including any repo-specific ones), and the label model.

> **Integration branch.** This skill says "verify against the integration branch" throughout — that is whatever branch your repo's `agent-loop-instructions.md` opens PRs against (`origin/main` for most repos, `origin/staging` for repos with a staging→main promotion flow). Substitute your repo's value.

**Arguments**: `$ARGUMENTS` — dispatch on the first word; default to `queue`.

---

## Mode: `queue` (default)

Show the refinement backlog — open issues not yet assessed (`agent: refined` absent) — so the operator can see what's left to prepare.

```bash
python3 .claude/skills/backlog-refinement/scripts/candidates.py
# --json for machine output, --limit N, --include-refined to also show assessed issues
```

Report counts: total open, already `dev: agent` (split into **ready** = also `agent: refined` vs **re-verify** = `dev: agent` without `agent: refined`), already `agent-bail:*`, and the un-refined remainder (the work).

> **Auto-managed skip.** Issues a scheduled workflow both opens and closes (e.g. a nightly metrics/digest issue) are never refinement tasks — and commenting on one resets its `updatedAt`, delaying the workflow's auto-close. Declare their labels in the `<!-- auto-managed-labels: … -->` marker in `RUBRIC.md` (§3); `candidates.py` routes matching issues to a `skipped` bucket that never enters the queue and never counts as un-refined. Empty marker → no skipping.

> **Re-verify bucket.** A `dev: agent` issue lacking `agent: refined` was tagged by something other than this skill (older triage, bulk import, a parallel pass) and has **never been verified-against-HEAD**. `candidates.py` surfaces these separately because `refine --all` walks only the un-refined bucket and silently skips them — leaving stale, pre-tagged work to feed `/agent-loop`. **Clear the re-verify bucket before trusting the queue** (see `refine` below).

## Mode: `refine [n | --all | --limit N]`

Prepare issues for the loop. Default refines the next un-assessed issue; `--limit N` processes a batch; `--all` walks the whole un-refined backlog. For each issue:

> **Re-verify pre-tagged `dev: agent` FIRST.** `--all` processes only the un-refined bucket — it does **not** touch `dev: agent` issues that lack `agent: refined`. Those are pre-tagged and unverified, and they are exactly what `/agent-loop` consumes, so a plain `refine --all` leaves the loop's real queue stale. Before (or alongside) `refine --all`, run the same per-issue steps below over `gh issue list --label "dev: agent"` filtered to those without `agent: refined` (the `candidates.py` **re-verify** bucket): strip `dev: agent` + add the matching `agent-bail:` on failures, add `agent: refined` on passes. Sanity-check the auto-rewrite on a handful (`assess <n>` or `refine --limit 5`) before a large sweep, since it mutates issue bodies at scale.

1. **Read it fully** — `gh issue view <N>` including comments.
2. **Early-exit excludes** — if the title/body matches a §3 Bucket-B disqualifier on its face (`Epic:`, `extractable as @`, obvious cross-repo/credential/synced-surface/repo-sensitive), apply `agent: refined` + the `agent-bail:` label, add a one-line comment citing the rubric clause, and move on. Don't over-invest in clearly-excluded issues.
3. **Verify-against-HEAD** (RUBRIC §2, highest-value check). Fetch the integration branch; determine whether the described problem still reproduces:
   - **Already fixed** → `agent: refined` + `agent-bail: stale`, comment with the evidence (commit/PR/file:line that shipped it) and recommend close. Do **not** tag `dev: agent`. Do **not** close it yourself (human triage gate).
   - **Partially shipped** → re-scope: rewrite the body to the residual only, then continue assessing the residual.
   - **Still open** → continue.
4. **External-dep check** (RUBRIC §2) — if the issue depends on a package/service, confirm it's published and consumable from this repo. If not → `status: blocked` + `agent-bail: cross-repo`, comment, skip.
5. **Assess against §1.** If it fails a Bucket-B criterion → exclude with the matching `agent-bail:` label + comment. If it fails a Bucket-A criterion that §2 can fix → apply the transformation.
6. **Auto-rewrite.** When the issue passes (possibly after §2 transforms), **rewrite the body** to the RUBRIC §5 agent-ready template:
   - Preserve the original verbatim under a `> ### Original report` blockquote — never destroy human intent.
   - Fill Goal / Acceptance criteria / Files-entry-points / Out-of-scope, grounding file pointers in real `grep`/`Read` results.
   - Write the new body to a repo-scoped temp path (e.g. `/tmp/<repo>/refine-body-<N>.md`), then `gh issue edit <N> --body-file <path>` (avoids heredoc permission prompts).
   - Apply `dev: agent` + `agent: refined`.

   > Some teams prefer refinement to **suggest** the rewrite as a comment rather than edit the body directly. If so, post the proposed agent-ready body as a comment and leave the body untouched; a human applies it. Pick one mode per repo and note it in `RUBRIC.md`.

7. **Never** rewrite an issue into a scope it didn't ask for, invent acceptance criteria you can't ground in the code, or tag `dev: agent` on anything that fails §1. When uncertain between make-ready and exclude, **exclude** — a false-positive `dev: agent` costs a whole wasted loop iteration; a false-negative just waits for a human.

Batch etiquette: process sequentially, one `gh` mutation at a time; summarize at the end (X tagged ready, Y excluded by category, Z re-scoped).

## Mode: `assess <n>`

Dry-run a single issue: print the §1 verdict, which §2 transformations would apply, and the proposed rewritten body — **without** mutating the issue. Use to sanity-check the rubric's judgement before a big `refine --all`.

## Mode: `rca [run-window]` — post-loop aggregation (closes the learning loop)

Run **after** an `/agent-loop` run. Turns the run's bails into rubric improvements.

```bash
python3 .claude/skills/backlog-refinement/scripts/bail-report.py        # all agent-bail:* issues
python3 .claude/skills/backlog-refinement/scripts/bail-report.py --since 2026-01-01  # window
```

For each bailed issue (and its `<!-- agent-loop-rca ... -->` stub, RUBRIC §4):

1. **Re-ask the two questions** (RUBRIC top) on the actual outcome — don't just trust the inner agent's self-classification; confirm the bucket.
2. **Bucket A bail = a refinement miss.** This is the most valuable signal: refinement tagged `dev: agent` on something the loop couldn't finish. Ask "what §2 transformation or §1 check would have caught this at prep time?" and write it.
3. **Bucket B repeat = a dull disqualifier.** If the same inherent shape bailed more than once, the §3 pattern isn't catching it early — sharpen the wording so future refinement excludes it on sight.
4. **Loop-mechanics bail = an instructions/script gap.** Record the fix that belongs in `agent-loop-instructions.md` / `prompt.txt`; if it needs the synced `agent-loop.sh`, note it as an upstream change (don't edit synced files locally).
5. **Append a dated entry to `LEARNINGS.md`** for each distinct lesson (template below), and if any §1/§2/§3 criterion changed, **bump the rubric version** at the top of `RUBRIC.md` and note the bump in `LEARNINGS.md`.

Every loop run should leave the rubric at least as sharp as it found it. A run that produced bails but no `LEARNINGS.md` entry is an incomplete RCA.

### `LEARNINGS.md` entry template

```markdown
### <date> — #<issue> — <short title> [bucket A|B | <agent-bail category>]

- **Outcome:** PREVENTABLE | INHERENT
- **What could we have done differently:** <the answer to question 1, or "nothing — inherent">
- **Rubric/loop change:** <the concrete §2 transformation, §3 disqualifier, or agent-loop-instructions line this produced>
- **Evidence:** <commit / file:line / comment link>
```

## What this skill must not do

- **Don't edit synced-from-upstream files** (the `agent-loop` SKILL/script, shared instruction files, other skills' `SKILL.md`). Loop-side fixes go in `agent-loop-instructions.md` / `prompt.txt`, or upstream.
- **Don't close or reassign issues** — refinement labels and recommends; humans close. (Stale issues get `agent-bail: stale` + a close recommendation, not a close.)
- **Don't tag `dev: agent` on §3 Bucket-B work** — synced-surface, credential-gated, open-decision, cross-repo, or any repo-specific sensitive path (e.g. compliance/PHI/PII/encryption/audit) — those are exclusions by definition.

## Relationship to `/issues`

`/issues` is the day-to-day workflow (ready queue, claim, link). `/backlog-refinement` is the upstream curator that decides _what earns the `dev: agent` label that `/issues ready --agent` and `/agent-loop` key on_. Run refinement before a loop; run `rca` after.
