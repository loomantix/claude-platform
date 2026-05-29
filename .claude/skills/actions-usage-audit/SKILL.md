---
name: actions-usage-audit
description: Audit an org's GitHub Actions minute consumption — build a month-over-month minutes-by-repo matrix from the billing usage API, attribute a spike to repos and workflows, and surface concrete cut opportunities. Use when an Actions/billing alert fires or you need to understand what is burning CI minutes. Read-only.
argument-hint: optional org login (defaults to the current repo's owner) and optional year, e.g. `acme` or `acme 2026`
---

# actions-usage-audit

Read-only cost analysis of GitHub Actions minute usage for an organization. Produces (1) a month×repo minutes matrix, (2) spike attribution, (3) a per-workflow billable-minutes drilldown for the top repos, and (4) a ranked cut list. Pure `gh api` reads — changes nothing.

## Prereqs

- `gh` authenticated as an **org owner or billing manager**. The billing usage API requires it; a plain-member token returns 403.
- `jq` and `awk` available.

## Org context file (read first if present)

Org-specific knowledge — expected baselines, which repos are public, known heavy CI suites, prior incidents — lives outside this skill so the skill stays generic and portable. Before drawing conclusions, check for a context file at `.claude/actions-usage-audit.local.md` in the current repo (or one the user names) and read it: it tells you what "normal" looks like and which repos to prioritize. If absent, proceed from the data alone and note that baselines are inferred.

## Step 1 — Resolve scope

Set `ORG` to the first argument, else the current repo's owner (`gh repo view --json owner --jq .owner.login`). Set `YEAR` to the second argument, else the current year. Note an org may have a sibling org for internal packages — audit each separately.

## Step 2 — Pull monthly usage (the reliable path)

The enhanced billing usage API is the source of truth. **Gotcha: the no-argument `/organizations/$ORG/settings/billing/usage` call returns a misleading partial aggregate — do not trust it.** Always pass explicit `year` + `month`; that form returns per-day line items for every repo:

```bash
WORK=$(mktemp -d)
for m in $(seq 1 12); do
  gh api "/organizations/$ORG/settings/billing/usage?year=$YEAR&month=$m" \
    --jq ".usageItems[] | select(.product==\"actions\") | [\"$m\", .sku, .repositoryName, .quantity, .netAmount] | @tsv" 2>/dev/null
done > "$WORK/actions.tsv"
```

Reading the data:

- `quantity` for the `Actions Linux` / `Actions Windows` / `Actions macOS` SKUs is **raw minutes**. `Actions storage` is GigabyteHours, not minutes — exclude it from the compute total (track separately only if storage cost is in scope).
- Cost multipliers: Linux ×1, Windows ×2, macOS ×10. Raw minutes rank consumption; apply the multiplier for dollar impact.
- **Public repos never appear** — they bill unlimited free minutes. A repo missing from the data is public, not idle. Confirm the public set with `gh repo list $ORG --visibility public`.
- `netAmount` is post-allowance dollars and resets each billing cycle, so it reads near-zero early in the month even for heavy repos. Rank by `quantity`, never by `netAmount`.

## Step 3 — Month×repo matrix and spike attribution

```bash
# compute minutes by month × repo
awk -F'\t' '$2!="Actions storage"{m[$1"\t"$3]+=$4} END{for(k in m) printf "%s\t%d\n",k,m[k]+0.5}' "$WORK/actions.tsv" | sort -k1,1n -k2,2
# monthly totals
awk -F'\t' '$2!="Actions storage"{t[$1]+=$4} END{for(i=1;i<=12;i++) if(t[i]) printf "M%d %d min\n",i,t[i]+0.5}' "$WORK/actions.tsv"
```

- **Prorate the current (incomplete) month** before comparing it to full months: `actual / day_of_month * days_in_month`. Skipping this either understates the trend or triggers a false panic.
- Compute each repo's share of the total and its month-over-month delta. Separate the **structural baseline** (one or two repos that dominate every month) from **proliferation** (many small repos newly appearing) — they need different remedies.

## Step 4 — Per-workflow drilldown (top repos only)

Run counts are not minutes — a job firing 1000×/month may be 10s each. For each high-consumption repo, attribute minutes to workflows:

```bash
SINCE=$(date -u -d '30 days ago' +%Y-%m-%d)
gh api /repos/$ORG/$REPO/actions/workflows --jq '.workflows[] | "\(.id)\t\(.name)"' |
while IFS=$'\t' read -r id name; do
  c=$(gh api "/repos/$ORG/$REPO/actions/workflows/$id/runs?created=>$SINCE&per_page=1" --jq '.total_count')
  printf "%6s  %s\n" "$c" "$name"
done | sort -rn
```

Then sample billable time for the top workflows. Try `gh api /repos/$ORG/$REPO/actions/runs/<run_id>/timing` first — `billable.UBUNTU.total_ms` (plus `WINDOWS` / `MACOS`) is authoritative when populated. **Caveat: on private repos covered by an included-minutes plan, GitHub zeroes those `billable` fields.** When they read 0, fall back to how GitHub actually bills — per _job_ wall-clock rounded up to the whole minute, summed across all jobs in the run:

```bash
gh api /repos/$ORG/$REPO/actions/runs/<run_id>/jobs \
  --jq '[.jobs[] | select(.started_at and .completed_at)
         | (((.completed_at|fromdateiso8601) - (.started_at|fromdateiso8601))/60 | ceil)] | add'
```

Take a median over ~8–12 recent completed runs and multiply by the run count for an estimate. High-variance workflows (matrix builds, cache-dependent jobs) carry real sampling uncertainty — state it as ±. Read each sampled run's `event` field to record the trigger (`push` / `pull_request` / `schedule` / `workflow_dispatch`) — the trigger drives the remedy.

## Step 5 — Cut heuristics

Flag, in rough order of payoff:

- **Heavy CI on a hot branch.** The dominant repo's test/validation workflow on every push is usually the single biggest line. Levers: `concurrency` with `cancel-in-progress`, skip-on-draft, path filters, test sharding + caching, self-hosted runners.
- **Double-runs.** A workflow triggering on both `push` and `pull_request` runs twice per PR commit. Scope the trigger to one.
- **Over-frequent crons.** A `schedule` workflow with a high run count fires regardless of activity (watchdogs, scanners, sync). Lengthen the interval or gate it on real change.
- **Per-push security scanners.** Many separate scanner workflows, each per-push, multiply fast. Consolidate into one job or move non-blocking scans to a daily schedule.
- **Long-running agent / review workflows.** AI-review or cloud-agent workflows can hold a runner for many minutes; judge them by billable median, not count.
- **Fleet proliferation.** When many repos each inherit the same full CI suite, the long tail becomes material. Trim the default workflow set for low-activity repos.

## Step 6 — Report

Present: the monthly-total trend (current month prorated), the month×repo matrix with deltas highlighted, the per-workflow drilldown for the top repo(s), and a ranked, specific cut list (workflow + lever + rough minutes recoverable). Recommend nothing destructive — this skill only reads; the human decides what to cut.
