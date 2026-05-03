---
name: issues
description: GitHub issue workflow — ready queue with dependency resolution, claim, start, close, link
argument-hint: 'verb [args]: ready | show <n> | claim <n> | start <n> | close <n> [msg] | link <n> blocks|blocked-by <m> | search <q>'
---

# /issues

Thin workflow over `gh issue` with a smart **ready** query that parses `Blocked by #N` / `Depends on #N` from issue bodies to compute dependency-free work. (`Blocks #N` is written as the reciprocal side by `/issues link`, but isn't parsed as a blocker itself — the authoritative direction is `Blocked by`.)

**Arguments**: `$ARGUMENTS`

Dispatch on the first word of `$ARGUMENTS`. If no verb is given, default to `ready`.

---

## Permission hygiene

**Never use heredocs in `gh` commands** — the auto-approval regex can't match multiline commands, which causes permission prompts. Always write multiline content to a temp file first:

```bash
cat > /tmp/issue-body.md << 'BODY'
line 1
line 2
BODY
gh issue edit <n> --body-file /tmp/issue-body.md
```

Apply to: `gh issue create`, `gh issue edit`, `gh issue comment`.

---

## ready

Show open issues with no open blockers, sorted by priority.

```bash
./.claude/skills/issues/scripts/ready.py
```

Flags (all optional):

- `--mine` — only issues assigned to me
- `--unassigned` — only unassigned issues
- `--agent` — only issues labeled `dev: agent`
- `--priority critical|high|medium|low`
- `--area <name>` (matches `area: <name>` label — e.g., `backend`, `frontend`, `mobile`, `packages`)
- `--limit N` (default 20)
- `--json` — machine-readable

Exclusion rules:

- Label `status: blocked`
- Body contains `Blocked by #N` or `Depends on #N` where #N is still open

The `--agent` / `--priority` / `--area` flags work via standard label conventions (`dev: agent`, `priority: <level>`, `area: <name>`). Repos that don't use those labels will simply get an empty result for those filters — the script doesn't enforce a label scheme, it just queries one when asked.

---

## show \<n\>

```bash
gh issue view <n>
```

Surface dependency refs explicitly (useful for triage):

```bash
gh issue view <n> --json body --jq '.body' | grep -iE '^[[:space:]]*[-*]?[[:space:]]*(blocked by|blocks|depends on)[:\s]+#[0-9]+' || echo "(no dependency refs)"
```

---

## claim \<n\>

```bash
gh issue edit <n> --add-assignee @me
printf 'Claiming this.\n' > /tmp/issue-comment.md
gh issue comment <n> --body-file /tmp/issue-comment.md
```

---

## start \<n\>

Claim + create working branch off the repo's default branch. The default branch is auto-detected (`main`, `staging`, etc.) via the upstream HEAD ref so this works across consumer repos without per-repo config:

```bash
gh issue edit <n> --add-assignee @me
slug=$(gh issue view <n> --json title --jq '.title' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-//' | cut -c1-50 | sed 's/-$//')
default_branch=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
default_branch="${default_branch:-main}"
git fetch origin "$default_branch"
git checkout -b "fix/issue-<n>-$slug" "origin/$default_branch"
```

Replace `<n>` and the slug interpolation with the real issue number. If `git symbolic-ref` is unset (rare; happens when the remote was added without `--mirror` or `git remote set-head` was never run), the fallback is `main`. Run `git remote set-head origin --auto` once on the affected clone to fix it permanently.

---

## close \<n\> \[msg\]

With a comment:

```bash
printf '<msg>\n' > /tmp/issue-close.md
gh issue close <n> --comment "$(cat /tmp/issue-close.md)"
```

Without:

```bash
gh issue close <n>
```

**Confirm with the user before closing** if the issue isn't assigned to them or the close reason isn't obvious from the conversation.

---

## link \<n\> blocks|blocked-by \<m\>

Adds dependency refs to **both** issues so `ready` sees them regardless of which side you query.

```bash
./.claude/skills/issues/scripts/link.py <n> blocks <m>
./.claude/skills/issues/scripts/link.py <n> blocked-by <m>
```

- `link A blocks B` → writes `Blocks #B` to A, `Blocked by #A` to B
- `link A blocked-by B` → writes `Blocked by #B` to A, `Blocks #A` to B

Refs land under a `## Dependencies` section in each body. If a matching ref already exists, the script no-ops that side.

---

## search \<query\>

Forward to `gh issue list --search`:

```bash
gh issue list --search "<query>" --limit 20
```

Useful query fragments: `label:"dev: agent"`, `is:open no:assignee`, `in:title pipeline`, `author:@me`.

---

## Dependency parsing rules

`ready` recognizes these patterns (case-insensitive, on their own line, optionally bulleted):

- `Blocked by #N`
- `Depends on #N`

To mark a dependency, prefer `/issues link` over manual edits — it keeps both issues consistent and under a parseable `## Dependencies` section. The label `status: blocked` is also honored (excludes from `ready` regardless of body content).

---

## Hard rules

- Never use heredocs in `gh` commands — always temp files.
- Never close someone else's issue without explicit user confirmation.
- Keep dependency refs under the `## Dependencies` section so they stay parseable and don't conflict with prose.
- If `ready` returns nothing, don't invent work — report the empty queue and stop.

---

## Source of truth

This skill lives upstream at `.claude/skills/issues/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten on next sync — make changes upstream.
