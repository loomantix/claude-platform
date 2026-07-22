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
   `lineage` + `serial` + instance count (`state pull | jq`); config-dir +
   `.terraform.lock.hcl` hashes. Record every value in the manifest — the version
   IDs are the _only_ rollback path.
6. **Copy the exact version** (admin, after go): `copy-object` with
   `?versionId=<V>` to the destination key → verify dest lineage/serial/count/ETag
   EQUAL the recorded values. Any mismatch = STOP.
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
  grep the signed requests for the action names. Three classes bite repeatedly:

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

**Gate 3 — destination key absent** (`head-object <destkey>` → 404).

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
  declares (list them live and diff against the `.tf`), not the prefix. Example: a
  Secrets Manager root owning 14 named secrets under `dev/`, `beta/`, `staging/`,
  `prod/` prefixes that also hold DB/redis/other secrets it does not manage — the
  grant is 14 exact `secret:<name>-*` ARNs (the `-*` matches Secrets Manager's random
  6-char suffix), and a policy-simulation asserting an unmanaged same-prefix secret is
  DENIED proves no leak. When the root _does_ own a whole dedicated namespace with
  nothing else in it, a `<prefix>/*` grant is fine.
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
  - **`explicitDeny` vs `implicitDeny` will differ per role for the SAME assertion, and
    that is not a gap.** A Deny only shows as _explicit_ on a role that also holds an
    Allow for the action (the Deny has something to override); on a role with no such
    Allow the same verb comes back _implicitDeny_. So an escalation check can read
    `explicitDeny` on the apply role and `implicitDeny` on the plan role. **Both
    block** — write the assertion to accept either, or a correct policy reads as a
    false finding.
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
    repo** (read its condition — e.g. a GitHub WIF provider whose `attribute_condition`
    is `repository_owner == '<org>' && repository != '<source>'` already admits any other
    org repo) rather than adding or mutating one. That avoids a self-referential change
    to the very root you are moving and keeps the migrated `.tf` byte-identical.
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
    dev laptop's `~/.kube/config` current-context is often something like `minikube`; a
    default-context plan points the providers at the _wrong cluster_ and reports every
    resource as **to-be-created** — which reads as catastrophic drift and is pure
    artifact. Use an isolated kubeconfig
    (`aws eks update-kubeconfig … --kubeconfig <scratch>`) and pass the root's kubeconfig
    variable. **If the root hardcodes `~/.kube/config` with no variable**, temporarily
    repoint the real file (back it up) or switch the current-context — and put that in
    the manifest so the next operator doesn't rediscover it.
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
  a newer module and the dest plan diffs even though the state is byte-identical. Resolve
  the real version with `terraform init` + read `.terraform/modules/modules.json`, and run
  the source Gate-2 plan and the dest plan in the **same window** so both resolve the same
  latest — a `0/0/0` dest plan then confirms they matched (no pin needed; pin the module
  version only if they diverge). Likewise keep **each root's OWN `.terraform.lock.hcl`**:
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
- **Managed policy vs inline at the 10240-byte limit.** The dest apply role aggregates
  every migrated root's grant inline and eventually hits AWS's **10240-byte inline
  role-policy limit**. In one migration the database root was the tipping point — the plan
  role squeaked in at ~10117 B, the apply role would have been ~13315 B. When appending a
  root's grant would exceed it, put the grant in a **customer-managed policy** (6144 B
  each; a role attaches many) and attach it to _both_ roles instead of inline — this also
  de-dups a read-only grant that is identical on plan and apply. Re-plan then shows the
  managed policy + 2 attachments added and the plan-role inline reverting. Use managed
  policies from the start for the remaining large roots.
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
  (bucket changes = deliberate admin hand-apply). A module-based root (e.g. 24
  `../modules/s3` instantiations → 138 instances) must carry the shared module dir alongside
  it (`infra/modules/s3`); ensure CI root-discovery excludes `infra/modules/*` — a module
  has no `provider.tf`, so it is never mistaken for a root.
- **Plaintext-state roots: read state ONLY via streamed `jq`.** A database root's state
  holds `aws_secretsmanager_secret_version` values and `random_password` results in
  cleartext. Never `terraform show` / `state pull` to disk, and never a `jq` filter that
  emits `.instances[].attributes.*`; stream (`aws s3 cp <key> - | jq …`) and extract only
  lineage/serial/counts. Verify the `random_password` resources did **not** regenerate by
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

Only possible if the manifest captured version IDs. Rollback re-arms a writer against a key
the destination now owns, so **fence the destination first** (mirror the forward discipline
exactly), restore from the **latest destination version** (never the stale source object) or
re-point old code at the destination key, then re-enable exactly one writer.
