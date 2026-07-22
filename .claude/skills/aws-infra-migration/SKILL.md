---
name: aws-infra-migration
description: >-
  Drive a fenced, one-writer-at-a-time migration of a Terraform root's live state
  from one CI/repo into another (or into a central Terraform pipeline) with a
  zero-diff, identity-preserving cutover. Use when moving Terraform roots between
  repos or CI systems, consolidating infra into central Terraform CI, re-keying
  remote state, or when the user says "infra migration", "state cutover", "fenced
  cutover", "move the <X> root", or opens a per-root migration window. Read-mostly:
  every live state mutation is explicitly gated on a human go.
---

# AWS infra migration — fenced one-writer state cutover

Move a Terraform root's **live remote state** from one CI/repo (the _source_) to
another (the _destination_) without split-brain, keeping every managed resource's
identity so running workloads never notice.

**The one invariant: exactly one writer for a given state key at every instant.**
Source and destination use different backend `key`s, so the DynamoDB lock table
locks them _independently_ — there is no shared lock. "Copy the object and leave
the old workflow armed" is split-brain. You make one writer, fence the other, and
only ever move forward.

## Orient first (do not reinvent)

The per-root procedure should be authoritative in the **destination infra repo**,
conventionally at `docs/runbooks/fenced-cutover.md`, with a
`transfer-manifest.template.md` and filled exemplars under `docs/runbooks/manifests/`.
Read the runbook and the most recent exemplar; copy the exemplar's shape. This
skill is the operator's wrapper around that runbook — it does not replace it. If
they disagree, the runbook wins (and fix this skill).

If the destination repo has no such runbook yet, the first migration should
produce one; a per-root manifest is the only rollback path you will have.

## Actor split (who can do what)

| Action                                                                                                                | Actor                                                                                           |
| --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Edit `.tf` / backend key, open PRs, drive CI, fence source, fill manifest                                             | dev (you)                                                                                       |
| Backend **state object** ops (`head-object` / `copy-object` / snapshot) + hand-apply `bootstrap/` + policy-simulation | **admin credentials** (a privileged SSO/role profile) — the dev role cannot touch backend state |
| **Go / no-go at every gate and before every state mutation**                                                          | the human owner                                                                                 |

Check the admin profile is live before you rely on it
(`aws sts get-caller-identity --profile <admin>`); if expired, ask the owner to
re-login. Never admin-merge (`gh pr merge --admin`) past branch protection on
repos that enforce reviews or commit signatures.

## Per-root loop

For each root, in order (**PAUSE for the owner's go before any live mutation** —
fence toggle, bootstrap apply, copy-object, destination apply/merge, source
retire):

1. **Pick the next root by correctness, not effort.** Prefer clean single-root
   canaries first (no cross-state ownership, no secret-bearing state, no live
   prod-user dependency, doesn't own the backend bucket). Leave the cluster root
   and the backend-bucket root for last.
2. **Pre-gates (below) — all green or STOP.**
3. **Fence** the source apply path (see Fence strategy).
4. **Drain** in-flight source runs; confirm the source key's lock is released.
5. **Snapshot** (admin): source `VersionId` + ETag + length (`head-object`);
   `lineage` + `serial` + instance count; config-dir + `.terraform.lock.hcl`
   hashes. Record every value in the manifest — the version IDs are the _only_
   rollback path.

   **Never read state with a bare `terraform state pull | jq`.** An unfiltered
   `jq` is the identity filter: it prints the whole state document, including
   every plaintext sensitive attribute, to your terminal and into any CI log or
   shell history that captures it. Stream the object and project only the three
   scalars you need — this form is safe on _every_ root, so use it as the default
   rather than remembering to switch on the secret-bearing ones:

   ```bash
   aws s3 cp "s3://<bucket>/<key>" - --profile <admin> \
     | jq '{lineage, serial, instances: ([.resources[].instances | length] | add)}'
   ```

6. **Copy the exact version** (admin, after go). Gate 3 established the
   destination key was absent, but that was a check at a point in time — between
   the gate and the copy, anything with write access could create it. Make the
   copy itself refuse to overwrite rather than trusting the earlier check:

   ```bash
   aws s3api copy-object --profile <admin> \
     --copy-source "<bucket>/<srckey>?versionId=<V>" \
     --bucket <bucket> --key "<destkey>" \
     --if-none-match '*'
   ```

   `--if-none-match '*'` fails the request with `PreconditionFailed` if the
   destination key already exists, closing the time-of-check/time-of-use window.
   **Record the `VersionId` the copy returns** — post-apply the destination has a
   newer version, and rollback needs to name the exact one it is restoring from.
   Then verify dest lineage/serial/count/ETag EQUAL the recorded values. Any
   mismatch = STOP.

7. **Add `infra/<root>/` to the destination** (fresh worktree): byte-identical
   `.tf` except the backend `key`; keep `.terraform.lock.hcl`. Open the PR →
   destination plan **must be `0/0/0`** (read the actual plan comment — "No
   changes" — not just a green check; plan exits 0 on a diff). Merge → sole-writer
   apply `0/0/0`. Verify post-apply serial unchanged.
8. **Observe**, then **retire** the source `.tf` (tombstone; delete the source key
   only after observed-green — versioning is the safety net). Finalize the manifest.

## Pre-gates (the make-or-break part)

**Gate 1 — CI policy for the root's services, hand-applied FIRST.**
The destination CI plan/apply roles need least-privilege permissions for exactly
this root's services before its first plan/apply.

- **Derive the action set from what the provider ACTUALLY calls, not from CRUD.**
  Enumerated CRUD is not enough — the AWS provider makes _implicit_ read calls on
  every refresh that fail the first plan with `AccessDenied` if missing. The
  reliable way to enumerate them is a `TF_LOG=trace` plan under admin creds, then
  grep the signed requests for the action names.

  **Treat the specific call-level claims below as observations, not as a spec.** Which
  implicit reads a refresh makes is provider-implementation behavior and changes across
  provider versions — the AWS **authorization** references (action names, ARN forms) are
  stable and authoritative, but "this resource's Read also calls X" is only true of the
  provider version you traced. Record the provider version alongside the grant in the
  manifest, and re-trace rather than re-use the list after a provider major bump. Three
  classes bite repeatedly:
  - **Tag reads** — but check HOW the service returns tags first. If the root's
    `provider.tf` sets `default_tags` (or a resource sets `tags`), the mutating side
    (`TagResource`/`UntagResource`) is needed on the apply role for any tag change.
    For the READ side it depends on the service:
    - Services whose Read/Describe does **not** return tags make a **separate**
      tag-read call on every refresh via the transparent-tagging interceptor. Miss
      it and the first plan `AccessDenied`s. Grant the read to both roles.
    - Services whose Read/Describe returns tags **inline** make **no** separate
      call — e.g. `secretsmanager:DescribeSecret` returns `Tags` in its response,
      so `default_tags` adds no read action. Do **not** invent a `GetTags`-style
      grant it will never use.
    - **The tag-read VERB SPELLING is per-service — a plausible guess grants
      nothing**, because IAM silently no-ops an action name that doesn't exist for
      that service. These are all real, all different, and all mean "read this
      resource's tags":

      | Service             | Tag-read action       |
      | ------------------- | --------------------- |
      | CloudWatch (alarms) | `ListTagsForResource` |
      | CloudWatch Logs     | `ListTagsForResource` |
      | RDS                 | `ListTagsForResource` |
      | S3 Control          | `ListTagsForResource` |
      | Glue                | `GetTags`             |
      | DynamoDB            | `ListTagsOfResource`  |

      Look the verb up in the service authorization reference per namespace; don't
      pattern-match it from the last root. Note CloudWatch and CloudWatch Logs are
      **different services** that happen to share a verb spelling — granting one is
      not granting the other.

  - **Sub-resource reads the provider makes unconditionally.** A resource's Read
    can fan out to describe/list calls for sub-resources that don't exist in your
    configuration — e.g. Glue's `GetPartitionIndexes` fires on every
    `aws_glue_catalog_table` read even when no partition index is defined. These
    never appear in a CRUD-derived action list.

  - **Encryption-at-rest pulls in a KMS read even when the root declares no KMS
    resource.** A resource encrypted with SSE-KMS resolves its key during Read, so
    the refresh calls `kms:DescribeKey` — including for **AWS-managed** keys (e.g.
    the `aws/dynamodb` key behind an SSE-KMS table). Grant `kms:DescribeKey` on that
    key. Note this is strictly the _metadata_ read: `kms:Decrypt` stays absent, so
    CI still cannot read the encrypted data.

- **Cross-check the currently-working source role.** The source CI role that plans
  this root today already holds the exact permission set a working plan needs
  (often a broad `<svc>:Get*`/`List*`). Pull its policy and make sure your tightened
  enumerated grant covers everything its wildcard would.
- **Policy-simulate under the real roles.** `aws iam simulate-custom-policy` (or
  `simulate-principal-policy` post-apply) — assert: plan reads allowed / plan
  mutations implicit-deny (read-only preserved); apply in-scope mutations allowed /
  out-of-scope resources implicit-deny; any trust-anchor protect-deny still
  explicit-deny. A green admin plan does NOT prove the CI role's scoping.
- **KMS stays hand-applied** in `bootstrap/` — CI gets zero KMS-create.
- The `bootstrap/` root is the trust anchor: **hand-applied by admin, never CI.**
  Verify its plan is _only_ your intended statements (e.g. `0 add / 2 change /
0 destroy` = the two inline role policies), no trust change, no drift.

**Gate 2 — source `0/0/0` vs live**, at a pinned freeze SHA (source `main` tip).
The migration zero-diff gate can't tell expected-state from un-captured drift, so
reconcile any drift into source first. If the source drift-detection workflow is
fenced/disabled, either temporarily re-enable it, dispatch a single read-only plan
for this root, confirm "No changes", and re-disable; or run `terraform plan`
locally under admin creds. Record the run/evidence.

**Gate 3 — destination key absent** (`head-object <destkey>` → 404). This is a
point-in-time check, not a guarantee: pair it with `--if-none-match '*'` on the
copy (step 6) so the write itself is what enforces non-overwrite.

**Gate 4 — backend bucket versioning is `Enabled`.**

```bash
aws s3api get-bucket-versioning --bucket <bucket> --profile <admin> --query Status
```

Every rollback instruction in this document assumes a prior object version can be
retrieved by ID. If versioning is `Suspended` or absent, there is no rollback path
and the whole procedure is a one-way door — stop and fix that before touching any
state. Confirm the value; do not infer it from the bucket having been created by a
module that usually enables it.

## Hard rules / gotchas

- **Verify the root's ACTUAL resources — trust neither its name nor its README.**
  Grep the `.tf` for `^resource`/`^data`; a root can be misnamed (e.g. one called
  "Athena" that declares only Glue catalog resources) and its README can describe an
  aspirational design that was never built. Scope the IAM grant to what the code
  declares, confirmed against live resource names. Also check for a `for_each`/module
  that inflates one `^resource` block into many live instances (e.g. a "VPC" root
  that is one `aws_vpc_endpoint` block plus a `terraform-aws-modules/vpc/aws` module
  = subnets/NAT/IGW/EIP/route-tables/flow-log role) — the instance count and the
  policy surface follow the live state, not the block count.
- **Scope to exact resource ARNs when the naming namespace is shared.** If the root
  owns only _some_ resources under a name prefix and other, unmanaged resources share
  that prefix, a `<prefix>/*` grant over-reaches (worst case: the apply role could
  delete an unmanaged prod secret/bucket/queue). Enumerate the exact ARNs the root
  declares (list them live and diff against the `.tf`), not the prefix. Shape of the
  problem: a Secrets Manager root declares some secrets under per-environment prefixes,
  but those same prefixes also hold datastore and third-party secrets it does not
  manage — so the grant must enumerate one exact `secret:<name>-*` ARN per declared
  secret (the `-*` matches Secrets Manager's random 6-char suffix), and a
  policy-simulation asserting that an unmanaged same-prefix secret is DENIED is what
  proves no leak. When the root _does_ own a whole dedicated namespace with nothing
  else in it, a `<prefix>/*` grant is fine.
- **Implicit reads with irregular ARN forms + multi-resource auth.** Some services'
  refresh reads authorize against ARN shapes you won't guess, and against _several_
  resource types at once — get either wrong and the first plan `AccessDenied`s.
  - **Identity Store ARNs are irregular.** `identitystore:GetGroupId`/`DescribeGroup`
    authorize against `Identitystore` =
    `arn:aws:identitystore::<account>:identitystore/<id>` (empty region, carries the
    account) OR `Group` = `arn:aws:identitystore:::group/<group-id>` (no region, no
    account, no store-id in the path). Grant both forms; the "standard"
    `arn:aws:identitystore:<region>:<account>:…` shape denies.
  - **Multi-resource actions need every authorizing ARN listed.**
    `sso:ListAccountAssignments` authorizes against Instance / Account / PermissionSet
    _together_ — list all three (`instance/…`, `account/<acct>`, `permissionSet/…`) or
    the refresh may deny depending on which the service evaluates.
  - **`simulate-custom-policy` can give a false verdict** here: a deny-scoping
    assertion ("the _other_ permission set must deny") only holds once the
    co-authorizing ARN (e.g. the `account/<acct>`) is also present in the policy under
    test — omit it and the simulator's implicit-deny can flip. Treat post-apply
    `simulate-principal-policy` against the **live** role as authoritative; use
    `simulate-custom-policy` only as a pre-apply sketch, always with every
    co-authorizing ARN present. Two mechanics: it **rejects
    `--policy-input-list file://…`** ("invalid content") — pass the JSON inline via
    `"$(cat file)"` — and it enforces a per-member length limit, so simulate a trimmed
    slice of the relevant statements for a large policy.
  - **`explicitDeny` vs `implicitDeny` mean different things — assert the one you
    actually expect per role, never "either".** `explicitDeny` means an applicable
    `Deny` statement matched, whether or not an Allow also matched. `implicitDeny`
    means nothing matched at all: no Allow _and_ no Deny. They both block the call
    today, which is why it is tempting to accept either — but they are not
    interchangeable evidence. `implicitDeny` says only "this role was never granted
    the action", which stops being true the moment someone widens an unrelated
    wildcard; `explicitDeny` says "a protection statement is in force here". An
    assertion written to accept either would report a **missing protection policy**
    as a pass.

    So when the same assertion returns `explicitDeny` on the apply role and
    `implicitDeny` on the plan role, do not wave it through as a quirk — that is
    telling you the Deny is present on one role's policy and **absent from the
    other's**. Decide deliberately which roles must carry the Deny (for a trust
    anchor, normally both), assert `explicitDeny` on each of those, and assert
    `implicitDeny` only where you genuinely intend "never granted, no guard needed".

  - **CloudWatch Logs ARN forms split by action.** `logs:DescribeLogGroups` has
    `Resources: null` in the service reference — no resource-level permissions — so it
    MUST be granted on `*`; scoping it to the group ARN denies. But
    `logs:ListTagsForResource` (the separate tag-read a log group makes when the root
    sets `default_tags`, since `DescribeLogGroups` doesn't return tags) authorizes
    against the `log-group` resource
    `arn:aws:logs:<region>:<account>:log-group:<name>` with **NO** trailing `:*` —
    unlike most Logs actions, which do take `:*`. Put them in two statements.
  - **IAM is a global service** — its refresh calls sign against `us-east-1`, not the
    provider region, so a `TF_LOG=trace` grep filtered by the provider region silently
    misses every IAM read.

- **Least-privilege can be _more_ restrictive than the source — decide per resource.**
  A crown-jewel resource (a KMS key over regulated data, a human-access SSO permission
  set) can be migrated **read-only** on the destination CI roles even though the source
  managed it under an admin role: the identity-preserving cutover only needs _reads_ (it
  lands `0/0/0`), so grant reads for the refresh and keep every mutating action off the
  0-approval CI path — future changes become a deliberate admin hand-apply. Prove it with
  a live `simulate-principal-policy` matrix asserting the mutations _deny_ on both roles.
  (Owner's call; louder than the default "match the source's surface".)

  **Read-only by omission vs by added Deny — check whether the dest apply role already
  over-reaches.** For most crown-jewel roots the read-only posture is free: the apply
  role simply never held the root's mutating actions (e.g. a VPC root, where the apply
  role had no `ec2:*`/`logs:*` mutation, so granting only the describe/tag _reads_ makes
  it read-only). But when the migrated root is the **same service** the apply role
  already manages broadly, its resources fall **inside** an existing wildcard mutation
  Allow — so omission is impossible and read-only requires an **explicit added Deny**
  scoped to the exact ARNs. Canonical case: migrating an all-IAM root (the GitHub OIDC
  provider + CI/app roles + a managed boundary policy) into a destination whose apply
  role holds `iam:*` role/policy mutation on `Resource = "*"` — the reads were already
  covered (no new grant), but a `ProtectOidcAnchor`-style Deny (role + managed-policy +
  `*OpenIDConnectProvider` verbs, on the role ARNs + the policy ARN + the provider ARN)
  had to be _added_ so a future 0-approval CI apply can't mutate them. Two tells you're
  in this case: (a) the dest apply role's grant for this service is `Resource = "*"`,
  and (b) the migrated resource is the dest CI's OWN trust anchor (the shared OIDC
  provider it federates through), which must be CI-immutable regardless. Scope the Deny
  to exact ARNs (role-ARN scoping also covers each role's inline policies / attachments
  / permissions boundary, which all authorize against the role ARN), assert per-ARN in
  the sim matrix that an _unrelated_ same-type resource still mutates (the Deny didn't
  over-reach), and confirm any pre-existing self-protection Deny is untouched.

  **A third read-only variant — by construction — appears for cross-cloud roots
  (below).** When the destination CI identity for the root is a _brand-new_ service
  account you mint in the bootstrap, read-only is trivial: grant it only viewer/read
  roles and it never holds a mutating one — no omission to arrange, no Deny to add.
  Prove it the same way (a refresh-reads-allow / mutations-absent matrix).

- **Cross-cloud roots (a non-AWS provider) — the state move is unchanged; only the CI
  auth is new.** A root whose provider is not the destination CI's native cloud (e.g. a
  `hashicorp/google` root migrating into an AWS-OIDC Terraform CI) still keeps its state
  in the same shared S3 backend, so the `copy-object` re-key, the snapshot/verify, and
  the zero-diff gate are byte-for-byte identical to an AWS root. What changes:
  - **CI must authenticate to the _other_ cloud to refresh.** For GCP that's a Workload
    Identity Federation service account impersonated via the **same GitHub OIDC token**
    the AWS credentials step already mints — added as a **conditional** auth step in the
    workflow gated on the root (`if: matrix.root == 'infra/<root>'`), with the existing
    AWS creds step **kept** (still needed for the S3 state backend + DynamoDB lock). The
    dest plan/apply job then authenticates to _both_ clouds.
  - **If the root declares ZERO resources in the CI's native cloud, its bootstrap needs
    NO native-cloud policy change** — the state grant already covers the new key, there's
    nothing to widen and nothing an errant apply could mutate natively (no added Deny
    either). The cleanest Gate 1 of all.
  - **Reuse an existing shared federation provider that already accepts the destination
    repo** rather than adding or mutating one — that avoids a self-referential change to
    the very root you are moving. Read its condition first. A GCP `attribute_condition` is
    CEL over the **qualified** claim namespace, so an admitting condition looks like
    `assertion.repository_owner_id == '<numeric-owner-id>'` (or `attribute.<mapped-name>`
    for claims routed through `attribute_mapping`) — a bare `repository_owner` is not
    valid CEL and will not evaluate. Prefer the **immutable numeric** owner/repository ID
    claims over the name-based ones: GitHub recycles org and repo names, so a name-based
    condition can silently start admitting a different repository.
  - **Local Gate-2 needs that cloud's local creds**, distinct from its CLI login — GCP's
    provider reads Application Default Credentials, so a local `terraform plan` needs an
    interactive `gcloud auth application-default login` (the CLI `gcloud auth login`
    alone drives `gcloud` describes but not the provider). Predefined viewer roles often
    cover the whole refresh first try (GCP: `iam.securityReviewer` +
    `iam.workloadIdentityPoolViewer`); if a refresh call `PERMISSION_DENIED`s, add the
    one missing viewer role — the same implicit-read iteration as AWS, in the other
    cloud's vocabulary.
- **Kubernetes-provider roots (`kubernetes`/`helm` via `config_path`) — auth is in TWO
  places, and only one of them is IAM.** A root whose providers talk to a cluster needs
  (a) a **kubeconfig built in CI** and (b) **in-cluster authorization**, and conflating
  them wastes a bootstrap round-trip:
  - **The IAM side is nearly nothing** — just `eks:DescribeCluster`, which
    `aws eks update-kubeconfig` calls to resolve the endpoint + CA. Resist granting
    `eks:AccessKubernetesApi`: it governs _console_ access, while kubectl/client-go auth
    is settled by the access entry with **no IAM action on the caller**. A zero-diff plan
    proves this — if the refresh reads cluster objects with it absent, it was never
    needed.
  - **The authorization side is an ACCESS ENTRY, not IAM and usually not `aws-auth`.**
    Prefer `aws_eks_access_entry` + scoped access-policy associations: out-of-band entries
    are invisible to an EKS module's `access_entries` map, so they **do not drift** the
    cluster root — whereas that root typically _fully manages_ the `aws-auth` ConfigMap,
    so an out-of-band ConfigMap edit gets reverted.
  - **Scope the cluster policy by what the refresh actually reads, and never take the
    admin one.** `AmazonEKSAdminViewPolicy` at cluster scope is `get/list/watch` on
    `*`/`*` — i.e. **every Secret in the cluster**, including other teams' production
    secrets, for a role any PR can assume. The working combination is
    `AmazonEKSViewPolicy` at **cluster** scope (covers cluster-scoped objects like
    `namespaces`; no secret read) plus `AmazonEKSSecretReaderPolicy` scoped to **only the
    namespaces the root's resources live in**. `helm_release`'s refresh needs exactly one
    secret read — Helm's release Secret in its own namespace — so a namespace-scoped
    SecretReader is sufficient. Never `ClusterAdmin`; the plan role is never
    `system:masters`.
  - **Add the `update-kubeconfig` step gated on the root and AFTER the credentials step**
    (the kubeconfig authenticates as the assumed role), in **both** the plan and apply
    jobs.
  - ⚠ **Local Gate-2 on a Kubernetes root is worthless against the default context.** A
    dev laptop's default kubeconfig usually has some unrelated current-context (a local
    single-node cluster, say); a default-context plan points the providers at the _wrong
    cluster_ and reports every resource as **to-be-created** — which reads as
    catastrophic drift and is pure artifact. Use an isolated kubeconfig
    (`aws eks update-kubeconfig … --kubeconfig <scratch>`) and pass the root's kubeconfig
    variable. **If the root hardcodes the default kubeconfig location with no variable**,
    temporarily repoint that file (back it up) or switch the current-context — and put
    that in the manifest so the next operator doesn't rediscover it.
  - **Migrate the small cluster root FIRST as a canary** when two roots share this wiring:
    it exercises the identical kubeconfig + access-entry design against a handful of
    resources instead of hundreds, so a scoping mistake surfaces cheaply and the large
    root reuses a proven configuration.
  - **Post-apply, verify the workload didn't bounce**, not just that the plan was
    `0/0/0`: helm **revision unchanged**, **pod uptime** older than the cutover, and for
    any stateful backing store (e.g. DynamoDB tables holding cluster identity / audit
    logs) that `CreationDateTime` + resource **ID are preserved** — a replace resets both.
- **Read prepared-but-uncommitted work against what actually merged.** When a dest branch
  is staged during design and only pushed after a later gate, review findings that changed
  the design in the _interim_ leave the prepared files stale. Comments are the usual
  casualty and the dangerous one when they describe a **security boundary** (e.g. workflow
  comments still naming an admin-tier cluster policy that review had already replaced with
  a scoped pair). Re-read the prepared diff against the merged bootstrap before committing,
  not just against your memory of the plan.
- **Unpinned registry modules + per-root provider locks.** A root whose `main.tf` uses a
  registry module with **no `version` constraint** re-resolves to _latest_ on every `init`,
  and the state doesn't record which version built it — so a dest `init` can silently pull
  a newer module and the dest plan diffs even though the state is byte-identical. Note the
  dependency lock file does **not** help here — it records provider selections only, never
  remote module versions.

  Resolve the version the source actually built with (`terraform init`, then read
  `.terraform/modules/modules.json`) and **pin it in the migrated `.tf` for the cutover**.
  Running the source Gate-2 plan and the dest plan in the same window so both resolve the
  same `latest` makes the zero-diff gate pass, but it only proves the two resolutions
  matched _at that moment_ — the destination is left re-resolving on every future `init`,
  so a later routine apply can silently become an unrelated module upgrade with nobody
  reviewing it. Pin for the move; propose the upgrade as its own reviewed change
  afterwards. (This is the one place the migrated `.tf` is deliberately not byte-identical
  to the source — note it in the manifest.)

  Likewise keep **each root's OWN `.terraform.lock.hcl`**:
  roots can pin different provider majors (a VPC root on aws `6.x` while others are `5.x`);
  the locks are independent, so a `6.x` root is inert to the rest — but a dest that reused
  another root's lock would load a different provider and diff. Watch the first plan of a
  new-major root for `5.x`→`6.x` behavior drift.

- **New repos get immutable OIDC subjects** (numeric org/repo IDs), not `owner/repo`.
  Name-based AWS trust fails `AssumeRole` even after the policy is correct. Map each
  trusted repo to its real subject prefix in `bootstrap/`.
- **Plan role stays non-mutating and never `system:masters`** (for cluster roots).
- **Split trust:** plan trust may list both repos (read-only, harmless); apply trust is a
  **single writer at every instant** — a swap, never a widen.
- **Fence via an Actions-level disable** (disable the workflow) rather than a code PR when
  the source repo has a heavy promotion / second-reviewer wall — it needs no merge and
  routes around that wall. Re-enabling the source path for a moved root is the split-brain
  trap; never do it while its stale `.tf` still carries the old backend key.
- **Identity-preserving only.** No `terraform_remote_state`; runtime refers to infra by
  hardcoded ARN/name, so a whole-state re-key keeps prod up. Never let a "move" become a
  recreate.
- **Worktree discipline:** fresh worktree per repo; never branch/switch a `main` checkout;
  sign commits; annotated tags; merge commits.
- **Managed policy vs inline at the inline-policy size ceiling.** The dest apply role
  aggregates every migrated root's grant inline and eventually hits AWS's **10,240-character
  inline role-policy limit** (characters excluding whitespace, not bytes — so minifying the
  JSON buys nothing, but reformatting it costs nothing either). Expect a mid-sized root to
  be the tipping point, and expect the apply role to hit it well before the plan role, since
  it carries the mutating statements too. Watch for it: the failure arrives as a
  `LimitExceeded` on apply, not as a review comment. When appending a root's grant would
  exceed the ceiling, put the grant in a **customer-managed policy** (6,144 characters each;
  a role attaches many) and attach it to _both_ roles instead of inline — this also de-dups
  a read-only grant that is identical on plan and apply. Re-plan then shows the managed
  policy + 2 attachments added and the plan-role inline reverting. Use managed policies from
  the start for the remaining large roots rather than migrating to them under duress.
- **Enumerate tag-reads per service namespace, not once.** One root can hold resources
  from several services that each make a _separate_ refresh tag-read — a database root
  with CloudWatch **alarms** (`cloudwatch:ListTagsForResource`) _and_ CloudWatch **Logs**
  log groups (`logs:ListTagsForResource`) needs both; they are different services, so
  granting the first is not the second. A green **local admin plan masks it** (admin reads
  everything) — only the **plan-role CI plan** surfaces the missing CI grant.
- **S3 bucket-config reads: scope to bucket ARNs; the object/key exposure is in the ACTION
  verb, not the resource.** For an S3 root (buckets + versioning/SSE/policy/lifecycle/PAB/
  CORS/logging/tagging), the whole refresh authorizes against the **bucket** ARN
  `arn:aws:s3:::<name>` — there is NO account-level call (`s3:ListAllMyBuckets`,
  `s3:GetAccountPublicAccessBlock` both have `resources=[]`) in a plain bucket refresh, so
  a bucket-ARN-scoped grant is complete; you never need a `*`-resource s3 statement. Two
  things the trace teaches that CRUD/naming won't:
  - **aws provider v6 reads bucket tags via the S3 _Control_ endpoint** —
    `S3 Control/ListTagsForResource` → IAM action `s3:ListTagsForResource` (resource type
    `bucket`), a per-bucket tag read distinct from `s3:GetBucketTagging`; grant it (covered
    by `s3:List*`, or enumerate it).
  - **`s3:Get*` on a bucket ARN grants bucket-CONFIG reads but CANNOT reach objects** —
    object actions (`GetObject`/`GetObjectVersion`/…) authorize against
    `arn:...:<bucket>/*`, which a bucket-ARN grant never lists. So on a bucket holding
    regulated or sensitive data the config is readable while object CONTENTS stay
    unreadable, _by ARN construction_, with no explicit object-content Deny needed (unlike
    a source role that grants `s3:Get*` on `*` and must Deny `s3:GetObject*`).

  **Prefer enumerating `List` (`s3:ListBucket` for HeadBucket + `s3:ListTagsForResource`)
  over `s3:List*`**: the wildcard also grants `ListBucketVersions` /
  `ListBucketMultipartUploads`, which the refresh never calls and which let CI enumerate
  object **key names / versions** on the state and data buckets — and key paths can
  themselves be identifying. Keep `s3:Get*` a wildcard (all bucket-config metadata, no
  key/object exposure, robust to a provider bump adding a new `GetBucket*` subresource);
  tightening the sensitive dimension (List) is what pays off. SSE that references a CMK
  needs **no** KMS grant — `GetEncryptionConfiguration` returns the key ARN without calling
  KMS.

- **The backend-bucket root is self-referential — the `copy-object` re-key writes INTO the
  bucket the root manages.** If an S3 root owns the Terraform backend bucket, that bucket
  holds _every_ migrated root's state (and its own). The state move is still safe — writing
  a new state key is a data-plane object op, independent of the bucket's
  `aws_s3_bucket`/versioning/policy/lifecycle _resource_ definition — but it makes the
  zero-diff gate the most load-bearing of the whole migration: a dest plan that wanted to
  change the backend bucket would put the shared state store at risk. Verify Gate 2 AND the
  dest plan both show the backend bucket refreshing to `0/0/0`, and keep the root
  **read-only on CI** so a future 0-approval apply can never mutate the backend bucket
  (bucket changes = deliberate admin hand-apply). A **module-based root** — one where a
  handful of module instantiations expand into a much larger instance count — must carry the
  shared module directory alongside it (`infra/modules/<name>`); ensure CI root-discovery
  excludes `infra/modules/*`, which it does naturally if discovery keys on `provider.tf`,
  since a module has none.
- **Plaintext-state roots make the step-5 discipline non-negotiable.** A database root's
  state holds `aws_secretsmanager_secret_version` values and `random_password` results in
  cleartext — Terraform state is not encrypted at the document level, so "sensitive" only
  suppresses CLI _display_, never storage. Never `terraform show` / `state pull` to disk,
  and never a `jq` filter that emits `.instances[].attributes.*`. Use the streamed,
  explicitly-projected read from step 5 — it is the default everywhere precisely so that
  arriving at a secret-bearing root requires no change in habit. Verify the
  `random_password` resources did **not** regenerate by
  comparing a **sha256 of the sorted `<name>=<result>` lines** pre- vs post-apply (hash
  only — never print the values); a changed hash = a data incident. Delete any trace file
  that captured plan-time secret reads or SSO tokens when done.
- **`removed{}` reconcile of a double-owned resource, folded into the cutover.** When two
  repos' Terraform both declare the same live resource, the loser relinquishes ownership
  _inside_ the move: omit the resource block(s) + output(s) and add
  `removed { from = …; lifecycle { destroy = false } }` so the first dest apply **forgets**
  it from state without deleting the live resource. Dest plan reads `0/0/0` **+ N to
  forget**; post-apply verify the live resource still exists (`describe-*` →
  `DeletedDate=null`, owner tag = the winner) and lineage is unchanged. Include the
  forgotten ARNs in the read grant **defensively** (a refresh-before-forget must not
  `AccessDeny`), then a follow-up PR drops the now-inert `removed{}` blocks (a no-op once
  forgotten) and trims those ARNs from the grant.

## Fence strategy (whole-migration decision)

Decide once with the owner:

- **Global disable (fewer reviewers):** keep the source apply workflow disabled for the
  _entire_ migration → every remaining root is already fenced, so each cutover skips its
  own fence PR. Bulk-retire all moved `.tf` in one PR at the end. Cost: source can apply no
  infra until the migration ends, and moved roots get no drift monitoring meanwhile
  (consider a minimal drift cron in the destination).
- **Per-root code fences (apply stays available):** re-enable source apply and fence each
  root with its own scoped code PR. More reviewer overhead; restores source apply sooner.

## Rollback

Only possible if Gate 4 passed and the manifest captured version IDs.

Rollback re-arms a writer against a key the destination now owns, which is the same
split-brain hazard as the forward move — so it gets the same discipline, step for step, not
just a "fence the destination first" gesture:

1. **Fence** the destination apply path.
2. **Drain** in-flight destination runs and **confirm the destination key's lock is
   released** — a rollback that copies over a key mid-apply is the worst outcome available.
3. **Snapshot** the destination as it stands now (`head-object` → `VersionId`/ETag; streamed
   `lineage`/`serial`/count per step 5). You are about to overwrite it and will want the
   ability to undo the undo.
4. **Select the exact version to restore.** Normally the **latest destination version**,
   never the stale source object — the destination has applied since the cutover and its
   state has moved on. Name the version ID explicitly from the manifest; do not rely on
   "current".
5. **Copy** it into place, then **verify** lineage/serial/count against what you expect.
6. **Re-enable exactly one writer.**

Note that step 5 is the one place `--if-none-match '*'` is wrong — you are deliberately
overwriting an existing key. That is precisely why steps 1–3 have to be real: the guard
that protects the forward copy is unavailable here, so draining and snapshotting are the
only things standing between a rollback and a lost state lineage.

**Inventory every write path before declaring the destination fenced.** Disabling the one
obvious apply workflow is not enough if a drift-detection cron, a scheduled plan-and-apply,
a reusable workflow called from elsewhere, or an operator's local credentials can also write
that key. Enumerate them; fence all of them.
