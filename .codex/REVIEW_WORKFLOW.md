# Review Workflow

This file is synced from `codex-platform` into consumer repos. Consumer-specific
edits will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any structured review cleanup such as
`refactorpass`, or adversarial review. Local author and reviewer passes use
GitHub review threads as durable shared context:
post each verified finding inline before editing, push the correction, reply
with the fix and validation, then resolve the thread. Every pass must read
resolved as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `pr-critique`, or local review hooks.
That file is the engine-neutral protocol published by the
[`@loomantix/review-ledger`](https://www.npmjs.com/package/@loomantix/review-ledger)
project and vendored verbatim into every engine repository, so all engines read
the same contract. The helper bundle beside it is vendored from that package's
published tarball and pinned by `review-ledger.version` and
`review-ledger.integrity`; CI byte-compares the bundle, not this document, so a
protocol edit must land upstream rather than here. Where the protocol writes
`<ledger-helper>`, this engine's path is:

```text
.codex/skills/critique/scripts/review-ledger.js
```

## Roles, Not Engine Names

The relay is defined over two roles:

- **author** — the engine that wrote the change. Exactly one.
- **reviewer** — an engine that reads the change cold. Zero, one, or more.

Codex is the author role when Codex wrote the change and a reviewer role
otherwise. No rule below names a specific engine, so adding a fourth changes
nothing here.

**One non-author reviewer is the recommended floor** and covers the great
majority of changes. A second earns its cost mainly where a defect is expensive
and hard to see: auth, crypto, secret handling, schema and data-shape work,
release and sync tooling, or a change whose blast radius crosses repositories.
Solo review is permitted but must be declared with a recorded reason.

The local relay and the hosted reviewers are **not competing paths** and no
longer need to be chosen between. See "Hosted Reviewers" below.

## Review Relay

### Claude invocation boundary

This boundary applies before selecting or entering a local session mode. Start
an independent Claude reviewer only through the synced, tested
`.codex/skills/critique/scripts/run-claude-review.sh` launcher. Never invoke the
raw `claude` CLI directly or hand-compose an equivalent command. Never supply
or override Claude's model, effort, permission, persistence, or output options;
the launcher owns those settings and pins literal `--effort low`. Do not set
`CLAUDE_REVIEW_CLI` outside launcher tests. A missing, incompatible, or failed
launcher is a blocker, not permission to fall back to the raw CLI.
The launcher also owns a 45-minute pass timeout. Operators may lower it with
`LOCAL_REVIEW_PASS_TIMEOUT_SECONDS`; values above the hard 3600-second ceiling
are rejected.

The contract-v4 `agent-loop` is the sole exception: its wrapper directly
invokes the separately tested
`.codex/skills/agent-loop/scripts/run-codex-review.sh`, which starts Claude with
the same pinned effort, permission, persistence, and output policy against an
immutable base-blob `.claude` snapshot. Interactive review and every
non-agent-loop caller continue to use `run-claude-review.sh` only.

### Select the local session mode

The local relay has two explicit session modes. A consumer may declare a default;
otherwise ask the user before the first cross-engine transition. Do not switch
modes silently in the middle of a round.

- **Auto mode** lets the outer controller run the complete bounded chain.
  Resolve the effective roster first and invoke each missing declared reviewer
  through its tested launcher;
  never change an in-flight roster implicitly. For a newly declared relay the
  default direct interactive reviewer engine is `gemini`, launched through the
  Agy CLI only via `.codex/skills/critique/scripts/run-agy-review.sh`. That
  launcher pins `gemini-3.7-flash-high`, literal `--effort high`, accept-edits
  mode, unattended permissions, structured output, and the same 30-minute
  default / 3600-second hard pass bound;
  callers cannot supply or override them. It also requires Agy to resolve the
  current `deepcritique` skill, resolves its real target, and validates a
  structurally compatible relay surface from a clean, exact-commit companion
  checkout before review. A consumer may explicitly retain Claude through the
  tested `run-claude-review.sh` launcher, which keeps its literal `--effort low`
  contract. Agy starts a fresh one-shot by omitting continuation flags, but the
  current CLI has no equivalent of Claude's `--no-session-persistence`; retain
  the Claude path when local conversation persistence is prohibited. Never
  hand-compose either CLI command. After the reviewer returns, validate its
  PR-head and ledger evidence before the outer controller decides whether to
  invoke another pass. Each launcher runs exactly one reviewer pass and must
  never start another reviewer, retry itself, or continue the relay. Continue
  until convergence or the tier cap. The separate `agent-loop` wrapper remains Codex-then-Claude
  until its fixed engine slots are migrated independently.
- **Handoff mode** never starts the other engine. Each nonterminal pass posts an
  authenticated `local-review-handoff:v1` PR comment and returns control to the
  user, who starts the requested reviewer in a new terminal session. Do not
  choose the other engine's model, effort, flags, or runtime settings.

In handoff mode, when the user says `continue review on PR <number>`, `resume
review`, or similar, first load the latest authenticated handoff comment.
Continue only when it names the current engine and its exact head is still the
PR head. If it names the other engine, stop and ask the user to start that
engine in a fresh terminal.

1. Make the change, run focused validation, and create a clean local commit.
2. Push the feature branch and open or reuse its draft PR. Record the PR number,
   head SHA, and all existing review threads before any reviewer runs.
   Resolve the tier, place the user's bounded-run authorization in a private
   temporary file, and create the authenticated run marker before launching a
   pass:

   ```bash
   local-review-handoff.py start-run --repo <owner/repo> --pr <number> \
     --head <head-sha> --base <base-sha> --tier <lean|deep> \
     --authorization-file <private-file>
   ```

   The helper fixes the cap at two Lean rounds or four Deep rounds. A later run
   on the same PR requires the current run to be ended and a new, explicit user
   authorization passed with `--restart`; a new session alone is not a restart.
   Use the returned `first_round` for the first pass of that run, and increment
   it for subsequent rounds. Attestation round numbers are PR-wide: restarting
   never reuses a historical number, even after a head rewrite or an aborted
   run. `authorize-pass` reports both that `round` and the relative `run_round`;
   the two/four-round cap applies to `run_round`. Preserve earlier attestations.

3. Declare the roster with the ledger helper's `post-roster`, naming the author
   engine and this PR's reviewer engines. Participation is declared, never
   inferred: an engine that has not attested is otherwise indistinguishable from
   one that was never going to run, so nothing downstream can tell an incomplete
   round from a finished one. A solo relay is `--reviewers none` with the reason
   in the content file.
4. Fetch the target base, record its immutable commit SHA, and give that exact
   SHA to every reviewer for the round. No reviewer may re-resolve a mutable
   remote-tracking ref independently.
5. Run each declared reviewer in a fresh session against the current head. Read
   the PR ledger, post every confirmed finding inline before editing, fix,
   validate, commit, push, reply, and resolve. Codex's lane is
   `deepcritique <pr-number>` when Codex is the author engine and
   `pr-critique <pr-number>` when it is reviewing another engine's change; other
   engines use their own equivalents. Reviewer order within a round is a
   scheduling choice, not a protocol rule — what matters is which commit each
   one read.

   How the next reviewer starts is a mode choice, not a protocol rule. In auto
   mode the outer controller authorizes one pass, launches it through the
   roster-selected tested launcher, and reassesses after it returns. Handoff
   mode posts a handoff comment
   and stops, so the next reviewer begins in a fresh user-started terminal. Both
   modes carry the same comment/fix/reply/resolve contract, and neither changes
   which commit an attestation names.

6. Classify committed review fixes as `material` or `minor`. A material fix
   affects behavior, correctness, security/privacy, data safety, compatibility,
   deployment/sync integrity, or another substantive contract. Minor-only fixes
   are validated and kept.

   **The chain gets cheaper as it repeats.** Three rules make that happen, and
   all are derived from the ledger so a fresh session reaches the same answer:
   - **The refactor pass runs once per engine per PR.** A second cleanup pass over
     an already-simplified diff returns naming and shape churn, which moves the
     head and invalidates the other engines' attestations for nothing that ships.
     Each engine's cleanup lane latches on a `local-review-refactor:v1` marker;
     a docs/config-only skip does not consume it.
   - **A fix invalidates by head, not by position.** An attestation is evidence
     for the exact commit it names. A material fix does not restart the round at
     some first engine; it moves the head, which invalidates precisely those
     attestations that named the old head. An engine that already attested the
     post-fix commit stays valid and does not re-run. This is what keeps a
     second reviewer from costing a full extra round every time anything
     changes.
   - **Rounds 1–2 are adversarial; round 3 and later are convergence rounds.**
     A reviewer holding no attestation on this PR runs adversarially on its
     first cold read whatever the round ordinal: the stance tracks how many
     times that reviewer has read the change, not how many rounds elapsed
     before it joined.
     Once every declared reviewer has read the change cold twice, the remaining
     findings are mostly about the review's own artifacts. A convergence round
     runs only the lanes that can find a reason not to deploy, changes the PR
     only for a realistically reachable blocking defect, defers everything else,
     creates an issue only for an urgent high-impact follow-up, and ends as soon
     as it finds no blocker. Lanes still report everything they find —
     the narrowing is a disposition rule applied when consolidating lane output,
     never an instruction to a lane to withhold what it found.

7. Cap the loop at two Lean rounds or four Deep rounds. Before each manual
   pass, require `local-review-handoff.py authorize-pass` for its engine, exact
   base/head, and round. Only `run-claude-review.sh` and `run-agy-review.sh`
   perform this check themselves; a Codex leg and the `agent-loop` review hooks
   are bounded by `review_max_rounds` and the whole-run deadline instead, so do
   not assume a pass is budget-checked merely because it was launched.
   At cap exhaustion, stop, preserve the branch,
   worktree, and draft PR, and report non-convergence. Do not mark it ready.
8. Converge when `verify-coverage` passes at the exact current head — a roster
   is declared and every declared reviewer holds an attestation naming that
   head — the round that produced those attestations had no material fix, and
   every local-review thread contains a disposition reply and is resolved.
   Revalidate the exact PR head, then end the run with
   `local-review-handoff.py finish-run --outcome converged` and mark the PR
   ready. Record `exhausted` or `aborted` when those are the actual terminal
   outcomes. Never leave a terminal run open merely to permit another pass.

### One publication per wrapper pass

When `$AGENT_LOOP_REVIEW_PUSH_HELPER` is set, the pass runs under the wrapper
and `review-push.sh` permits **exactly one** publication for it. That overrides
the per-finding publish cadence in
[`references/local-review-ledger.md`](references/local-review-ledger.md)
"Fix, reply, and resolve", whose step 2 reads as one helper call per finding:
here, apply every fix the pass will make, commit them, and call the helper once.
A cleanup lane running inside the same pass commits locally and does not publish
for itself.

The ledger's ordering contract is unchanged — each finding is still posted
inline before it is edited, and each thread still gets its own disposition reply
naming the fix SHA. Only the number of pushes changes: the second helper call in
a pass exits with "review-push permits only one publication per reviewer pass"
and aborts the pass mid-fix.

The author engine's own adversarial pass never counts toward coverage. It
re-reads the change while still holding the rationale that produced it, which is
the opposite of the cold read the relay exists to obtain. `coverage` reports it
as `authorAttested` so the fact stays visible, but the tier counts distinct
non-author engines only.

The `agent-loop` skill automates a two-engine instance of this relay with a
required non-mutating validation hook plus `review_max_rounds`,
`codex_review_hook`, and `claude_review_hook`. It still encodes a fixed
Codex-then-Claude order and a position-based restart rather than the head rule
above; that is deliberate for now, and the roster and head-exact rules do not
yet reach it. Under contract v3 and v4 every hook writes a structured clean,
changed, or blocked result to `$AGENT_LOOP_REVIEW_RESULT_FILE`. The wrapper
validates that result against observed Git state and the v3 ledger, then posts
the canonical pass/completion attestation itself. It opens a draft PR before
review, exports the pinned PR identity to both hooks, checkpoints private atomic
run state, and verifies that each hook leaves local, remote, and PR heads
aligned. Consumer hooks own semantic finding verification, deterministic inline
posting and disposition, and classification; they must fail or return blocked
if a valid finding or undisposed local-review thread remains.

An automated wrapper must make its mode explicit. Contract-v3/v4 auto mode requires
`config_doctor = true` and `claude_effort_policy = low`; for v3 the doctor
requires exactly one literal `--effort low` option in the Claude hook, while for
v4 it verifies the pinned launcher's effort-policy query before selection or
claim. Handoff mode stops after each nonterminal engine leg and uses the same
PR-comment protocol as an interactive review.

## Cross-Engine Session Handoff

This section is engine-specific and lives here rather than in the vendored
protocol document, which stays byte-identical across every engine.

### Post the handoff at the end of a pass

In handoff mode, never start another reviewer from the current session. At the
end of a pass, post a deterministic PR comment carrying the exact base, current
head, completed engine and round, outcome, next reviewer, and a pasteable
fresh-session prompt:

```bash
python3 .codex/skills/critique/scripts/local-review-handoff.py post-handoff \
  --repo <owner/repo> --pr <number> --head <full-head-sha> \
  --base <full-base-sha> --from-engine <engine> \
  --to-engine <engine> --round <completed-round> \
  --outcome <clean|minor|material|blocked> \
  [--context-file <public-safe-regular-utf8-file>]
```

The helper owns the `local-review-handoff:v1` marker and prompt. It verifies the
PR head before and after posting, verifies the comment read-back, rejects marker
injection from optional context, and makes an identical retry idempotent.

### Read the prior pass before every review

Before running any lane, read what the other engines already left at this head.
This is unconditional: it does not depend on the session mode, and it does not
depend on the user having said "continue" or "resume". A reviewer that starts
cold re-derives findings another engine already posted inline, which is the
expensive failure this read exists to prevent.

**The ledger attestation is the authority.** Every
`local-review-pass:v3` / `local-review-complete:v3` marker on the PR carries the
engine, round, base, before and final head, the classification, and the full
finding-fingerprint set of the pass that produced it. Read those markers, their
bodies, and the inline v3 threads they name. Do not re-litigate a fingerprint
another engine dispositioned at this head. `coverage` and `verify-coverage`
report the same state mechanically.

`local-review-handoff:v1` is a Codex-side enrichment on top of that record, not
a substitute for it and not a precondition. **Never refuse to start because no
handoff comment exists** — an engine that does not implement this protocol never
writes one, so its absence carries no information about whether the previous
pass ran. Where one is present it is the
richer read:

```bash
python3 .codex/skills/critique/scripts/local-review-handoff.py show-handoff \
  --repo <owner/repo> --pr <number> --engine <engine>
```

The helper considers the latest handoff from the authenticated GitHub actor,
verifies its content digest and exact live PR head, and fails if the comment
targets another engine. Never fall back to an older handoff addressed to the
current engine. A failure that means "targets the other engine" is a genuine
stop; a failure that means "none found" is not.

A handoff records who runs next; it is not evidence of review. Coverage still
comes from attestations naming the exact head, so a handoff neither creates nor
invalidates one.

## Hosted Reviewers

Hosted AI reviewers — the Gemini Flash and Copilot passes `reviewit` drives on
the PR itself — are a **different style of review**, not a fallback and not a
later phase. Run one whenever it is useful: before the relay, between rounds,
after convergence, or as the only review on a change that does not warrant a
local relay.

**The local relay is the default path here.** Coverage is expected to come from
declared roster engines reading the change cold, and that is what
`verify-coverage` measures. The hosted lane is an extension on top of that.

It stays fully supported because it is the primary path for a consumer whose
developers have no local agent engine — a repository with no local CLI and no
declared roster still gets real review from a hosted pass. That is the case the
lane exists for; it is not the case these defaults are tuned for.

A repository with no local engine has no roster, so `verify-coverage` does not
apply to it. There, convergence is the hosted lane's own contract: every hosted
finding disposed and resolved, and a final iteration that produced no fix. A
roster-less PR converges on that rule and must not claim relay coverage.

A hosted pass **invalidates nothing on its own.** Only a commit invalidates, and
only by the head rule in step 6, which treats a hosted-review fix exactly like
any other:

- a minor fix leaves attestations at the old head stale for the ordinary reason,
  and the affected engines re-run when the relay next needs them;
- a material fix means the round had a material transition and does not
  converge, the same as if a local reviewer had made it.

Classify a hosted-review fix by its effect on the code, with the same
material/minor rule as everything else. "A hosted reviewer touched this" is not
a category.

Hosted reviewers are not roster participants. They post under their own
identities, so their comments are context rather than actor-owned ledger
evidence, and they do not attest. Coverage counts local engines only — a hosted
pass does not turn a solo relay into a cross-model one.

Invocation:

- **Lean** — `reviewit <pr-number>` for the bounded Gemini Flash and Copilot
  loop. It verifies and deduplicates their findings, fixes confirmed issues,
  pushes, replies, and loops within its cap. Run it after
  `refactorpass <pr-number>` and `critique <pr-number>` when the local relay is
  also running.
- **Deep** — `reviewit <pr-number> deep`, with the larger cap and early-exit
  rules. Its final local `deepcritique` receives the same PR number and ledger,
  and skips the refactor pass when this engine's cleanup latch is already spent
  on the PR.

## Review Tier

Resolve the tier **before the first reviewer runs**, on every path. An
unresolved tier is not a neutral state — it is how the expensive path becomes
the default. **Lean is the default; Deep is the exception you justify.**

State the resolved tier and the trigger that selected it — or `no trigger` — in
the pass output, and post the ledger's `local-review-tier:v1` marker once per
PR. The marker is per-PR and shared across engines: later rounds resolve the
effective marker under the ledger's authenticated, forward-only transition rule
instead of reclassifying the unchanged range. A tier re-derived from scratch
each round, or re-derived against a different list in each engine, drifts back
to Deep.

### What sets the tier

Tier is set by what a missed defect reaches, not by how hard the change is to
review. Difficulty is the wrong input: every subtle diff feels like it deserves
more scrutiny, and that feeling is what pulls a whole repo onto the deep path.

Resolve the changed-file list once with
`git diff --name-only <base-sha>..<head-sha>`, then walk the triggers below.
**Any one selects Deep; no trigger means Lean.**

1. **Sensitive path** — authentication, authorization, cryptography, secret or
   credential handling, PHI/PII, tenant or customer isolation. However small
   the edit.
2. **Irreversible in production data or a published artifact** — migration,
   backfill, a published package's API or version: anything a revert cannot
   undo.
3. **Fans out past this repo** — the synced `.codex/**` surface, the sync
   engine, a published package, a contract other repositories consume. One
   defect lands in every consumer.
4. **Non-obvious behaviour in deployed runtime code** — concurrency, retries or
   idempotency, cache invalidation, money or clinical calculation, state
   machines, partial-failure and rollback paths: correctness that is not
   readable from the diff.
5. **Recurring-incident area** — the touched paths produced a post-merge defect,
   revert, or hotfix in roughly the last 90 days. Evidence is a specific defect,
   revert, or hotfix commit you can name; an active path with ordinary commit
   traffic is not evidence.
6. **Explicitly requested** — a human directly asked for a deep review, or the
   change is a first of its kind the author cannot self-assess. An internal
   `deep` argument passed between tier-aware skills only asserts the recorded
   tier; it is not a new request.

### What does not set the tier

Subtlety does not: a change can be hard to reason about and still be Lean. Nor
does diff size — a large mechanical refactor is Lean unless it also trips
trigger 4. Nor does topic adjacency: code _about_ security that does not itself
enforce a sensitive boundary is not trigger 1.

**The dominant rule: when the worst outcome of a missed defect is a red CI run,
a broken build, or a broken developer workflow, the change is Lean.** CI
scripts, lint rules, build tooling, developer utilities, fixtures, and test
harnesses land here even when they are subtle and even when a defect in them
fails open. That class of defect is caught by the next person the tool touches
and fixed by editing the tool.

Classify enforcement controls by the consequence of failure, not by their CI
location. A secret/privacy scanner, provenance gate, or release guard is Deep
when failing open can expose protected data, grant access, or compromise a
published artifact; that outcome trips trigger 1 or 2 rather than this rule.

**Precedence: walk triggers 1–6 first. The dominant rule only resolves a change
that matched no trigger.** It is dominant over the difficulty instinct, not over
the trigger list. Tooling that also fans out past this repo — the sync engine, a
shared CI action, anything under `.codex/` — is trigger 3 and therefore Deep,
because its blast radius is not confined to the developer who runs it.

### Round budget and stopping rule

A round is one complete pass per available engine at the same head.

- **Lean — cap 2.** Round 1 is adversarial. Round 2 runs only if round 1 made a
  material fix, and runs in convergence mode.
- **Deep — cap 4.** Rounds 1–2 adversarial, rounds 3–4 convergence.

**Stop as soon as a complete round produces no material fix.** That is the
stopping rule for both tiers, and it is a rule rather than a budget to spend: a
Lean change that lands after one clean round has had enough review.

A Lean change that reaches round 3 has either been mis-tiered — escalate it
deliberately, below — or is not converging, which is a signal about the change
rather than a licence for another round. Say which, and stop.

### Escalate and de-escalate on evidence

Both moves require a confirmed finding. A suspicion, an unverified severity
label, or "this feels risky" is not evidence and does not move a tier.

**Lean → Deep.** Escalate when a confirmed finding shows the change reaches a
trigger the classification missed — a real authorization or isolation bypass, a
real data-shape change, a real break in a contract another repository consumes —
or when the human directly requests Deep, which is trigger 6. Name the finding
or request and the trigger, post a replacement tier marker that preserves every
recorded trigger and adds the new one, and adopt the Deep budget. The round
already run counts as Deep round 1; do not restart the count.
**The first round after an escalation is adversarial whatever its ordinal.**

**Deep → Lean.** De-escalate when Deep round 1 completes and _every_ lane owning
a recorded trigger returned no confirmed finding. Finish at Lean: the lean lane
set, one further round at most. Running the full matrix again over a
substantively unchanged diff audits the review rather than the change. Record
the de-escalation and the lanes that came back clean.

Trigger 6 — an explicitly requested deep review — is never de-escalated. The
request is the evidence, and no clean lane overrides it. For the rest, a trigger
de-escalates only through the lane that owns it:

| Trigger                          | Owning lane                               |
| -------------------------------- | ----------------------------------------- |
| 1 sensitive path                 | security reviewer                         |
| 2 irreversible data or artifact  | code reviewer (migration/compat pass)     |
| 3 fans out past this repo        | code reviewer on the consumed contract    |
| 4 non-obvious deployed behaviour | silent failure hunter + code reviewer     |
| 5 recurring-incident area        | code reviewer scoped to the incident path |
| 6 explicitly requested           | not de-escalatable                        |

Tier selection narrows which lanes run and how many rounds are owed. It never
narrows what a lane may report, and it never relaxes the post-before-editing,
reply, or resolve contract.

`deepcritique` runs only on a resolved Deep tier and hands a Lean changeset back
to `critique`; typing the deep skill does not select the deep path. `reviewit`'s
iteration cap matches the tier's round cap numerically — two at Lean, four at
Deep.

## Skip Path

For docs/config-only changes, skip expensive review automation unless the user
explicitly wants it. Source-code changes include common implementation
extensions such as `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.rs`, `.go`, `.java`,
`.cpp`, `.c`, `.h`, `.cs`, `.rb`, `.swift`, `.kt`, `.sh`, and `.bash`.

## Reviewing Another Engine's Change

When Codex holds the reviewer role, run `pr-critique <pr-number>` from an
isolated worktree. It reads the existing ledger, runs the deep matrix, posts
confirmed findings inline, applies fixes, and completes those same threads.

If that pass commits a fix, it moves the head and invalidates the attestations
that named the old commit — including the author engine's. Those engines re-run
against the new head; engines that had not yet attested are unaffected. The
hand-back is the head rule, not a separate obligation.

## Pass Telemetry

Every pass records what it cost, as a `local-review-telemetry:v1` marker in its
own PR comment. The record carries token buckets per exact model, classified
line churn, finding dispositions, and the pass identity needed to ask whether
Deep earns its cost. This engine reports its prompt side as one figure with the
cached and cache-write portions inside it, so the helper subtracts them to reach
the disjoint canonical buckets and keeps the reported figure alongside as a
provider bucket — that arithmetic is what makes an `input` count here mean the
same thing it means for another engine.

The record carries no prose, no file paths, no finding titles, and no money:
rates move and a subscription's marginal cost is zero, so a stored dollar figure
is wrong when written and unverifiable later. Counts keep the whole series
re-priceable.

### Two gates: measuring and publishing

Extraction and emission are separate decisions and have separate gates. Both
are read by the usage helper and nowhere else; both are environment
configuration set once, never an interactive prompt during a pass, because a
prompt would block an autonomous run.

| Variable                        | Governs                             | Default           |
| ------------------------------- | ----------------------------------- | ----------------- |
| `LOOM_REVIEW_TELEMETRY`         | emitting a record to a pull request | off               |
| `LOOM_REVIEW_TELEMETRY_EXTRACT` | measuring this pass at all          | the emission gate |

Each accepts exactly `on` or `off`. Any other non-empty value is neither: the
helper stays disabled and says why, so a typo reads as a misconfiguration
rather than as a deliberate opt-out.

Publication is the part that warrants an opt-in rollout, so it is the part that
keeps the original variable and its original meaning — nothing changes for a
repository that has already set it. Measurement is separable because a local
consumer of usage data has no business publishing anything: set
`LOOM_REVIEW_TELEMETRY_EXTRACT=on` with the emission gate off and the numbers
are available while emission is structurally unreachable rather than merely
unrequested.

The helper reports `enabled` for extraction and `emit` for emission on every
payload, in every mode and on every failure path. **Invoke `emit-telemetry`
only when `emit` is true**, and never derive that from anything else.

### Never read a telemetry marker

A pass must not read prior telemetry: not into a finder prompt, not into a
review packet, not into context assembly, not into a summary of the PR. An
agent that can see its own measured cost and a readable trend has been handed a
target, and the thing it can most easily optimise is the review rather than the
spend. Reporting **this** pass's own numbers at the end, after findings are
posted, is fine — a single figure with no baseline is not a trend.

Filter by the `local-review-telemetry:` prefix rather than by a list of known
markers, so a record type added later is excluded by default instead of leaking
into reviewer context until someone teaches the filter about it.

### Snapshot before, delta after

`<usage-helper>` is `.codex/skills/critique/scripts/usage-snapshot.js`, beside
the vendored ledger helper and invoked the same way. It binds the coordinator
to the host-provided session ID and includes new descendant sessions spawned by
that coordinator during the pass; the ledger helper never reads session logs,
and never may.

Write telemetry working files to `$AGENT_LOOP_LOG_DIR/telemetry/` when that
variable is set, or to another owner-only directory outside the Git worktree.

Before the first reviewer, cleanup agent, or classification read — early enough
that the pass's own setup counts as part of its cost:

```bash
node <usage-helper> snapshot --out "<telemetry-dir>/usage-start.json"
```

After the pass has finalized its v3 result, and after any fix commits:

```bash
node <usage-helper> delta \
  --start "<telemetry-dir>/usage-start.json" --out-dir "<telemetry-dir>"
```

`delta` prints `enabled`, `emit`, `tokenSource`, nullable `engineVersion`,
nullable `durationSeconds`, and the paths it wrote. This engine reports no per-lane
attribution, so `lanesFile` is always null and `lanes` is absent from the record
rather than empty. Pass non-null values through verbatim and omit their flags
when null. `tokenSource` is the provenance of the numbers and must never be
upgraded by hand:

- `session-log-delta` — measured, scoped to this pass.
- `unscoped-session` — measured, but a truthful upper bound rather than this
  pass's cost. A standalone pass with no start snapshot lands here.
- `unavailable` — no usable data, and the record carries **no** token buckets.

**A pass with no usable usage data must never emit zero tokens.** A zero makes
the engine look free and skews every average in its favour, and it is the kind
of defect that survives a year because the dashboard still looks plausible.
Aggregation excludes a missing measurement; nothing zero-fills it.

### Identify the prompt stack

A record that says what a pass cost but not what it was running cannot answer
"did that prompt change help" — findings-per-token is a property of the prompt
as much as of the model. `<hash-helper>` is
`.codex/skills/critique/scripts/prompt-stack-hash.js`. Run it once per pass,
any time after the boundary and before emission:

```bash
node <hash-helper> --repo-root "<repository root>"
```

It reads only files already checked into the repository — no session log, no
path under a home directory — so it is ungated and safe to run anywhere. It
always exits 0 and always prints one JSON object carrying `promptStackSha256`,
`repoInstructionsSha256`, and `hashInputVersion`. Pass the two digests through
verbatim and omit the corresponding flag when one is null. Nothing else it
prints is an `emit-telemetry` argument.

**The two digests are never collapsed into one.** The synced stack is
fleet-wide and moves when upstream moves; repo-local instructions are per
repository. A combined digest would make every repository look like a different
prompt generation forever, which destroys the cross-repository correlation the
hash exists to enable.

#### Hash input, version 1

The digest input is a definition, not an implementation detail. Two engines
that hashed the same stack in different orders would mint two identities for
one prompt generation, which reads downstream as a real difference and is worse
than having no hash. Version 1 is:

- **Prompt stack** — an enumerated list of synced review prompt files, not a
  glob: `REVIEW_WORKFLOW.md`, `references/local-review-ledger.md`, the six
  review lens roles under `references/roles/`, and the `critique`,
  `deepcritique`, `pr-critique`, `refactorpass`, and `reviewit` skill bodies.
  `code-architect` and `code-explorer` are excluded because no review pass loads
  them. Scripts are excluded too: the ledger bundle and the usage extractor are
  not prompts, and folding them in would move the digest on every ledger
  release. This engine's lens prompts sit inside its synced surface and the
  sibling engine's do not, which is a difference in what each engine can read in
  a consumer checkout rather than a difference in method — the two stack digests
  were never comparable to each other, since each names one engine's own prompt
  generation.
- **Repo instructions** — root `AGENTS.md` and root `CLAUDE.md`, both declared
  for both engines so the same repository state yields the same digest whichever
  engine emitted the record. Nested instruction files are out of scope: their
  discovery depends on which directories a pass happened to touch.
- **Order** — paths sorted byte-wise, computed from the set rather than taken
  from the order the list is written in.
- **Normalisation** — a leading UTF-8 BOM is stripped and CRLF and lone CR
  become LF, so a checkout with `core.autocrlf` on holds the same prompt
  generation as one without. Nothing else is normalised: trailing whitespace
  and blank-line changes are real edits to a prompt and must move the digest.
- **Framing** — the digest is taken over per-file digests, each record carrying
  its path, prefixed by a domain string that names the hash-input version and
  which set it is. A rename is a change, a file moved between the two sets
  cannot collide, and no byte can be shifted across a file boundary unnoticed.
- **Absence** — a declared file that is absent is recorded as absent rather than
  skipped, so a consumer that opted out or drifted has a visibly different
  identity. A set with nothing present at all yields null, not the digest of
  "everything absent".
- **Failure** — a declared file that exists and cannot be read yields null for
  that whole digest. Never a partial hash: one covering part of the stack is
  indistinguishable from one covering all of it.

The version is mixed into the digest rather than reported beside it, so a later
redefinition cannot silently rewrite the meaning of records already emitted —
version 2 produces different digests by construction. Changing the file list,
the order, the normalisation, or the framing **is** a redefinition and bumps it.

`hashInputVersion` is not `promptStackVersion`. The latter is the prompt stack's
semantic version, which nothing computes yet; it stays null and is not this
helper's to fill.

### Count the findings

Write this pass's own dispositions to a regular file with the active
file-editing tool — never a heredoc or command substitution:

```json
{
  "posted": 0,
  "bySeverityAndOutcome": {
    "blocking": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "major": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "minor": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 },
    "nit": { "validFixed": 0, "validDeferred": 0, "invalidDismissed": 0 }
  },
  "chainInducedRegressions": 0
}
```

Count only threads this pass posted or dispositioned, and keep `posted`
greater than or equal to the sum of every disposition: the emitter checks that
and refuses the record otherwise. A pass that dispositions a thread an earlier
pass posted — fixing a deferred finding, say — therefore counts it in `posted`
as well, so the record stays internally consistent. `chainInducedRegressions`
counts new fingerprints whose diff anchor traces via `git blame` to a commit
recorded as a fix SHA in an earlier disposition **on this PR** — rework the
chain caused itself, which is far more expensive than either a finding or a
clean pass. A `reopen-occurrence` is not one of these: that is the same defect
still present, not a new one the chain introduced.

### Emit the record

```bash
node <ledger-helper> emit-telemetry \
  --repo <owner/repo> --pr <number> --engine codex \
  --base <full-base-sha> --head <full-head-sha> \
  --pass-type <review|refactor> --review-tier <lean|deep> \
  --trigger <autonomous|interactive> --round <n> \
  --stance <adversarial|convergence> --status <clean|changed|blocked|skipped> \
  --token-source <from delta> --engine-version <from delta> \
  --duration-seconds <from delta> \
  --tokens-file <from delta> \
  --prompt-stack-sha256 <from hash helper> \
  --repo-instructions-sha256 <from hash helper> \
  --findings-file <path>
```

**Skip this step entirely when the helper reports `emit: false`.** A pass that
emitted anyway would post a telemetry record on a repository that opted out.
There is nothing to publish when the emission gate is off, whatever extraction
did.

`emit: true` with `enabled: false` — extraction switched off on a repository
that still publishes — is a legitimate combination. The helper reports
`tokenSource: unavailable` itself and the record carries no usage; pass that
through like any other value rather than repairing the gap by hand.

Omit `--engine-version`, `--duration-seconds`, and `--tokens-file` whenever
`delta` reported the corresponding value as null, and
`--prompt-stack-sha256` / `--repo-instructions-sha256` whenever the hash helper
did.
Omit `--changeset-file` and the classifier runs over `<base>..<head>` itself.
Add `--truncated` when a lane silently truncated the diff it was given: a lane
that reviewed less than it was asked to produces cheap, bad findings, which is
the exact pattern that otherwise reads as efficiency.

`trigger` is `autonomous` when a runner set the `AGENT_LOOP_*` variables and
`interactive` otherwise.

### What each pass emits

| Pass                                  | `--pass-type`      | `--status` |
| ------------------------------------- | ------------------ | ---------- |
| Adversarial pass, nothing to fix      | `review`           | `clean`    |
| Adversarial pass that committed a fix | `review`           | `changed`  |
| Pass that could not complete          | `review`           | `blocked`  |
| Docs/config-only skip                 | matching pass-type | `skipped`  |
| Cleanup pass that committed           | `refactor`         | `changed`  |
| Cleanup pass that found nothing       | `refactor`         | `clean`    |
| Cleanup skipped on a spent latch      | `refactor`         | `clean`    |

A skip still burns tokens reading and classifying the PR, and "we spent eight
thousand tokens deciding not to review" is exactly the machinery overhead worth
seeing. `skipped` is reserved for the changeset that had nothing reviewable in
it — the record rejects a `skipped` pass carrying review-significant files, so a
cleanup pass that stopped on a spent latch reports `clean` instead. Its
changeset was reviewable; this engine had simply already spent its one pass.

### Emission failure is never fatal

`emit-telemetry` exits zero either way and prints `emitted` with the reason on
failure. Report that outcome and move on. Never retry it into the review, never
let it change the v3 result, and never let it delay marking the PR ready. This
is why the record is a separate marker in a separate comment rather than an
extension of the attestation, whose body is byte-verified and hash-checked: a
telemetry defect must not fail a review that found real defects.

## Review Principles

- Treat every generated finding as a hypothesis. Verify it against code, tests,
  and documented constraints before posting or changing anything.
- Fix a confirmed finding only when the likelihood and impact of real user harm,
  or a credible path to security exploitation, justify the fix's churn and
  regression risk.
- Create a follow-up issue only for a concrete, high-impact defect that should
  be scheduled within roughly two weeks. Record ordinary deferrals without an
  issue; do not turn speculative hardening, cleanup, or low-likelihood edge cases
  into backlog.
- A review fix without a preceding inline finding, a finding without a reply,
  or a resolved thread without a visible disposition is a failed review pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata. Only the concise verified finding and its disposition belong there.
- Stop at the configured cap and preserve recovery state when reviewers do not
  converge.
