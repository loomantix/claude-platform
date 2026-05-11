# claude-platform — Codex Project Guide

Upstream source of truth for Loomantix's Claude Code skills, agents, and sync engine. Apache 2.0 + DCO. See [README.md](README.md) for what ships here and [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Public-Repo Policy

This repo is public. Keep repository content suitable for public readers:

- Do not reference non-public repositories, systems, incidents, or trackers by name.
- Do not document deployment-specific wiring, app slugs, secret names, or escalation paths beyond the public templates.
- Keep compliance and security rationale generic; do not include organization-specific evidence or customer-specific details.
- Put project-specific consumer details in that consumer's own repository, not here.

If work needs non-public context, discuss that context outside this public repository and keep any public issue or PR focused on the reusable change.

## Working Rules

- Start each session by reading this file and checking `git status --short --branch`.
- Use `rg` / `rg --files` for search and file discovery.
- Use `apply_patch` for manual file edits where practical.
- Do not revert user changes or unrelated dirty worktree state.
- Keep changes scoped to the user's request and the existing repo architecture.
- Run the smallest meaningful validation command after edits; report anything that could not be run.

## Cross-References

- [README.md](README.md) — what ships here, how to install skills, how to wire up a consumer.
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, scope, branch / commit / DCO conventions.
- [SECURITY.md](SECURITY.md) — responsible disclosure.
- [.claude/REVIEW_WORKFLOW.md](.claude/REVIEW_WORKFLOW.md) — canonical AI review chain.
