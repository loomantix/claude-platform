# Review workflow — canonical

This file is synced from the upstream repo to every consumer repo. Edits in a consumer repo will be overwritten on next sync — make changes upstream.

Each consumer repo's `CLAUDE.md` should reference this document as the authoritative description of the AI review workflow. Claude sessions working in a consumer should follow it without exception.

---

## The flow

Two paths exist. Pick the right one before starting.

### Default (lean) — most PRs, ~one-third the token cost

1. **Make changes locally.** Normal coding. No review steps yet.
2. **Run `/refactorpass`.** Runs `/simplify` once on local changes and commits the result. **Skipped automatically on docs/config-only changesets.**
3. **Run `/grill`.** Adversarial pass with two highest-signal agents: `code-reviewer` (always) + `silent-failure-hunter` (when error/async signals present). Aggregates findings, requires per-finding verification from you (fix / defer / ignore-with-rationale). Critical findings cannot be silently ignored. **Skipped automatically on docs/config-only changesets.**
4. **Push and open the PR.** `git push` + `gh pr create`.
5. **Run `/reviewit <pr-number>`.** Fires Gemini Flash + Copilot at the same iteration watermark, fixes Gemini findings first, then folds in Copilot once it finishes, **2-iteration review cap**. No in-skill `/review` — `/grill` already covered Claude-side review pre-push, so a second Claude-side pass post-push was redundant in lean mode (and historically broke `/reviewit`'s polling loop when fired inline). Review-fix commits push directly; no per-iter `/refactorpass` (the base was refactor-passed in step 2, and re-running `/simplify` on small surgical fixes has been validated to add negligible value).
6. **Review and merge.**

### Deep — complex or high-risk changes, full historical chain

1. **Make changes locally.**
2. **Run `/deepgrill`.** Orchestrator that runs `/refactorpass` + `/grill deep` (full agent matrix: code-reviewer, silent-failure-hunter, type-design-analyzer, comment-analyzer, pr-test-analyzer, security-review).
3. **Push and open the PR.**
4. **Run `/reviewit <pr-number> deep`.** Same two bot reviewers as lean (Gemini Flash + Copilot) with the same staggered handling (Gemini first, then Copilot folded in), but with a **4-iteration cap** and an **early-exit when an iteration produces no `fix` resolutions** across either pass (defer/dismiss-only doesn't justify another round on an unchanged HEAD). Between iter 2 → 3 and iter 3 → 4, a **cost-shift checkpoint** also pauses when fixes are still being produced but findings aren't converging (any critical, or critical+suggestion+nitpick ≥ 5 post-dedup) — three exits: continue the chain, bail early to the final `/deepgrill` (skipping remaining paid iters), or stop and merge as-is (skipping `/deepgrill` too). After the loop exits for any reason except merge-as-is — clean, no-fix early-exit, cost-shift bail-out, or cap — `/reviewit` invokes `/deepgrill` as a sub-skill so fresh Claude-side agents (`code-reviewer`, `silent-failure-hunter`, `type-design-analyzer`, `comment-analyzer`, `pr-test-analyzer`, `security-review`) look at the PR's current state in a separate session. The old in-loop `/review` is gone — moving it to a single post-loop `/deepgrill` invocation avoids the orchestration trap where `/review`'s self-contained prompt caused `/reviewit` to drop out of polling early.
5. **Review and merge.**

### When to use deep

The lean default is right for ~80% of PRs. Reach for `/deepgrill` when **any** of these apply:

- Touches `.claude/skills/**`, `scripts/sync*`, `.github/workflows/**` (sync-propagating — bugs land in every downstream consumer)
- Touches authentication, crypto, secret handling, sensitive-data paths
- Schema migration or data-shape change
- Large refactor (>20 files modified or >500 lines net)
- Bug fix in an area with prior recurring incidents
- Customer/tenant-variable behavior: vendor/third-party integrations, per-tenant config, prompt/output generation, data normalization — the **tenant-coupling lens** (catches one customer's data/config hardcoded into shared logic) runs in deep mode; see [`skills/grill/SKILL.md`](skills/grill/SKILL.md)
- Explicit "review this carefully" / "this is high-risk" request

Claude sessions should proactively recommend deep when these signals are present rather than waiting to be asked.

### When to skip the chain

The chain is theatre on small / no-code changesets. Each skill applies the same triviality heuristic:

- **Source-code change present** (any of `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`, `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`) → run the full chain.
- **Only docs/config/fixtures** (`.md`, `.txt`, `.yml`, `.yaml`, `.json`, `.toml`, `.gitignore`, `LICENSE`, `CHANGELOG`, `README`, files under `docs/`, `*.fixture.*`, snapshot files) → `/refactorpass` and `/grill` skip silently; `/reviewit` prompts to either run the full chain on the doc content or skip everything.
- **Mixed** (some source, some docs/config) → full chain — source files justify the spend.

The chain is recommended on every source-code PR but not enforced — there's no push-gate. Trust + post-process audit, not pre-push blocking.

### Optional add-on: accessibility

`/review-accessibility` is not part of the default or deep chain and isn't triggered automatically — run it yourself whenever a change touches UI (new component, layout change, form, anything rendering to the DOM), or periodically against a whole app. A human explicitly invokes it; from there it's fully automated — axe-core scans every route (auto-discovered, or a specific list you pass), every violation it reports gets fixed directly in source, and the run ends by opening a PR summarizing the changes. Automated scanning covers WCAG A/AA plus best-practice rules and is not a compliance certification; the skill doc spells out what isn't covered. There's no per-violation approval step in the browser — review happens on the PR diff, same as any other change. See [`skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md).

---

## Why this shape

- **`/simplify` is refactor-positive by design.** Pre-push, that's the intended stance — consolidate fresh code freely. Bot reviewers (Copilot, Gemini) post-push are scope-controlled by `copilot-instructions.md` precisely because expanding scope post-PR creates review burden. Different stages, different stances.
- **Auto-trigger of Copilot and Gemini is intentionally OFF.** Both are configured manual-only. This gives cost control on Gemini (Flash is $0.05–$0.20, Pro is $1–$8 — auto-firing on every iteration push burns money), audit-trail completeness (the PR comment history captures the full dialogue), and predictable reviewer state (no bot reviewing a stale commit while a fix push is in flight).
- **`/reviewit` is the only path that fires AI review.** Don't try to manually `gh workflow run "Gemini Code Review"` or request Copilot as a reviewer outside the skill — the skill handles ordering, staged deduplication, and reply threading. Manual invocation outside the skill produces orphaned findings.
- **Copilot is slower than Gemini Flash.** `/reviewit` should not leave Gemini findings idle while waiting for Copilot. It fires both reviewers, handles Gemini first, then polls Copilot for the original head before deciding whether another iteration is needed.
- **No push-gate.** The chain is trust-based, not enforced. The cost of a PreToolUse gate (extra manual approvals on every push) outweighed the belt-and-suspenders value once the skills themselves were stable. Skipping `/refactorpass` shows up in post-process audits, not as a push-time block.

---

## Why the pre-push chain is two passes, in this order

A common misread of the pre-push chain is that `/refactorpass` is a cheap pre-filter that reduces the work the expensive `/grill` agents have to do — clean up the easy stuff so the costly pass has less to chew on. That model is wrong, and the ordering only makes sense once you discard it.

**The two passes have orthogonal jobs.**

- `/refactorpass` (wrapping `/simplify`) is _constructive_. It looks for consolidation and simplification: DRY violations, dead code, repeated blocks worth extracting, unclear names. It rewrites the _shape_ of the code and commits the result.
- `/grill` is _adversarial_. Its agents look for defects: `code-reviewer` for convention and logic bugs, `silent-failure-hunter` for swallowed errors and fallback that masks failures, and in deep mode `type-design-analyzer`, `comment-analyzer`, `pr-test-analyzer`, and `/security-review` for their respective failure classes. It finds what's _wrong_, not what's _messy_.

**Because the jobs are orthogonal, the finding-overlap is thin — and that means `/refactorpass` is not a cost-saving filter.** Tidying duplication doesn't make `silent-failure-hunter` cheaper, and it doesn't stop `code-reviewer` from finding a real bug. The grill agents reason over the whole diff regardless of how clean it is; their cost is dominated by agent reasoning, not by diff size, so a tidier diff barely moves the bill. The only genuine overlap is `code-reviewer` occasionally flagging duplication or dead code that `/simplify` would have already removed — a thin slice. The real payoff of running `/refactorpass` first is not cheaper grilling; it's that the adversarial pass — and the bot and human reviewers after it — examine the code in the shape it will actually ship in, instead of a shape that's about to change.

**That payoff is also why the two run sequentially rather than in parallel.** Given the thin overlap, parallelizing them to save wall-clock looks tempting. It doesn't work, because the passes aren't independent operations even though their concerns are: `/simplify` is a writer and `/grill` is a reader of what it wrote. Run them concurrently and:

- grill anchors each finding to a `file:line` location that `/simplify` is actively rewriting — so by the time you act on "bug at `foo.ts:42`", that code may have moved into a helper and the anchor points at nothing;
- the thin overlap flips from "deduped for free" to pure waste — you pay an agent to critique a duplicated block one moment before `/simplify` deletes it;
- and, most importantly, grill ends up critiquing a shape that won't ship instead of the post-refactor shape that will.

There _is_ a defensible partial-parallel design: the grill agents that provably never touch `/simplify`'s surface — `silent-failure-hunter` on error-handling logic, `pr-test-analyzer` on test files — have low staleness risk and could in principle run against the pre-refactor tree. It's deliberately not done. The orchestration ("these agents in parallel, those after") buys only wall-clock, not token cost — the agents cost the same whichever order they run in — and the pre-push step is human-gated anyway: you verify each grill finding interactively, so a minute saved overlapping the agent runs vanishes against the verification time. Not worth the complexity or the residual staleness risk.

**The mental model:** orthogonal in what they look for, sequentially dependent in that one rewrites what the other reads. `/deepgrill` encodes the ordering directly — it runs `/refactorpass`, then `/grill deep`, as a single chain.

---

## What about the post-PR enforcement?

There's currently NO hard gate forcing `/reviewit` to be invoked once a PR is open — a session can end with the PR mergeable but unreviewed by AI. The eventual lever is GitHub branch protection requiring an `ai-review-complete` status check that only `/reviewit` can post (with an emergency-label escape hatch). Until that ships, the workflow relies on Claude sessions reading this document and following it. If you're a Claude session: invoke `/reviewit` after every PR creation. If you're a developer reviewing this with a colleague: same expectation.

---

## Skill quick reference

### Default (lean) path

| Skill            | When                               | What it does                                                                                                                                   |
| ---------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `/refactorpass`  | Before any push                    | Single `/simplify` pass + commit. Skips on docs-only.                                                                                          |
| `/grill`         | After `/refactorpass`, before push | code-reviewer + silent-failure-hunter (when relevant), user verifies findings. Skips on docs-only.                                             |
| `/reviewit <pr>` | After PR is open                   | Fire Gemini Flash + Copilot, handle Gemini first, then Copilot, dedup, fix, reply, push, loop ≤2. Prompts on docs-only to skip paid reviewers. |

### Deep path (high-risk or complex changes)

| Skill                 | When             | What it does                                                                                                                                                                                                                                                                                                                                                  |
| --------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/deepgrill`          | Before push      | Orchestrator: `/refactorpass` + `/grill deep` (full agent matrix).                                                                                                                                                                                                                                                                                            |
| `/reviewit <pr> deep` | After PR is open | Fire Gemini Flash + Copilot, handle Gemini first, then Copilot, dedup, fix, reply, push, loop ≤4 with early-exit on no-fix iters and a cost-shift checkpoint after iters 2 & 3 (continue / bail-to-`/deepgrill` / merge-as-is). Normally ends with a sub-skill `/deepgrill` so fresh Claude-side agents look at the PR's current state in a separate session. |

### Retired

| Skill           | Status                                                                      |
| --------------- | --------------------------------------------------------------------------- |
| `/pushit`       | Replaced by `/reviewit`. Stub removed — old invocations resolve to nothing. |
| `/review-cycle` | Replaced by `/reviewit`. Stub removed — old invocations resolve to nothing. |

---

## Override scenarios

- **`-F tier=pro`** in `/reviewit`'s Gemini fire: only when the user has explicitly asked for a deep review on a high-stakes PR (security/auth, schema migrations, large refactors). Costs $1–$8 per run. Default is `tier=flash`.
- **Branch protection bypass**: GitHub admins can always merge despite missing checks. This is a known and accepted gap.

---

## Sync workflow auth & commit signing (optional pattern)

When a consumer needs **verified commits** on sync PRs (e.g. for SOC 2 controls), the upstream-sync workflow can be configured to:

1. Authenticate as a GitHub App (org-installed with `contents: write` + `pull_requests: write`) via `actions/create-github-app-token@v1`. Required secrets on the consumer: `SYNC_APP_ID`, `SYNC_APP_PRIVATE_KEY` (rename in the workflow file if your conventions differ).
2. Create commits via the GitHub Contents API (`git/blobs` → `git/trees` → `git/commits` → `git/refs`) rather than `git commit` + `git push`. Commits made via the API path are auto-signed by GitHub: `committer: GitHub`, `verified: true`.

This decouples the committer (the App identity) from any human reviewer, so reviewers can approve sync PRs without violating segregation-of-duties controls. The reference template lives at `.github/workflows/sync-from-upstream.yml.template`; the API-side commit creation lives at `scripts/create-signed-commit.py` and runs from the cloned upstream repo at sync time.

## Cross-references

- [`.claude/skills/refactorpass/SKILL.md`](skills/refactorpass/SKILL.md) — pre-push refactor skill (single `/simplify` pass)
- [`.claude/skills/grill/SKILL.md`](skills/grill/SKILL.md) — pre-push adversarial pass (lean 2-agent default, deep full matrix)
- [`.claude/skills/deepgrill/SKILL.md`](skills/deepgrill/SKILL.md) — orchestrator for the deep pre-push chain
- [`.claude/skills/reviewit/SKILL.md`](skills/reviewit/SKILL.md) — post-push reviewer skill (lean: Gemini first, then Copilot, 2 iters; deep: same reviewers, 4 iters with early-exit + final `/deepgrill`)
- [`.claude/skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md) — optional, human-triggered a11y pass; scans, fixes in place, and opens its own PR, not part of the default/deep chain
- [`.github/workflows/sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template) — canonical sync workflow template
- [`scripts/create-signed-commit.py`](../scripts/create-signed-commit.py) — Contents-API commit creator
