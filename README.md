# claude-platform

A reusable set of [Claude Code](https://claude.com/claude-code) skills, agents, and a sync engine that propagates them to consumer repos. Apache 2.0 + DCO.

> **Status:** v0.1 — initial extraction from Loomantix's internal platform repo. Production-tested across a four-repo fleet for ~6 months before extraction.

## What's in here

### Claude Code skills (`.claude/skills/`)

Operational skills you can install globally or sync into any repo:

| Skill | What it does |
|---|---|
| `/refactorpass` | Pre-push wrapper around `/simplify` — single-pass refactor, commits the result, skips on docs-only changesets. |
| `/grill` | Pre-push adversarial review. Lean default runs `code-reviewer` + `silent-failure-hunter`; `deep` arg runs the full agent matrix. |
| `/deepgrill` | Orchestrator for the deep pre-push chain (`/refactorpass` + `/grill deep`). |
| `/reviewit <pr>` | Post-push AI review orchestrator. Lean default fires Gemini Flash + Copilot, caps at 2 iterations. `deep` arg adds `/review` and restores 4-iter cap. Dedups, addresses each finding, replies in the PR thread. |
| `/copilot-review <pr>` | Address GitHub Copilot review comments on a PR systematically. |
| `/feature-dev` | Guided feature development — discovery → architecture → implementation → quality review. |
| `/issues` | Thin workflow over `gh issue` with a dependency-aware ready queue. Parses `Blocked by #N` / `Depends on #N` from issue bodies. |
| `/agent-loop` | Autonomous Claude relay over the `/issues` ready queue. Claims an issue, spawns a fresh Claude session, pushes results to a collection branch, repeats. |
| `/task-packet` | Execute a markdown Task Packet end-to-end (code, tests, GitHub issue, PR, closure). |
| `/phone-install` | Build a release APK from the consumer repo and install it on a tethered Android device over wireless ADB. |

### Custom sub-agents (`.claude/agents/`)

Three agent definitions invoked by the skills above:

- `code-explorer` — traces feature execution paths across an existing codebase.
- `code-architect` — designs implementation blueprints by analyzing existing patterns.
- `code-reviewer` — confidence-filtered code review against project conventions.

### Sync engine (`scripts/`)

A two-script mechanism that lets one upstream repo propagate canonical files (skills, workflows, docs) to many consumers via a daily-cron PR:

- `sync-engine.py` — reads `sync-targets.yml` from the upstream and a `.platform-config.yml` from the consumer, applies `<<KEY>>` substitutions, writes / deletes destination files. Idempotent; hard-fails on missing required substitutions; soft-warns on undeclared placeholders.
- `create-signed-commit.py` — creates the sync commit via the GitHub Contents API rather than `git commit + git push`. Commits made via the API are auto-signed by GitHub (`committer: GitHub`, `verified: true`) when invoked with a GitHub App installation token, satisfying SOC 2 / ISO 27001 controls that require attested-actor sign-off.

The reference consumer-side workflow lives at [`.github/workflows/sync-from-upstream.yml.template`](.github/workflows/sync-from-upstream.yml.template). Drop it into a consumer, fill in `UPSTREAM_REPO`, set the App-token secrets, and the consumer pulls daily on a `sync-v1` tag.

### Other

- `.claude/REVIEW_WORKFLOW.md` — canonical doc describing the lean / deep AI review chains. Sync this into every consumer's `.claude/` so Claude sessions follow the same flow.
- `.github/copilot-instructions.md.template` — substitution-driven Copilot reviewer prompt. Each consumer fills in `PROJECT_NAME`, `STACK_TABLE`, `CODE_RULES`, etc. via `.platform-config.yml`.
- `claude/github-api-usage.md` — drop-in guidance for any repo's `CLAUDE.md` on rate-limit-aware GitHub API usage.

## Install (developer-side)

To install the skills into your local Claude Code:

```bash
git clone https://github.com/loomantix/claude-platform.git
cd claude-platform
./scripts/install-skills.sh           # symlinks each skill into ~/.claude/skills/
./scripts/install-skills.sh --dry-run # report what would happen, write nothing
./scripts/install-skills.sh --force   # replace existing entries (backed up)
```

Updates flow via `git pull` in the clone — no re-install needed unless new skills are added.

## Wire up a consumer repo

See [`docs/getting-started.md`](docs/getting-started.md) for the full walkthrough. Short version:

1. Create a `.platform-config.yml` at the consumer's root with the substitution values.
2. Copy `.github/workflows/sync-from-upstream.yml.template` → `.github/workflows/sync-from-upstream.yml`, fill in `UPSTREAM_REPO`.
3. Create a `scripts/sync-targets.yml` (consumer-owned) listing which upstream files to pull. Use [`scripts/sync-targets.yml.example`](scripts/sync-targets.yml.example) as a starting point.
4. Set the App-token secrets on the consumer (or as org-level secrets).
5. Run the workflow once via `gh workflow run "Sync from upstream"` — the first PR opens cleanly.

## How to think about this project

The skills assume you're using a specific review chain (lean by default, deep for high-risk changes; pre-push `/grill` + post-push `/reviewit`; manual-only Gemini and Copilot). That chain is documented in [`.claude/REVIEW_WORKFLOW.md`](.claude/REVIEW_WORKFLOW.md). If you adopt the skills, adopting the chain too is the path of least friction. If you want a different chain, fork — the skills are small and the orchestration is explicit.

The sync engine is intentionally minimal:

- One upstream, one consumer, one manifest.
- `<<KEY>>` find-and-replace, no template engine.
- Daily PR open / merge cycle, with a tag-based gate (`sync-v1`) so unintended pushes to upstream main don't auto-propagate.
- `delete: true` to retire a previously-synced file across all consumers.

It's not Renovate. It's not Dependabot. It's a deliberately small primitive for "one upstream, many consumers, propagate-by-PR."

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Apache 2.0 + DCO sign-off (`git commit -s`) required on every commit.

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
