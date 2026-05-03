# GitHub API Usage — canonical Claude guidance

Drop this section into your repo's `CLAUDE.md` (or reference it from there) so Claude sessions follow the same rate-limit-aware patterns when calling `gh` / the GitHub API.

---

## Section: GitHub API Usage

GitHub's REST API is 5000 req/hr authenticated, and secondary anti-abuse limits can trip sooner on polling loops or rapid comment creation. Claude should optimize for fewer calls — a long agent session can easily burn 1000+ requests across tasks, and rate-limit exhaustion mid-session blocks every `gh` command until reset.

**Fetch once, reuse:** Run `gh pr view <N> --json <fields>` once at the start of a task and thread the result through your steps. Don't re-fetch PR metadata you already have. For multi-PR sweeps, prefer one `gh pr list --json ...` with a filter over N individual `gh pr view` calls.

**Waiting on CI / reviews — prefer streams over polling:**

- `gh run watch <run-id>` streams job completion in a single long-lived call. Use it instead of looping `gh pr checks`.
- When polling is unavoidable (e.g., waiting for Copilot + Gemini reviews that don't have a stream endpoint), use exponential backoff starting at 60s and capping at 5min. Never poll sub-minute.
- For "wake me up when the 30-min review window is probably done", use `ScheduleWakeup` with ≥1200s delay — one check beats twelve.

**PR comment replies — batch or summarize:**

- For review-cycle-style workflows with N inline findings, prefer **one summary comment** ("fixed all findings in `<sha>`, see per-file notes below") over N individual thread replies.
- When per-thread replies are warranted, batch via a single GraphQL mutation with aliased `addPullRequestReviewThreadReply` operations — one network call instead of N REST calls.
- Never loop `gh api ... /replies` in a shell `for` loop; that's the worst offender for secondary rate limits.

**Headroom check before heavy work:** If a task will generate many calls (branch babysitting, review cycles, multi-PR sweeps), check `gh api rate_limit --jq .rate.remaining` first. Below 500, back off or batch more aggressively.

**Respect rate-limit backoff headers:** On 403 or 429, honor `Retry-After` when present (seconds to wait, used for secondary/abuse limits); otherwise fall back to `x-ratelimit-reset` (epoch timestamp, used for primary quota exhaustion). Don't retry in a tight loop.

**GraphQL for aggregated reads:** A single GraphQL query can fetch PR + reviews + inline comments + checks in one request. Prefer it over 4 parallel REST calls when you need more than one of those.
