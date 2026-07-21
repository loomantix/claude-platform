---
name: review-accessibility
description: Automated accessibility audit for a running web app — runs axe-core against each route, auto-fixes every violation it finds directly in source, and opens a PR summarizing the changes for human review. Works on any repo since nothing is installed in the target app; axe-core is injected at runtime through the browser tool.
argument-hint: (optional) a URL alone scans every discovered route, public and auth-gated; a URL plus route paths (e.g. "localhost:3000 /dashboard /settings") scans only those; with no arguments you'll be asked for the dev-server URL or the command to start it
---

# /review-accessibility — automated a11y scan + fix + PR

You are running an automated accessibility audit against a running web app (any framework — this does not depend on the target repo having any accessibility tooling installed). The flow is four phases: **scan** (axe-core, headless, every route), **fix** (you edit source for every violation found), **verify** (rescan), and **PR** (branch, commit, push, open a PR — the human's review happens there, not in the browser).

This is always run by a human explicitly invoking `/review-accessibility` — it is never wired into an unattended CI job. There is no per-violation approval step in the browser: everything found gets fixed, and the human's single decision point is reviewing and merging (or rejecting) the resulting PR. Keep that consistent with how `/grill` and `/reviewit` resolve findings by editing code directly — the difference here is the review happens on the PR diff, not inline.

## How it works

The detection engine is [axe-core](https://github.com/dequelabs/axe-core), loaded inside the target page from a version-pinned, SRI-checked CDN URL — no install in the target app. The script at `.claude/skills/review-accessibility/assets/axe-scan.js` (resolve from the consumer repo, or from `~/.claude/skills/` if you installed the skills globally) is injected into each live route via the browser tool's JS-eval capability. It runs a scan and exposes the results on `window.__a11yScan` for you to read back — no DOM overlay, no click-to-triage UI, nothing rendered into the page.

You never modify the target app's source to install this — it's 100% injected at runtime.

**Standard**: the scan runs axe-core's default ruleset — WCAG 2.0/2.1 Level **A and AA** success criteria, plus axe's curated non-WCAG "best-practice" rules (`heading-order`, `empty-heading` and ~25 others: no formal WCAG mapping, real value). Three things are **not** covered, and no PR from this skill may imply otherwise:

- **Level AAA and WCAG 2.2 rules do not run.** axe-core ships them `enabled: false` (`color-contrast-enhanced`, `identical-links-same-purpose`, `meta-refresh-no-exceptions`, `target-size`).
- **`experimental` and `deprecated` rules do not run** — axe excludes them by default.
- **`region` and `landmark-one-main` are switched off** by `assets/axe-scan.js` as page-structure noise. Both are best-practice landmark rules, not WCAG criteria.

Every violation carries a `tags` array (e.g. `wcag2aa`, `wcag143`) identifying the success criterion and level it maps to, or only `best-practice` if it maps to none. Phase 4 classifies each fix from those tags instead of making a blanket "WCAG AA compliant" claim.

## Prerequisites

Check these before Phase 0 and **stop with an actionable message** if any is missing — don't start a scan you can't finish.

1. **A browser tool family is available** — either Claude in Chrome (`mcp__claude-in-chrome__*`, e.g. a session started with `claude --chrome`) or the sandboxed Browser pane (`mcp__Claude_Browser__*`). This skill cannot run without one; tell the user which surfaces you can see and how to enable one.
2. **The target is a development or staging origin.** Refuse to scan a production hostname unless the user explicitly confirms — the scan executes third-party script inside whatever session the browser holds.
3. **`gh` is authenticated** (`gh auth status`) — Phase 4 opens a PR.

## Phase 0: Resolve targets

1. Confirm the target repo is a git repo with a clean working tree (`git status`) — Phase 4 branches off a known-good state. If there are pre-existing uncommitted changes, stop and ask how to proceed rather than mixing your edits into theirs. Do this **first**: it's the cheapest check and the most likely to abort the run, so failing here costs the user nothing.
2. Parse `$ARGUMENTS` for a base URL and, optionally, specific route paths.
   - **Route paths given** (e.g. `/dashboard /settings`) → scan exactly those routes, nothing else. This is the narrowing case: the human named what they want checked.
   - **No route paths given** (just a base URL, or nothing at all) → default to full coverage. Discover every real, navigable route in the target repo yourself: read the routing structure (e.g. `page.tsx`/`page.jsx` under `src/app` for Next.js App Router, files under `pages/` for Pages Router, or the route config for React Router/Vue Router/etc.) and scan all of it — public marketing/login pages and auth-gated app pages alike, not just whichever set happens to be top of mind. Skip routes you can't meaningfully load: dynamic segments with no real param available (e.g. `/invite/[token]`), and dev-only/preview/debug pages (e.g. anything with `robots: noindex` metadata or an obvious non-product purpose). Report the discovered route list to the user before scanning starts — informational, not a blocking approval gate, consistent with the rest of this skill having no per-item gate.
   - If no dev server URL is known at all (neither in `$ARGUMENTS` nor inferable), ask for it (or the command to start it, e.g. `npm run dev`).
3. Resolve the **active surface** — which browser tool family every later phase uses. Default to whichever the Prerequisites check found; when both are available:
   a. Probe with Claude in Chrome: navigate to one of the target routes. If the app itself loads (not a login/sign-in form, no redirect to a `/login`-style URL), the user's real browser already holds a session. Adopt Claude in Chrome and skip to step 4.
   b. If the probe comes back logged out (password field, sign-in form, or redirect to an auth route), fall back to the Browser pane: open it at the login URL and tell the user to log in there themselves — you must never type or enter their credentials, even with permission. Wait for them to confirm, then adopt the Browser pane.
   c. If the app needs no auth, either surface works; prefer the one already available.
   The active surface stays fixed for all of Phase 1–3 — don't switch mid-audit, since session state doesn't carry between the two browser contexts.
4. If a dev server needs starting, use the preview tool (`preview_start`, part of the Browser pane's tool family) to launch it and open the target URL. This only applies when the Browser pane is the active surface — Claude in Chrome just navigates to whatever URL is already serving. Do not use Bash to run dev servers.

## Phase 1: Scan (per route)

Using whichever active surface was resolved in Phase 0, for each route:

Before the first route, clear the scratch directory (`rm -rf /tmp/<repo>/a11y-review` then recreate it). A previous run's files are stale — their CSS selectors describe a DOM that no longer exists, and Phase 2 globs the whole directory, so leftovers from a wider earlier run would be "fixed" again during a narrowed one.

1. Navigate to it — a full page load, not an in-app client-side transition.
2. Read `.claude/skills/review-accessibility/assets/axe-scan.js` and inject its full contents via the browser tool's JS execution (eval the file contents as-is — it's a self-invoking function, safe to inject verbatim).
3. Confirm the scan settled: poll until `window.__a11yScan.ready === true` **or** `window.__a11yScan.failed === true`. Never poll `ready` alone — a CSP block, an offline CDN, an SRI mismatch, or an axe runtime error all end in `failed` with a human-readable `window.__a11yScan.error`, and polling `ready` alone would hang forever. If `failed`, report `error` to the user and stop; don't proceed to Phase 2 on partial data. Give it a few seconds and cap the poll (~30s) before treating it as a failure.
4. Read back `window.__a11yScan.violations` and report to the user how many were found on this route, broken down by impact (critical/serious/moderate/minor) — a status update, not a gate; don't wait for approval to continue.
5. Every violation at every impact level is in scope for fixing — there's no severity cutoff and no ignore list. Persist the full violation list (per node: `ruleId`, `impact`, `help`, `helpUrl`, `tags`, `target` selector, `html` snippet, `failureSummary`, `url`) to `/tmp/<repo>/a11y-review/<route-slug>.json`, where `<repo>` is the target repo's directory name and `<route-slug>` is the route path with `/` replaced by `-` (`/` itself → `root`). Keep scratch **outside** the repo under audit so it can never be committed, never dirties `git status`, and needs no `.gitignore` entry. `tags` is what lets Phase 4 state which WCAG success criterion (if any) each fix maps to — don't drop it when persisting.
6. Also read back `window.__a11yScan.incomplete` — axe's "needs review" bucket, where it puts findings it can't prove either way (classically colour contrast over a background image). Don't auto-fix these; persist them alongside the violations and list them in the Phase 4 PR body as items needing a human look.

## Phase 2: Fix

Do this once, after all requested routes have been scanned — don't fix after every single route; gather everything first so edits to a shared component (e.g. a `Button` used on three pages) collapse into one pass instead of three.

1. Load every `/tmp/<repo>/a11y-review/*.json` file produced in Phase 1. Group entries by which source file they likely belong to, not by route — several entries across routes may resolve to the same component.
2. For each entry you have `ruleId`, `target` (CSS selector), `html` (outer HTML snippet of the offending element), and `failureSummary` (axe's specific instruction, e.g. "Fix any of the following: Element has insufficient color contrast of 2.1 (foreground color: #999999, background color: #ffffff, font size: 12.0pt, font weight: normal): expected contrast ratio of 4.5:1"). Use the `html` snippet's distinguishing text/class names/attributes to locate the matching source (grep the repo for a unique fragment — class names, visible text content, `data-testid`, element structure). Do not guess blindly; if you can't confidently match an entry to source within a couple of searches, leave it unfixed and flag it clearly in the Phase 4 PR description rather than editing the wrong component.
3. Apply the minimal fix `failureSummary` calls for — add the missing `aria-label`/`aria-labelledby`, wire `aria-expanded`/`aria-activedescendant`/`role` on custom interactive widgets, associate `<label>`/`htmlFor` with inputs, add `alt` text, fix heading order, or adjust the color token/class to satisfy the stated contrast ratio. Batch multiple fixes to the same file into one edit pass rather than one `Edit` call per violation.
4. **Prefer the narrowest scope that fixes the violation.** A shared design token or a base component class reaches everything that uses it, so a contrast tweak there is a site-wide visual change dressed up as an a11y fix. Override at the component or usage site unless the token itself is the defect. Same for heading order — change the tag and keep the visual class rather than letting the page restyle. Any change to a shared token, theme value, or base component gets called out explicitly in the Phase 4 PR body under its own heading, so a reviewer sees the blast radius without reading the diff.

## Phase 3: Verify

Re-navigate and re-inject the scan script on **every route scanned in Phase 1**, not just the routes whose source you edited. Phase 2 deliberately collapses fixes into shared components, so an edit made for one route can regress another that had no findings at all.

If a fix didn't take (rescan still shows it) or introduced a new violation, iterate on that file. Cap this at **three** rescans per route: some violations resist an automated fix (contrast over a gradient or image is the usual one), and since this skill has no ignore list, an unbounded loop has nothing to stop it. When a violation survives the cap, leave it, move on, and list it in the Phase 4 PR body as unresolved with what you tried.

## Phase 4: Open PR

1. Create a new branch off the current HEAD (don't build on top of unrelated in-progress branches — check what branch you're on and confirm with the user if it looks like someone else's unfinished work rather than a clean base).
2. Stage and commit only the fixed source files. Scratch lives in `/tmp` (Phase 1 step 5), so there is nothing to exclude and no `.gitignore` to touch — if `git status` shows anything you didn't deliberately edit, stop and look rather than staging it. Commit message summarizes what was fixed (rule types + route count), not a route-by-route diary.
3. Push the branch and open a PR (`gh pr create`) with a body that lists, per fix: the rule, impact, a brief description, and its **WCAG classification** derived from the persisted `tags`.
   - Read the criterion from the parallel `EN-9.x.y.z` tag when one is present — it spells the number out already (`EN-9.1.4.12` → SC 1.4.12), which avoids parsing entirely.
   - Otherwise decode a `wcagNNN` tag as **first digit = principle, second digit = guideline, everything remaining = the success criterion**: `wcag143` → 1.4.3, `wcag1412` → 1.4.**12** (not 1.4.1.2). Match these with `^wcag\d{3,}$` so the version/level tags — `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa` — aren't misread as criteria. Those are what give you the level, e.g. "WCAG 1.4.3 Contrast (Minimum), Level AA".
   - If the only tags are `best-practice`/`cat.*` with no `wcagNNN`, label it "best practice (no formal WCAG mapping)" rather than implying a compliance requirement.
     Also call out — critically — anything from Phase 1 you couldn't confidently match to source, every violation that survived the Phase 3 rescan cap, and everything in the `incomplete` bucket, so the human knows what to check by hand instead of assuming full coverage.
4. Report the PR URL back to the user. Merging is their call, not something this skill does.

## Notes

- This skill has no dependency on the target repo's framework — it works against a plain HTML page just as well as a Next.js/React/Vue app, since the scan runs purely against the live DOM.
- The axe-core CDN URL is pinned to an exact version and carries an SRI hash. If the script fails to load, `window.__a11yScan.error` says why: a strict CSP on the target page, no network, or an integrity mismatch. Report it and stop — never fall back to an unpinned URL or strip the integrity attribute to make a scan succeed. Bumping the version means re-deriving the hash; `assets/axe-scan.js` documents the command.
- Auth-gated apps: see Phase 0 step 3 for the hybrid flow (Claude in Chrome's existing session first, Browser-pane manual login as fallback). Login itself is always done by the human, never by this skill — entering credentials on someone's behalf is out of bounds regardless of permission granted.
- No ignore mechanism: every run is a fresh, full pass over every violation the configured ruleset reports, at every severity. False positives get caught at PR review, not suppressed ahead of time — if a fix is wrong, reject it in the PR like any other code change.
- Automated coverage is partial, not a compliance certification. Deque reports that axe-core finds, on average, [57% of accessibility _issues_](https://github.com/dequelabs/axe-core) automatically — a share of issues, not of success criteria, and the two are often conflated. Measured per success criterion the coverage is far thinner: keyboard-only navigation, screen-reader announcement quality, and cognitive/plain-language criteria need manual testing this skill doesn't do, and Level AAA isn't scanned at all. A clean run means "no axe-core-detectable violations on the scanned routes under the ruleset above," not "WCAG 2.1 AA compliant." Don't let a PR from this skill imply the latter — say the former.
- Git safety: this skill always works on a fresh branch and opens a PR — it never commits to the branch you started on if that branch has unrelated pending work, never force-pushes, and never merges. Ask before branching if the working tree isn't clean or the current branch looks mid-flight.

## Source of truth

This skill lives upstream at `.claude/skills/review-accessibility/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten — make changes upstream.
