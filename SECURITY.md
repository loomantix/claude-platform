# Security

This repo ships Claude Code skills, agents, and a sync engine. The skills do not handle secrets directly, but the sync engine and `create-signed-commit.py` execute inside CI runners with privileged GitHub App tokens. A vulnerability in this repo could affect every downstream consumer that runs the sync workflow.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email **security@loomantix.com** with:

1. Description of the vulnerability
2. Steps to reproduce (or proof-of-concept)
3. Affected files / scripts / skills
4. Your name and contact (for follow-up)

You will receive an acknowledgement within 3 business days. We aim to triage within 7 business days and ship a fix within 30 days for confirmed vulnerabilities.

## Scope

In scope:

- Vulnerabilities in `scripts/sync-engine.py` or `scripts/create-signed-commit.py` that could allow path traversal, arbitrary file write, token exfiltration, or supply-chain compromise of downstream consumers.
- Vulnerabilities in `.github/workflows/sync-from-upstream.yml.template` (the canonical consumer-side workflow) that could leak secrets, escalate permissions, or weaken the App-token boundary.
- Skill instructions that could be weaponized to drive Claude into destructive actions (e.g. unintended `git push --force`, secret disclosure, mass-modification beyond stated scope) when the skill is invoked under its documented contract.
- CI/build supply-chain vulnerabilities affecting this repo's own pipelines.

Out of scope:

- Vulnerabilities in upstream dependencies (PyYAML, GitHub Actions used by the workflow templates) — please report to the upstream.
- Vulnerabilities in Claude itself or in the Claude Code CLI — report to Anthropic.
- Misconfiguration of a *consumer* repo (e.g. a consumer setting `SYNC_APP_PRIVATE_KEY` to an over-privileged App). The consumer owns its threat model.

## Disclosure policy

We follow coordinated disclosure:

- We will work with you to understand the issue and ship a fix.
- Once a fix is released, we publish a security advisory crediting you (unless you prefer to remain anonymous).
- 90 days after the fix is published, the full technical details may be disclosed.

If a vulnerability is being actively exploited, we may shorten this timeline.
