# claude-platform

A reusable [Claude Code](https://claude.com/claude-code) toolkit for teams that want the same agent workflows in more than one repository. It ships operational skills, supporting sub-agents, and a small sync engine that opens propagation PRs against downstream repos.

Apache 2.0 + DCO.

> **Status:** v0.1 — public bootstrap release. APIs and workflows may evolve as the sync surface stabilizes.

## Why this exists

Claude Code project setup tends to drift as soon as a team has several repos: one repo gets a better review prompt, another gets a safer issue workflow, and a third still has last month's instructions. This repo keeps that surface reviewable in one place while still letting each downstream repo own its local project context.

Use this project if you want:

- A repeatable pre-push and post-push AI review chain.
- Repository-local skills that every teammate can invoke the same way.
- A pull-request-based sync flow instead of direct writes to downstream default branches.
- Public, auditable defaults for DCO, Copilot instructions, and Claude Code guidance.

## What's in here

### Claude Code skills (`.claude/skills/`)

Operational skills you can install locally or sync into a repo:

| Skill                   | What it does                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/refactorpass`         | Pre-push wrapper around `/simplify` — single-pass refactor, commits the result, skips on docs-only changesets.                                                                                                                                                                                                                                                                                                                  |
| `/grill`                | Pre-push adversarial review. Lean default runs `code-reviewer` + `silent-failure-hunter`; `deep` runs the full matrix plus the conditional tenant-coupling pass.                                                                                                                                                                                                                                                                |
| `/deepgrill`            | Orchestrator for the deep pre-push chain (`/refactorpass` + `/grill deep`).                                                                                                                                                                                                                                                                                                                                                     |
| `/reviewit <pr>`        | Post-push AI review orchestrator. Both modes fire Gemini Flash + Copilot with staggered handling (Gemini first, Copilot folded in) — no in-skill `/review`. Lean caps at 2 iterations. `deep` arg bumps the cap to 4, early-exits when an iteration produced no fix-pushes, and runs a final `/deepgrill` so fresh Claude-side agents look at the PR's current state. Dedups, addresses each finding, replies in the PR thread. |
| `/codex-review`         | Independent second-opinion review of a PR or local diff via the Codex CLI (a different model family). Read-only by default; pairs with `/deepgrill` as the cross-review step before merge. `verify` arg lets Codex run tests/build.                                                                                                                                                                                             |
| `/copilot-review <pr>`  | Address GitHub Copilot review comments on a PR systematically.                                                                                                                                                                                                                                                                                                                                                                  |
| `/feature-dev`          | Guided feature development — discovery → architecture → implementation → quality review.                                                                                                                                                                                                                                                                                                                                        |
| `/issues`               | Thin workflow over `gh issue` with a dependency-aware ready queue. Parses `Blocked by #N` / `Depends on #N` from issue bodies.                                                                                                                                                                                                                                                                                                  |
| `/backlog-refinement`   | Curate the autonomous queue, verify issues against the integration branch, classify exclusions, and turn loop bails into rubric improvements.                                                                                                                                                                                                                                                                                   |
| `/agent-loop`           | Autonomous Claude relay over the refined `/issues` queue. Claims an issue, spawns a fresh Claude session, pushes results to a configurable-base collection branch, and repeats.                                                                                                                                                                                                                                                 |
| `/actions-usage-audit`  | Read-only GitHub Actions billing and workflow-usage analysis with month-over-month attribution.                                                                                                                                                                                                                                                                                                                                 |
| `/task-packet`          | Execute a markdown Task Packet end-to-end (code, tests, GitHub issue, PR, closure).                                                                                                                                                                                                                                                                                                                                             |
| `/phone-install`        | Build a release APK from the consumer repo and install it on a tethered Android device over wireless ADB.                                                                                                                                                                                                                                                                                                                       |
| `/review-accessibility` | Automated accessibility audit against a running web app — axe-core scans every route (auto-discovered or a specific list), fixes every violation in source, and opens a PR. Human-triggered, not part of the pre/post-push chain.                                                                                                                                                                                               |

### Custom sub-agents (`.claude/agents/`)

Three agent definitions invoked by the skills above:

- `code-explorer` — traces feature execution paths across an existing codebase.
- `code-architect` — designs implementation blueprints by analyzing existing patterns.
- `code-reviewer` — confidence-filtered code review against project conventions.

### Sync engine (`scripts/`)

A two-script mechanism that lets one upstream repo propagate canonical files (skills, workflows, docs) to many downstream repos via a scheduled PR:

- `sync-engine.py` — reads `sync-targets.yml` from the upstream and a `.platform-config.yml` from the consumer, applies `<<KEY>>` substitutions, writes / deletes destination files. Idempotent; hard-fails on missing required substitutions; soft-warns on undeclared placeholders.
- `create-signed-commit.py` — creates the sync commit via the GitHub Contents API rather than `git commit + git push`. Commits made via the API are auto-signed by GitHub (`committer: GitHub`, `verified: true`) when invoked with a GitHub App installation token.

The reference downstream workflow lives at [`.github/workflows/sync-from-upstream.yml.template`](.github/workflows/sync-from-upstream.yml.template). Drop it into a downstream repo, fill in `UPSTREAM_REPO`, set the App-token secrets, and the repo pulls daily from a `sync-v1` tag.

### Other

- `.claude/REVIEW_WORKFLOW.md` — canonical doc describing the lean / deep AI review chains. Sync this into each downstream repo's `.claude/` so Claude sessions follow the same flow.
- `.github/copilot-instructions.md.template` — substitution-driven Copilot reviewer prompt. Each downstream repo fills in `PROJECT_NAME`, `STACK_TABLE`, `CODE_RULES`, etc. via `.platform-config.yml`.
- `claude/github-api-usage.md` — drop-in guidance for any repo's `CLAUDE.md` on rate-limit-aware GitHub API usage.

## Getting started

There are two common paths: install the skills for your own Claude Code sessions, or wire this repo into another repository so the whole team gets the same checked-in workflow.

### Install locally

To install the skills into your local Claude Code:

```bash
git clone https://github.com/loomantix/claude-platform.git
cd claude-platform
./scripts/install-skills.sh           # symlinks each skill into ~/.claude/skills/
./scripts/install-skills.sh --dry-run # report what would happen, write nothing
./scripts/install-skills.sh --force   # replace existing entries (backed up)
```

Updates flow via `git pull` in the clone — no re-install needed unless new skills are added.

### Wire up a downstream repo

See [`docs/getting-started.md`](docs/getting-started.md) for the full walkthrough. Short version:

1. Create a `.platform-config.yml` at the downstream repo root with substitution values.
2. Copy `.github/workflows/sync-from-upstream.yml.template` to `.github/workflows/sync-from-upstream.yml`, then fill in `UPSTREAM_REPO`.
3. (Skip — the manifest [`scripts/sync-targets.yml`](scripts/sync-targets.yml) is upstream-owned and ships the full skill set. Forks can edit it to add or drop entries.)
4. Set the App-token secrets on the downstream repo or organization.
5. Run the workflow once via `gh workflow run "Sync from upstream"` — the first PR opens cleanly.

## How to think about this project

The skills assume you're using a specific review chain (lean by default, deep for high-risk changes; pre-push `/grill` + post-push `/reviewit`; manual-only Gemini and Copilot). That chain is documented in [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md). If you adopt the skills, adopting the chain too is the path of least friction. If you want a different chain, fork — the skills are small and the orchestration is explicit.

### Why the pre-push chain is two separate passes, in order

`/refactorpass` and `/grill` look like a cheap-filter-then-expensive-filter funnel — clean up the easy stuff first so the costly agents have less to do. They aren't, and understanding why explains the ordering.

The two passes have **orthogonal jobs**:

- `/refactorpass` (wrapping `/simplify`) is **constructive** — DRY, readability, dead-code removal, extracting repeated blocks. It changes the _shape_ of the code.
- `/grill` is **adversarial** — logic bugs, swallowed errors, type-design holes, missing test coverage. It hunts for _defects_.

Because they look for different categories of thing, the overlap is thin: tidying duplication doesn't make `silent-failure-hunter` cheaper or stop `code-reviewer` from finding a real bug. So running `/refactorpass` first is **not** a cost-saving pre-filter — the grill agents reason over the whole diff regardless of how clean it is, and their cost is dominated by agent reasoning, not diff size. The real payoff is landing the code in its final shape _before_ anything — adversarial agent, bot reviewer, or human — scrutinizes it.

That payoff is also why the two run **sequentially rather than in parallel**, despite the thin overlap. `/simplify` is a writer; `/grill` is a reader of what it wrote. Run them concurrently and grill anchors findings to `file:line` locations that simplify is actively rewriting — you get stale references, findings about code that's about to be deleted, and an adversarial pass critiquing a shape that won't ship. You want grill (and the reviewers after it) to scrutinize the code as it will actually merge.

The mental model: **orthogonal in what they look for, sequentially dependent in that one rewrites what the other reads.** `/deepgrill` bakes the ordering in — it runs `/refactorpass` then `/grill deep` as a single chain. For the full operational walkthrough and the post-push half (`/reviewit`), see [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md).

### The sync engine

The sync engine is intentionally minimal:

- One upstream, one downstream repo, one manifest.
- `<<KEY>>` find-and-replace, no template engine.
- Daily PR open / merge cycle, with a tag-based gate (`sync-v1`) so unintended pushes to upstream main don't auto-propagate.
- `delete: true` to retire a previously-synced file across all downstream repos.

It's not Renovate. It's not Dependabot. It's a deliberately small primitive for "one upstream, many downstream repos, propagate-by-PR."

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Apache 2.0 + DCO sign-off (`git commit -s`) required on every commit.

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
