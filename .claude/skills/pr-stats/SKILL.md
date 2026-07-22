---
name: pr-stats
description: Pull PR-authorship counts per repo and per author across a GitHub org over a given time window. Use when the user asks "how many PRs did X open", "who's been shipping", "PR throughput by repo", or any similar curiosity about creation-volume.
argument-hint: [--months N | --start YYYY-MM --end YYYY-MM] [--owner <org>] [--merged]
---

# /pr-stats — org-wide PR author breakdown

You are aggregating PR authorship across a GitHub org for a time window the user specifies (or 12 months back by default). The output is three tables: monthly trend per human author, top repos per human, and bot totals separated out.

## Arguments

- `--months N` — look back N calendar months from today (default `12`).
- `--start YYYY-MM --end YYYY-MM` — explicit window (mutually exclusive with `--months`). Inclusive on both ends.
- `--owner <org>` — GitHub owner. Defaults to the owner of the current repo's `origin` remote (`gh repo view --json owner --jq .owner.login`). If that fails (no remote, not in a repo), ask rather than guessing.
- `--merged` — count merged-in-window instead of created-in-window. Default is `created`.

If the user just says "how many PRs did each person do this year" / "last 6 months" / "in May", translate to the right flags rather than asking — clarify only when the window is genuinely ambiguous.

## Required environment

- `gh` CLI authenticated against the target org (`gh auth status` should show the org under "Token scopes"). No special env vars beyond that.
- **`gawk`.** The aggregation uses true arrays-of-arrays (`m[$1][$3]`) and `asorti()`, both GNU extensions. `mawk` (the default `awk` on Debian/Ubuntu) and BSD `awk` on macOS do not support them and fail with a syntax error. Check with `command -v gawk` — the snippets below invoke `gawk` explicitly, not the system `awk`, so testing `awk --version` tests the wrong binary. If `gawk` is absent, install it or tell the user the aggregation step needs it; do not hand-translate the matrix into a one-dimensional version, which is where a real misreport would come from.

## Optional org context

Org-specific knowledge — which logins are bots, which repos are archived, who left mid-window — lives outside this skill so the skill stays generic and portable. Before reporting, check for a context file at `.claude/pr-stats.local.md` in the current repo (or one the user names) and read it. If absent, proceed from the data alone and rely on the generic bot heuristic below.

## Method

The whole skill is `gh search prs` + `awk`. Two operational hazards make it non-trivial: **the 1000-result per-query cap** and **the 30-req/min search rate limit**.

### 1. Build the month list

One query per calendar month is the right granularity — small enough that no single month hits the 1000 cap on most orgs, large enough that 12 months = 12 queries (well under the per-hour budget after rate-limit cooldowns).

```bash
# For --months N: enumerate the last N month boundaries inclusive of the current month.
# For --start/--end: enumerate from start month to end month inclusive.
# Each month's range: YYYY-MM-01..YYYY-MM-<last-day>.
```

Use `date -d "$start +1 month" +%Y-%m-01` then `date -d "$next -1 day" +%Y-%m-%d` to get clean month-end dates without hardcoding leap years. (These are GNU `date` flags; on macOS use `gdate` from coreutils.)

### 2. Pull each month, watch for the 1000 cap

Write the working TSV under a repo-scoped temp path so concurrent runs in different repos cannot collide:

```bash
tmp="${TMPDIR:-/tmp}/pr-stats/$(gh repo view --json name --jq .name 2>/dev/null || echo default)"
mkdir -p "$tmp"
out="$tmp/counts-$$.tsv"
: > "$out"
trap 'rm -f "$out" "$out.tmp"' EXIT

# Append one window's rows and echo how many were added. Counting the delta
# locally is what lets the cap check below cost zero extra API calls.
pull_window() {  # $1=range  $2=label
  local before after
  wait_for_search_quota
  before=$(wc -l < "$out")
  gh search prs --owner "$OWNER" \
    --"$WINDOW_FIELD" "$1" \
    --limit 1000 --json author,repository \
    --jq ".[] | \"$2\t\(.repository.name)\t\(.author.login)\"" >> "$out"
  after=$(wc -l < "$out")
  echo $(( after - before ))
}

for m in "${months[@]}"; do
  start="${m% *}"; end="${m#* }"; label="${start:0:7}"
  n=$(pull_window "${start}..${end}" "$label")
  echo "$label  $n PRs"
  if [ "$n" -eq 1000 ]; then
    echo "  ⚠ hit 1000-result cap — splitting $label into halves" >&2
    # Drop the truncated rows. Use awk, NOT `grep -v "^$label"`: when the capped
    # month is the only month in the file so far (the first or only requested
    # month), grep selects nothing and exits 1, so an `&& mv` never fires and the
    # 1000 truncated rows stay — then both halves are appended on top of them,
    # overcounting by exactly 1000. awk's exit status doesn't depend on matching,
    # so the replacement always installs.
    awk -F'\t' -v l="$label" '$1 != l' "$out" > "$out.tmp"
    mv "$out.tmp" "$out"
    mid=$(date -d "$start +14 days" +%Y-%m-%d)
    mid_next=$(date -d "$mid +1 day" +%Y-%m-%d)
    for range in "${start}..${mid}" "${mid_next}..${end}"; do
      half_n=$(pull_window "$range" "$label")
      # Neither half may itself be capped. Checked from the rows just appended —
      # no confirmation re-query, which halves the API cost of a capped month.
      if [ "$half_n" -eq 1000 ]; then
        echo "::error::Half-window $range also hit the 1000 cap; this skill's split-once strategy is insufficient. Switch to per-week windows." >&2
        exit 1
      fi
    done
  fi
done
```

`$WINDOW_FIELD` is `created` by default, `merged` if `--merged` was passed.

**Never silently report a truncated count.** If a window hits 1000 and the split doesn't resolve it, fail with a clear error rather than under-reporting throughput — the user is going to draw conclusions from these numbers.

The `trap` deletes the working TSV on exit; it is intermediate data, not a deliverable, and the hard-failure path above exits without reaching any explicit cleanup.

### 3. Wait-for-rate-limit pattern

**One `gh search prs` invocation is not one API request.** `--limit 1000` paginates at 100 results per request, so a single full-window pull can consume **up to 10** of the 30-per-minute search budget. A guard that reserves headroom for one request will happily start a pull that dies part-way through pagination, and a partial pull is exactly the silent undercount this skill exists to prevent.

Reserve headroom for a whole paginated pull, and when short, wait for the documented reset rather than polling blindly:

```bash
# `gh api rate_limit` hits /rate_limit, which is free — it does not consume
# search budget, so polling it costs nothing.
wait_for_search_quota() {
  local need="${1:-12}" remaining reset now delay
  while :; do
    remaining=$(gh api rate_limit --jq '.resources.search.remaining')
    [ "$remaining" -ge "$need" ] && return 0
    reset=$(gh api rate_limit --jq '.resources.search.reset')
    now=$(date +%s)
    delay=$(( reset - now + 1 ))
    [ "$delay" -lt 1 ] && delay=5
    echo "  search quota ${remaining}/${need} — waiting ${delay}s for reset" >&2
    sleep "$delay"
  done
}
```

`12` is 10 pagination requests plus a small margin. If a pull still fails mid-pagination, treat it as a hard error and re-run the window — do not accept the partial result.

`sleep` is blocked in the foreground in some harness modes. If that applies, drive the wait from the harness's own polling primitive instead of a bare `sleep`; the logic is unchanged.

### 4. Aggregate

Three views, all from the same TSV (`month \t repo \t author`). The bot filter is spelled out inline in each; keep the three copies identical, or View A and View C stop being complements.

**View A — Monthly matrix per human:**

```bash
gawk -F'\t' '$3 !~ /\[bot\]$/ && $3 != "Copilot" {m[$1][$3]++; a[$3]=1} END {
  na=asorti(a, authors)
  nm=asorti(m, months)
  printf "%-9s","month"; for (j=1;j<=na;j++) printf "%-15s", authors[j]; print ""
  for (i=1;i<=nm;i++) {
    printf "%-9s", months[i]
    for (j=1;j<=na;j++) printf "%-15s", m[months[i]][authors[j]]+0
    print ""
  }
}' "$out"
```

Sort the author names into an indexed array **once** with `asorti()` and drive both the header and the data rows from it. Do not iterate `for (u in a)` in both places and assume the two traversals agree: awk specifies no traversal order for an associative array, so alignment would rest on an implementation detail rather than a guarantee. Sorting also makes the report deterministic between runs, which matters when someone diffs two months' output.

**View B — Per-repo per-human totals:**

```bash
gawk -F'\t' '$3 !~ /\[bot\]$/ && $3 != "Copilot" {print $2"\t"$3}' "$out" \
  | sort | uniq -c | sort -rn \
  | awk '{printf "%-25s %-15s %s\n", $2, $3, $1}'
```

**View C — Bot totals (separated out):**

```bash
gawk -F'\t' '$3 ~ /\[bot\]$/ || $3 == "Copilot" {print $3}' "$out" \
  | sort | uniq -c | sort -rn
```

### 5. Report

Render the three views as markdown tables back to the user. After the tables, surface anything actually interesting in the trend — month-over-month inflections, authors dropping off, sudden ramps. Don't editorialize for the sake of it; if the data is flat, say "no notable trends" and stop.

If any month hit the cap and was split, mention it briefly so the user knows the numbers are still complete.

## What counts as a "bot"

Anything whose login ends in `[bot]` (e.g. `dependabot[bot]`, `github-actions[bot]`, `renovate[bot]`) plus the literal login `Copilot`, which doesn't carry the `[bot]` suffix despite being one. Anything else is a human.

GitHub App installations always render as `<app-slug>[bot]`, so the suffix rule covers org-specific apps without needing to name them. If an org has a machine account that is a plain user (no suffix, not `Copilot`), it will count as human — list those in `.claude/pr-stats.local.md` and filter them explicitly rather than widening the regex.

If the user explicitly asks about bot PRs (e.g. "how much sync churn is dependabot doing"), invert the filter — same shape, but `$3 ~ /\[bot\]$/ || $3 == "Copilot"` becomes the inclusion condition.

## Hard rules

- **Created-in-window, not merged-in-window, is the default.** "Did how many PRs" most often means authored, and `created` is the lighter-weight signal (no extra filtering). If the user asks about throughput-to-main or shipping velocity, switch to `--merged`.
- **Archived repos are included by default.** `gh search prs --owner <org>` covers them. If the user is asking about current activity and an archived repo shows up materially in the totals, flag it — they may have forgotten about it.
- **Don't paginate past 1000.** GitHub's search API caps at 1000 results regardless of `per_page` / `--limit`. The split-window strategy above is the only reliable workaround; bumping `--limit` higher is a silent no-op.
- **Cost is essentially zero.** A 12-month run is ~12 search API calls plus a handful of `rate_limit` polls. Don't ask the user "do you want me to do this; it might be expensive" — just do it.
- **Report logins, not people.** PR authorship is by GitHub login. Don't try to reconcile a login to an email, a display name, or a second account; if the user cares about that mapping they will say so.

## When NOT to use this skill

- **Reviewer / approver stats** — different query (`gh search prs --reviewed-by` or pull `reviews` per PR). This skill answers "who authored", not "who reviewed".
- **Line counts / +/- per author** — needs a clone-and-`git log --shortstat` approach. PR count and line count diverge a lot once one person opens lots of small PRs and another opens fewer-but-bigger ones.
- **Cross-org / personal-repo aggregation** — `gh search prs` takes `--owner`, so multiple orgs means multiple runs (or `--owner` per repo). The skill is owner-scoped on purpose; widening it loses the per-org grouping that makes the output legible.
- **Real-time dashboards** — this is a one-shot pull. If the user wants ongoing tracking, point them at GitHub Insights or set up a `/loop` invocation with a daily cadence.

## Notes

- If people join the org mid-window, their early-window count is legitimately zero, not missing data. Don't try to "fix" it by widening the search.
- If a repo is renamed during the window, both names appear in the totals. Mention this if it materially affects the top-repo ranking; don't auto-merge them — the user knows the rename context better than this skill does.
- Counts are creation- or merge-time facts and don't change retroactively, so two runs over the same closed window should agree exactly. If they don't, something is wrong with the windowing — investigate rather than averaging.
