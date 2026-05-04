# Contributing to claude-platform

Thank you for considering a contribution. This repo is the upstream source-of-truth for a set of Claude Code skills, agents, and a sync engine that propagates them to consumer repos. Bugs here propagate to every downstream consumer; the bar on review and testing is therefore deliberately high.

## License

This project is licensed under [Apache 2.0](./LICENSE). All contributions are licensed under the same terms.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a Contributor License Agreement. By signing off on your commits, you certify the contribution is your own work or you have the right to submit it under the project's open-source license.

**Every commit must be signed off**, with the trailer:

```
Signed-off-by: Your Real Name <your.email@example.com>
```

Use `git commit -s` to add the trailer automatically. CI rejects PRs with unsigned commits.

The full DCO text is at https://developercertificate.org/. By signing off, you are agreeing to it.

## What we accept

**In scope:**

- New Claude Code skills that have value across multiple consumers (avoid skills that are tightly coupled to a single project's conventions — those belong in that project's repo).
- Improvements to existing skills (`/grill`, `/refactorpass`, `/reviewit`, `/issues`, `/feature-dev`, `/copilot-review`, `/agent-loop`, `/deepgrill`, `/phone-install`, `/task-packet`).
- Improvements to the sync engine (`scripts/sync-engine.py`, `scripts/create-signed-commit.py`) that make it more robust, more portable, or safer to operate.
- Improvements to the consumer-side workflow template (`.github/workflows/sync-from-upstream.yml.template`).
- Documentation, examples, contract clarifications.
- Bug fixes anywhere.

**Out of scope (please open an issue first to discuss):**

- New layers in the sync model (e.g. inheritance between manifests, recursive imports). The simple "one upstream, one consumer, one manifest" shape is intentional.
- Skills that bind to a specific tech stack in their core (e.g. a skill that only works on Rails, or only on Expo) — those belong in stack-specific repos.
- Hooks that auto-fire on every PR (the project deliberately keeps post-PR review manual via `/reviewit`).

## Workflow

1. **Open an issue** describing the change. For non-trivial changes, get rough alignment before opening a PR.
2. **Fork the repo and create a feature branch**. Branch names: `feat/<short-description>`, `fix/<short-description>`, `docs/<short-description>`.
3. **Make your changes**, with `git commit -s` (DCO sign-off) on every commit.
4. **Run CI locally** — see CI workflow for the exact commands.
5. **Open a PR** against `main`. CI must pass.

## Skill-edit conventions

- Edit upstream only. The skill files in this repo are the source of truth for every consumer; edits in a consumer repo will be overwritten on next sync.
- Keep SKILL.md content **operational** — what to do, in what order, with what guard rails. Avoid historical "why this was added" narrative; that belongs in the PR description and rots quickly.
- Prefer adjusting an existing skill over forking it. Three skills with overlapping scope is a maintenance tax on every consumer.

## Sync-mechanism rules

- Changes to `scripts/sync-engine.py` or `scripts/create-signed-commit.py` are **sync-propagating** — they ship to every consumer on the next `sync-v1` retag. Treat these as the highest-stakes files in the repo. Add tests where the existing surface lacks them; review extra carefully for path-traversal, token-exfil, or unintended-write paths.
- `scripts/sync-targets.yml` is the canonical manifest. Adding to the sync surface = add an entry here. New entries should be well-commented; consumers that don't need a particular file opt out via `skip_targets` in their own `.platform-config.yml` rather than us splitting the manifest.

## Security

If you discover a security issue, do **not** open a public issue. See [`SECURITY.md`](./SECURITY.md) for the responsible-disclosure process.

## Code of Conduct

By participating you agree to abide by the [Code of Conduct](./CODE_OF_CONDUCT.md).
