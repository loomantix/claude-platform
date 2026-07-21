---
name: review-accessibility
description: Automated accessibility audit for a running web app — runs axe-core against each route, auto-fixes every violation it finds directly in source, and opens a PR summarizing the changes for human review. Works on any repo since nothing is installed in the target app; axe-core is injected at runtime through the browser tool.
argument-hint: a URL alone (scans every discovered route, public and auth-gated) or a URL plus specific route paths (e.g. "localhost:3000 /dashboard /settings" scans only those)
---

# /review-accessibility — automated a11y scan + fix + PR

You are running an automated accessibility audit against a running web app (any framework — this does not depend on the target repo having any accessibility tooling installed). The flow is four phases: **scan** (axe-core, headless, every route), **fix** (you edit source for every violation found), **verify** (rescan), and **PR** (branch, commit, push, open a PR — the human's review happens there, not in the browser).

This is always run by a human explicitly invoking `/review-accessibility` — it is never wired into an unattended CI job. There is no per-violation approval step in the browser: everything found gets fixed, and the human's single decision point is reviewing and merging (or rejecting) the resulting PR. Keep that consistent with how `/grill` and `/reviewit` resolve findings by editing code directly — the difference here is the review happens on the PR diff, not inline.

## How it works

The detection engine is [axe-core](https://github.com/dequelabs/axe-core) (loaded from CDN inside the target page — no install needed). A vendored script at `assets/axe-scan.js` in this skill directory is injected into each live route via the browser tool's JS-eval capability. It loads axe-core, runs a scan, and exposes the full violations array on `window.__a11yScan` for you to read back — no DOM overlay, no click-to-triage UI, nothing rendered into the page.

You never modify the target app's source to install this — it's 100% injected at runtime.

**Standard**: the scan runs axe-core's full default ruleset, not a single filtered standard — that's WCAG 2.0/2.1 Level A, AA, and AAA success criteria together with axe-core's curated non-WCAG "best-practice" rules (things like `heading-order`, `empty-heading` — real value, no formal WCAG mapping). Every violation axe reports carries a `tags` array (e.g. `wcag2aa`, `wcag143`) that identifies exactly which WCAG success criterion and level it maps to, or shows only `best-practice` if it doesn't map to one. Phase 4 uses those tags to classify each fix in the PR rather than making a blanket "WCAG AA compliant" claim — see Phase 4 step 3 and the Notes below for why that distinction matters.

## Phase 0: Resolve targets

1. Parse `$ARGUMENTS` for a base URL and, optionally, specific route paths.
   - **Route paths given** (e.g. `/dashboard /settings`) → scan exactly those routes, nothing else. This is the narrowing case: the human named what they want checked.
   - **No route paths given** (just a base URL, or nothing at all) → default to full coverage. Discover every real, navigable route in the target repo yourself: read the routing structure (e.g. `page.tsx`/`page.jsx` under `src/app` for Next.js App Router, files under `pages/` for Pages Router, or the route config for React Router/Vue Router/etc.) and scan all of it — public marketing/login pages and auth-gated app pages alike, not just whichever set happens to be top of mind. Skip routes you can't meaningfully load: dynamic segments with no real param available (e.g. `/invite/[token]`), and dev-only/preview/debug pages (e.g. anything with `robots: noindex` metadata or an obvious non-product purpose). Report the discovered route list to the user before scanning starts — informational, not a blocking approval gate, consistent with the rest of this skill having no per-item gate.
   - If no dev server URL is known at all (neither in `$ARGUMENTS` nor inferable), ask for it (or the command to start it, e.g. `npm run dev`).
2. **If the target routes require authentication to view**, resolve which browser surface to use before touching anything else — this decides which tool family (`mcp__claude-in-chrome__*` or `mcp__Claude_Browser__*`) every later phase uses:
   a. Probe first with Claude in Chrome: navigate to one of the target routes there. If the app itself loads (not a login/sign-in form, no redirect to a `/login`-style URL), the user's real browser already has an authenticated session. Adopt Claude in Chrome as the **active surface** for the rest of the run and skip to step 3 — no login step needed.
   b. If that probe comes back logged out (password field, sign-in form, or redirect to an auth route), fall back to the Browser pane: open it at the login URL and tell the user to log in there themselves — you must never type or enter their credentials, even with permission. Wait for them to confirm they're logged in. Adopt the Browser pane as the **active surface** for the rest of the run.
   c. If the app doesn't require auth, skip this check and use the Browser pane as the active surface by default.
   Note the chosen active surface stays fixed for the whole run (all of Phase 1–3) — don't switch mid-audit, since session state doesn't carry between the two browser contexts.
3. If a dev server needs starting, use the preview tool (`preview_start`, part of the Browser pane's tool family) to launch it and open the target URL. This only applies when the Browser pane is the active surface — Claude in Chrome just navigates to whatever URL is already serving. Do not use Bash to run dev servers.
4. Confirm the target repo is a git repo with a clean working tree before you start editing anything in Phase 2 (`git status`) — Phase 4 needs to branch off of a known-good state. If there are pre-existing uncommitted changes, stop and ask the user how to proceed rather than mixing your edits into theirs.

## Phase 1: Scan (per route)

Using whichever active surface was resolved in Phase 0, for each route:

1. Navigate to it.
2. Read `assets/axe-scan.js` from this skill directory and inject its full contents via the browser tool's JS execution (eval the file contents as-is — it's a self-invoking function, safe to inject verbatim).
3. Confirm the scan finished: poll `window.__a11yScan.ready === true` (it starts `false` until axe-core loads from the CDN and finishes running, so this may take a couple seconds).
4. Read back `window.__a11yScan.violations` and report to the user how many were found on this route, broken down by impact (critical/serious/moderate/minor) — a status update, not a gate; don't wait for approval to continue.
5. Every violation at every impact level is in scope for fixing — there's no severity cutoff and no ignore list. Persist the full violation list (per node: `ruleId`, `impact`, `help`, `helpUrl`, `tags`, `target` selector, `html` snippet, `failureSummary`, `url`) to `.claude/a11y-review/<route-slug>.json` in the **target repo's working directory** (create the folder if needed) — this is scratch input for Phase 2, local to your working tree only. `tags` is what lets Phase 4 state which WCAG success criterion (if any) each fix maps to — don't drop it when persisting. These files are **never committed** — see Phase 4 step 2.

## Phase 2: Fix

Do this once, after all requested routes have been scanned — don't fix after every single route; gather everything first so edits to a shared component (e.g. a `Button` used on three pages) collapse into one pass instead of three.

1. Load every `.claude/a11y-review/*.json` file produced in Phase 1. Group entries by which source file they likely belong to, not by route — several entries across routes may resolve to the same component.
2. For each entry you have `ruleId`, `target` (CSS selector), `html` (outer HTML snippet of the offending element), and `failureSummary` (axe's specific instruction, e.g. "Fix any of the following: Element has insufficient color contrast of 2.1 (foreground color: #999999, background color: #ffffff, font size: 12.0pt, font weight: normal): expected contrast ratio of 4.5:1"). Use the `html` snippet's distinguishing text/class names/attributes to locate the matching source (grep the repo for a unique fragment — class names, visible text content, `data-testid`, element structure). Do not guess blindly; if you can't confidently match an entry to source within a couple of searches, leave it unfixed and flag it clearly in the Phase 4 PR description rather than editing the wrong component.
3. Apply the minimal fix `failureSummary` calls for — add the missing `aria-label`/`aria-labelledby`, wire `aria-expanded`/`aria-activedescendant`/`role` on custom interactive widgets, associate `<label>`/`htmlFor` with inputs, add `alt` text, fix heading order, or adjust the color token/class to satisfy the stated contrast ratio. Batch multiple fixes to the same file into one edit pass rather than one `Edit` call per violation.

## Phase 3: Verify

For each route you touched, re-navigate and re-inject `assets/axe-scan.js` to confirm the violations you fixed are gone and nothing new broke. If a fix didn't take (rescans still show it) or introduced a new violation, iterate on that file before moving to Phase 4 — the PR should reflect a clean rescan, not a hopeful diff.

## Phase 4: Open PR

1. Create a new branch off the current HEAD (don't build on top of unrelated in-progress branches — check what branch you're on and confirm with the user if it looks like someone else's unfinished work rather than a clean base).
2. Stage and commit only the fixed source files — never the `.claude/a11y-review/*.json` scratch files from Phase 1. Those are point-in-time scan artifacts (CSS selectors like `:nth-child(1)` tied to the exact DOM state at scan time) with no consumer after this run; the PR body (step 3) is the real audit trail, and it's a better one — permanently archived by GitHub against the exact commit, not a growing pile of JSON blobs that goes stale the next time the page changes. If the target repo doesn't already ignore `.claude/a11y-review/`, add that path to its `.gitignore` in this same commit so it stays out of `git status` on future runs too. Commit message summarizes what was fixed (rule types + route count), not a route-by-route diary.
3. Push the branch and open a PR (`gh pr create`) with a body that lists, per fix: the rule, impact, a brief description, and its **WCAG classification** derived from the persisted `tags` — state the specific success criterion and level (e.g. "WCAG 1.4.3 Contrast (Minimum), Level AA" — the number in a `wcagNNN`-style tag is the SC number, `wcag2a`/`wcag2aa`/`wcag21a`/`wcag21aa` gives the version and level) or, if the only tags present are `best-practice`/`cat.*` with no `wcagNNN` tag, label it "best practice (no formal WCAG mapping)" instead of implying it's a compliance requirement. Also call out — critically — anything from Phase 1 you couldn't confidently match to source, so the human knows to check it manually rather than assuming full coverage.
4. Report the PR URL back to the user. Merging is their call, not something this skill does.

## Notes

- This skill has no dependency on the target repo's framework — it works against a plain HTML page just as well as a Next.js/React/Vue app, since the scan runs purely against the live DOM.
- If the target page has a strict CSP that blocks the `cdn.jsdelivr.net` script load for axe-core, tell the user rather than silently failing — they'll need to allowlist it for the dev environment or you'll need to vendor axe-core locally (not done by default to keep this skill's footprint small).
- Auth-gated apps: see Phase 0 step 2 for the hybrid flow (Claude in Chrome's existing session first, Browser-pane manual login as fallback). Login itself is always done by the human, never by this skill — entering credentials on someone's behalf is out of bounds regardless of permission granted.
- No ignore mechanism: every run is a fresh, full pass over every violation at every severity. False positives or unwanted fixes get caught at PR review, not suppressed ahead of time — if a fix is wrong, reject it in the PR like any other code change.
- Automated coverage is partial, not a compliance certification: axe-core's own docs estimate automated testing catches roughly 30-50% of WCAG success criteria — things like keyboard-only navigation, screen-reader announcement quality, and cognitive/plain-language criteria need manual testing this skill doesn't do. A clean run means "no axe-core-detectable WCAG violations on the scanned routes," not "WCAG 2.1 AA compliant." Don't let a PR from this skill imply the latter — say the former.
- Git safety: this skill always works on a fresh branch and opens a PR — it never commits to the branch you started on if that branch has unrelated pending work, never force-pushes, and never merges. Ask before branching if the working tree isn't clean or the current branch looks mid-flight.
- `.claude/a11y-review/` is scratch, not an artifact: it exists only to carry violation data from Phase 1 into Phase 2 within a single run. Never commit it (Phase 4 step 2) — the PR description is the durable audit trail.
