# claude-platform — Claude project guide

Upstream source-of-truth for Loomantix's Claude Code skills, agents, and sync engine. Apache 2.0 + DCO. See [README.md](README.md) for what ships here and [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Public-repo policy

This repo is public. When working in it, do **not**:

- Reference any private Loomantix repository by name.
- Describe sync-engine internals tied to a private deployment (specific consumer wiring, internal repo names, App slugs, secret names beyond what is already in the public templates).
- Describe fleet topology (the number or identity of consumer repos, deployment cadence specifics, internal escalation paths).
- Describe the project's compliance posture (audit findings, control mappings, vendor relationships).

State the rule and the intent, not the list. The denylist that enforces this lives off-repo and stays private; the in-repo reminder is the human-facing half of the same policy.

If a piece of work would otherwise need to mention any of the above, file and discuss it in your private tracker and reference this repo from there — not the other way around.

## Cross-references

- [README.md](README.md) — what ships here, how to install skills, how to wire up a consumer.
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, scope, branch / commit / DCO conventions.
- [SECURITY.md](SECURITY.md) — responsible disclosure.
- [.claude/REVIEW_WORKFLOW.md](.claude/REVIEW_WORKFLOW.md) — canonical AI review chain.
