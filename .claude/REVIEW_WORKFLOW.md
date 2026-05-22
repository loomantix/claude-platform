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
- Explicit "review this carefully" / "this is high-risk" request

Claude sessions should proactively recommend deep when these signals are present rather than waiting to be asked.

### When to skip the chain

The chain is theatre on small / no-code changesets. Each skill applies the same triviality heuristic:

- **Source-code change present** (any of `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`, `.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, `.bash`) → run the full chain.
- **Only docs/config/fixtures** (`.md`, `.txt`, `.yml`, `.yaml`, `.json`, `.toml`, `.gitignore`, `LICENSE`, `CHANGELOG`, `README`, files under `docs/`, `*.fixture.*`, snapshot files) → `/refactorpass` and `/grill` skip silently; `/reviewit` prompts to either run the full chain on the doc content or skip everything.
- **Mixed** (some source, some docs/config) → full chain — source files justify the spend.

The chain is recommended on every source-code PR but not enforced — there's no push-gate. Trust + post-process audit, not pre-push blocking.

---

## Why this shape

- **`/simplify` is refactor-positive by design.** Pre-push, that's the intended stance — consolidate fresh code freely. Bot reviewers (Copilot, Gemini) post-push are scope-controlled by `copilot-instructions.md` precisely because expanding scope post-PR creates review burden. Different stages, different stances.
- **Auto-trigger of Copilot and Gemini is intentionally OFF.** Both are configured manual-only. This gives cost control on Gemini (Flash is $0.05–$0.20, Pro is $1–$8 — auto-firing on every iteration push burns money), audit-trail completeness (the PR comment history captures the full dialogue), and predictable reviewer state (no bot reviewing a stale commit while a fix push is in flight).
- **`/reviewit` is the only path that fires AI review.** Don't try to manually `gh workflow run "Gemini Code Review"` or request Copilot as a reviewer outside the skill — the skill handles ordering, staged deduplication, and reply threading. Manual invocation outside the skill produces orphaned findings.
- **Copilot is slower than Gemini Flash.** `/reviewit` should not leave Gemini findings idle while waiting for Copilot. It fires both reviewers, handles Gemini first, then polls Copilot for the original head before deciding whether another iteration is needed.
- **No push-gate.** The chain is trust-based, not enforced. The cost of a PreToolUse gate (extra manual approvals on every push) outweighed the belt-and-suspenders value once the skills themselves were stable. Skipping `/refactorpass` shows up in post-process audits, not as a push-time block.

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
- [`.github/workflows/sync-from-upstream.yml.template`](../.github/workflows/sync-from-upstream.yml.template) — canonical sync workflow template
- [`scripts/create-signed-commit.py`](../scripts/create-signed-commit.py) — Contents-API commit creator
