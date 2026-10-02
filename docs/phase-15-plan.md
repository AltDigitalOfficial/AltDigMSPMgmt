# Phase 15 — Reconciliation and plan (prompt 15.0)

> **Status at 2026-10-02 (end of the 15.0–15.4 working session).** The body below
> is the 2026-09-25 plan, kept as written. What has happened since:
>
> - **Done:** B-014 (renumbered to D-023/D-024), PCI halt at vesting (D-025),
>   RCP federation fix (D-003; SAML and OIDC both proven), account settings
>   (B-139), 15.2 exemption rewrite (B-135, B-136, D-026), D-012 closed: StackSets
>   operate from Infra Tooling, **F-2 withdrawn** (the premise was wrong),
>   developer boundary StackSet (B-143), 15.4a/b: v2 SCPs on every OU, and
>   04–07 and the RCP on Members, B-138 IdP deny, vesting quota guard (B-140), baseline
>   settle-and-retry (B-062), publish committed-only guard (B-144).
> - **Arc8** dev and test are vested and verified (10 stack sets CURRENT,
>   account settings in place). Prod waits on the account-quota increase (B-140).
> - **Before Arc8's first deploy:** the deploy role pinned to the exact OIDC claim
>   (B-142), after Monday's kickoff, plus the tagging requirement briefing (SCP 04).
> - **Open decisions:** harness on Arc8 dev (`--no-kms-fixture`?), IAM.1
>   suppression for the boundary (B-147), readout access (B-146), router
>   evidence events (B-148), publish-telemetry (B-145), canary-smoke StackSet.
> - **Corrections to this plan:** the GitHub org is `AltDigitalOfficial`, not
>   `altdigital`. Five baseline StackSets already targeted Members (§1 15.3 item 1
>   read instance lists, not targets). StackSet ownership is not a constraint (§6.2).

Written 2026-09-25 from a read-only session. No AWS calls, no other repository
changes. Sources: `CLAUDE.md`, `design/open-items.md`, design docs 02, 03, 04,
08, 10, 11, 12, 13, 14, 15, every file named in prompts 15.1–15.9, the registers
`docs/build-backlog.md` and `docs/deviations.md`, and the estate dump
`estate/estate.20260925.json` (see §0.4 for what that file actually is).

Citations are `file:line`. "Proven" means the repository or the dump shows it;
"unproven" means it is inferred and must be tested before it is relied on.

---

## 0. Corrections that apply to every prompt in the phase

Fix these in `design/phase-15-prompts.md` (or in the standing-rules paste)
before 15.1 runs. Each one would otherwise produce a wrong artefact in every
session.

| # | Prompt text | Repository | Effect if not fixed |
|---|---|---|---|
| 0.1 | "`docs/open-items.md`" (standing rule 2, 15.0, 15.11) | Does not exist. Open items: `design/open-items.md` (B1–B16, X, N, D, R, V). Backlog: `docs/build-backlog.md`. Deviations: `docs/deviations.md`. CLAUDE.md: build work does not edit `design/`. | A new fourth register, or edits to the design package. |
| 0.2 | "backlog items from B-129, deviations from D-019" | B-129…B-134 and D-019…D-021 exist. **Next free: B-135, D-025** (D-022 taken 2026-09-27; D-023/D-024 by B-014). | Duplicate IDs. B-014 already records the two cases where this happened (D-009, D-010). |
| 0.3 | "B10" and "B-010" used side by side | `B10` = open item, service account identities (`design/open-items.md:21`). `B-010` = backlog item, `locked` egress profile (`build-backlog.md:779`). | Different items that are easy to confuse. The expected-closure list in 15.0 uses both. |
| 0.4 | "review of `estate_20260925.json` (dump generated 2026-09-22)" | `estate/estate.20260925.json` is byte-identical to the tracked `estate/estate.json` (`generated_at` 2026-09-22T21:42Z). It is untracked and **not gitignored**. | The filename implies a newer dump than it is. It is also one `git add` away from being committed. |
| 0.5 | "Do not commit the dump output" (15.1) | `estate/estate.json` is tracked, and `estate/README.md` treats it as the committed artefact. | Either the practice changes (untrack it, add `estate/*.json` to `.gitignore`) or the instruction does. **Founder decision F-1.** |
| 0.6 | Nothing on the layer tag | CLAUDE.md hard rule: everything the platform builds carries `platform-layer: infrastructure` (B-127). | Every new resource in 15.3–15.9 needs the tag: OIDC provider, roles, recorders, CMKs, FinOps buckets, Athena workgroup, budgets, anomaly monitors. The existing `FlowLogBucket` lacks it (`security/10-log-archive.yaml:674-677`). |
| 0.7 | Trust and delegation policies (15.3 steps 1–4, 15.5 step 1) | CLAUDE.md: nothing hardcodes an account id or OU id. | Principals must be built from SSM `/platform/` values or template parameters fed from `config/platform.env`, never literals. |
| 0.8 | Cost estimates in 15.6, 15.8, 15.9 | Org instruction: no financial data in conversations; 15.9 step 8 already says "do not write spend figures into the repository". | Apply the same rule to 15.6/15.8 estimates. Report unit rates and ratios; keep account spend out of the repo and out of session reports. |
| 0.9 | 15.2 cites "Every SCP exempts `role/Platform*`" | SCP 00 exempts nothing. SCP 03 `DenyQuorumExecutorCreationEvenByOperators` exempts **only** `stacksets-exec-*`, on purpose (B-125). | The rewrite must preserve that statement's narrower list. |

**Also stale, not blocking.** CLAUDE.md says design 05 is absent; `design/05-resilience.md` exists. The SCP README says "5 policies per OU" (`policies/scp/README.md:75`), but the dump shows **8 SCPs attached to Sandbox** (01–07 plus FullAWSAccess). Either the quota was raised or the README is wrong. 15.2 must read the live quota from Service Quotas before planning attachments (§2.4).

---

## 1. Per prompt: files, design dependencies, conflicts

### 15.1 — Extend the estate dump

**Touches.** `scripts/lib/estate_dump.py`, `scripts/dump-estate.sh`, `estate/README.md` (to document the schema). Possibly `.gitignore` (F-1).

**Design.** None directly. The redaction guarantees come from CLAUDE.md and `estate/README.md:25-75`.

**Conflicts and findings.**
- **Root attachments: the review is correct.** `collect_organization` records only `policy_types` for the root (`estate_dump.py:123-125`). `list_policies_for_target` runs for OUs only (`:127-153`), and for SCP and RCP only (`:137-145`). Account-level attachments and accounts sitting directly under root are also not captured.
- **The verify step already exists.** `verify_no_leak` (`estate_dump.py:724-781`) sweeps the whole serialised document against `ALLOWED_EMAIL` (`:707-713`). Any finding exits with status 2 and nothing is written (`:876-881`; `dump-estate.sh:75-76`). New fields are covered automatically. Extend the allow-list only if a new field legitimately needs it. Do not add a second verifier.
- **Config recording mode is partly captured already.** `config_recorders` stores the raw API object, which includes `recordingMode`/`recordingGroup` (`:518-521`). The same file claims "nothing dumps a raw API response" (`:15`). That claim is false for recorders, recorder status and backup plans (`:507-509`). Fix the claim or the storage while the file is open.
- **Budgets (item 6).** Names, types, time units and notification *thresholds as percentages* are safe. `BudgetLimit` and `CalculatedSpend` are financial data and must be dropped at collection, not redacted after.
- **Identity Center (item 7).** Permission-set names and account assignments by *group name* are fine. Principal IDs are not personal data, but they resolve to people: drop them.

**Stop conditions.** None; read-only against AWS.

---

### 15.2 — Role namespaces and the SCP exemption rewrite (Sandbox only)

**Touches.** `policies/scp/01`–`07`, `policies/rcp/00`, `policies/scp/attachments.tsv`, `policies/scp/README.md`, `change/00-appdeployer.yaml`, `baseline/80-developer-boundary.yaml`, `scripts/test-guardrails.sh`, `scripts/validate-policies.sh`, `scripts/deploy-policies.sh`. **Missing from the prompt's list:** `change/src/reconcile.py`, `change/10-reconciliation.yaml` and `identity/10-developer-access.yaml` (see below).

**Design.**
- Design 08:19-22, 46 and 11:93: the role is named `AppDeployer` and is "denied from touching platform-managed resources (SCP)".
- Design 04:56-57 and 11:31: exemptions are for "the `BaselineDeployer` and platform automation roles" and "platform roles only".
- Design 11:39-42: no boundary, no role.
- Design 11 says nothing about role paths.

**Conflicts and findings.**

1. **15.2 cannot be Sandbox-only for SCPs 01–03 as written.** A policy is one object. The dump shows the *same* policy ids (`p-nb7wqdcc`, `p-im36b0so`, `p-7bm8ptth`) attached to Sandbox, Members, Security and Infrastructure. An in-place `update-policy` on 01–03 changes all four OUs at once. That breaks standing rule 3 (Sandbox first) and hits rule 5 (Members/Security/Infra), and it is the most likely lockout in the phase. **Required approach:**
   - Create the rewritten 01–03 as *new* policy objects, for example with a `-v2` suffix in the name, and deploy only those to Sandbox.
   - Then swap the policies per OU in 15.4: attach the new one first, then detach the old one. Never detach before attaching.
   - `deploy-policies.sh` derives the policy name from the file. It needs versioned names and a swap mode.
   - 04–07 and the RCP are attached to Sandbox only, so updating them in place *is* Sandbox-only.

2. **Finding A (app deployer exempt from everything) is confirmed, and matches the design's intent.**
   - The role is `PlatformAppDeployer-${ApplicationCode}` (`change/00-appdeployer.yaml:283`). The comment at `:280-282` shows the `Platform` prefix was chosen *for tamper protection*. The exemption came along as a side effect.
   - Its boundary already denies it any IAM or EC2 creation (`:171-212`). The SCP exemption still covers 04 (tags), 06 (region) and 07.
   - `TheApplicationsOwnCompute` (`:365-374`) grants `ecs:*`, `lambda:*`, `states:*`, `apigateway:*`, `elasticloadbalancing:*` and `application-autoscaling:*` on `*`, with no `aws:RequestedRegion` condition.

3. **Finding B (pathed roles) is confirmed from policy text, and the real exposure is wider than the review describes.** All of these need harness cases in step 1:
   - **`stacksets-exec-*` squatting.** Every SCP 01–07 exempts `stacksets-exec-*`. Nothing denies creating a role with that name: the SCP 03 squatting deny covers `Platform*` only (`03:14-19`). The developer boundary's `NeverThePlatformIdentities` covers only the unpathed name (`baseline/80-developer-boundary.yaml:128-136`). And `DeveloperDev` grants `iam:CreateRole` on `*` (`identity/10-developer-access.yaml:92-125`). So a developer can create a bounded role named `stacksets-exec-x`, which is exempt from **01, 02 and 03** as well as 04–07. What stops it is the boundary's own denies, not the SCPs. Prove it.
   - **`OrganizationAccountAccessRole<suffix>`.** 04–07 exempt `…role/OrganizationAccountAccessRole*` (trailing wildcard). 03 and the boundary protect only the exact name.
   - **The pathed Platform role against 01–03, not only 04–07.** B-001 (`build-backlog.md:919-923`) records that "assumed-role requests present only `arn:aws:sts::*:assumed-role/...`". The assumed-role ARN carries no path. If that observation is right, a role at `/x/PlatformProbe` presents `assumed-role/PlatformProbe/…`. That matches the `arn:aws:sts::*:assumed-role/Platform*/*` exemption in 01–03, while its creation evades 03's `role/Platform*` squatting deny. **Unproven.** AWS documentation says `aws:PrincipalArn` for a role session is the *role* ARN. The harness settles it either way.
   - **Other pathed names.** Paths beyond `/role/` (for example `/x/stacksets-exec-y`) against every pattern.

4. **The prompt's example "response automation needs 02" is not supported.** `PlatformResponseAutomation` holds none of 02's actions; they appear only in its own Deny (`baseline/60-response.yaml:428-433`). The derived per-SCP lists are:

   | SCP | Runtime principals that perform the denied action (cited) | Deployment principals |
   |---|---|---|
   | 01 | PlatformConfigSetupRole (`10-config-recorder.yaml:156-157`), PlatformQuorumExecutor (`quorum/00-executor-role.yaml:213-222`) | stacksets-exec-\*, OAAR today; PlatformDeployer after 15.3 |
   | 02 | PlatformQuorumExecutor (`:189-211`) | as above; no runtime Put\*Policy caller exists |
   | 03 tag-tamper | PlatformSecurityResponder (`containment/src/contain.py:367-372`), PlatformRestoreWorker (`recovery/src/worker.py:765-781`), PlatformInstrumentation (`instrumentation/src/handler.py:784-785`), PlatformExerciseHarness (`containment/src/exercise.py:141-150`) | as above |
   | 03 role-squat / modify | none at runtime | stacksets-exec-\*, OAAR; **PlatformDeployer must be added** or no direct stack can create a Platform\* role after 15.5 |
   | 04 | PlatformExerciseHarness (`exercise.py:141-150`) | as above |
   | 05 | PlatformExerciseHarness (`exercise.py:159-170`), PlatformSecurityResponder `PutRolePolicy` (`contain.py:606,688`) | as above |
   | 06 | none found | as above |
   | 07 | PlatformSecurityResponder `ModifySnapshotAttribute` (`contain.py:396-400`) | IGW/NAT are created by stacksets-exec-\* only (`platform-network` is SERVICE_MANAGED) |

   Add the harness roles `PlatformHarnessProbe`/`GuardrailTestAdmin` (`test-guardrails.sh:70-71`). The harness depends on the probe being exempt. That is B-091's territory, and the probe must now be named explicitly.

5. **Adding PlatformDeployer to 03's squatting exemption must not re-open B-125.** `DenyQuorumExecutorCreationEvenByOperators` must keep exempting `stacksets-exec-*` only. Otherwise the pipeline role becomes the single-operator quorum bypass B-125 closed.

6. **Renaming to `TenantAppDeployer-<app>` breaks change reconciliation silently.** `change/src/reconcile.py:195,215,235` matches the literal `PlatformAppDeployer`. So does the classification table (`build-backlog.md:2688`). A renamed role's deploys would classify as an unaccounted-for credential. Rename and reconciler must change in one commit, with a reconciler unit test for the new name.

7. **The rename replaces a role.** Changing `RoleName` means CloudFormation deletes and recreates the role. That is a rule-5 STOP ("deleting a role"). It is not automated remediation, so there is no hard-rule breach. It also changes the OIDC-trusted role ARN, so the tenant pipeline's configured role ARN has to change at the same moment.

8. **The D- item is misdescribed.** The prompt says the rename "changes a name that design doc 08 uses". It doesn't: design 08 says `AppDeployer`. `PlatformAppDeployer-` was already an **unrecorded** deviation, and `TenantAppDeployer-` is a second one. Record both in one D- item.

9. **Denying pathed `iam:CreateRole`.** Design 11 doesn't need pathed roles. The deny must still exempt service-linked roles (`/aws-service-role/`) and Identity Center's `/aws-reserved/sso.amazonaws.com/` roles, which 15.5's break-glass trust depends on. Prove both in the harness.

10. **Size.** Explicit lists grow 01–03, and 01 is 4,983 of 5,120 bytes (B-001). The B-001 option "drop the `iam::role` variants" is only safe if item 3's harness result establishes which ARN form SCPs actually see. Decide it from that evidence.

11. **This reverses a closed decision.** B-125 kept the broad `Platform*` exemption because it "is load-bearing everywhere else". Removing it reverses B-125's reasoning. That is legitimate, because the finding is new, but it needs a line in B-125 or the new D- item saying so.

**Stop conditions.**
- The role rename (a delete).
- Any attachment outside Sandbox.
- Anything that detaches an old 01–03 from Sandbox before the new one is attached.

---

### 15.3 — Platform pipeline and deployer roles

**Touches.**
- New: `.github/workflows/`, `tooling/20-pipeline.yaml`, `baseline/95-platform-deployer.yaml`.
- Existing: `scripts/lib/common.sh`, `scripts/deploy-to-account.sh`, `scripts/deploy-stackset.sh`, `scripts/deploy-policies.sh`, all 13 `scripts/publish-*.sh`, and `policies/scp/03` (§1 15.2 item 5).

**Design.**
- Design 08:106-162: platform pipeline in Platform Tooling, "CodePipeline, and the StackSet administration role".
- Staged rollout Sandbox → non-prod Members → prod Members, with bake periods and `MaxConcurrentCount` (08:127-142).
- Drift detection to Truveon (08:144-147).
- Interim approval is Jamie as sole approver, with a second named approver (open item B5) (08:149-162).
- Design 03:41 and 04:56: role name `BaselineDeployer`, "pipeline only… not assumable by humans".
- Design 03:110-114 and 08:85-102: approvals live in Jira, and quorum actions run only through a pipeline that checks two Jira approvals.

**Conflicts and findings.**
1. **"Infra Tooling is already the StackSets delegated admin" is true but misleading.**
   - Delegation is registered (B-003).
   - But **nothing uses it**. `deploy-stackset.sh` hard-codes `require_account mgmt` (`:82`) and `--call-as SELF` (`:131`).
   - The dump shows **ten** SERVICE_MANAGED StackSets, all owned by the management account: canary-artifacts, canary-smoke, config, event-forwarding, instrumentation, kms-secrets, log-data-protection, network, response, security-responder.
   - **D-012 says four. It is stale by six.**
   - A StackSet cannot change owner (D-012). So the pipeline in Infra Tooling can create *new* StackSets as delegated admin, but it **cannot update any existing baseline StackSet**.
   - Every baseline change in 15.2 and 15.6–15.8 therefore either runs from the management account (a rule-5 STOP every time), or goes through D-012's second close path: retire and replace the StackSets as Tooling-owned during a baseline version bump.

   Phase 15 changes three of the four StackSets D-012 names (config, network, kms-secrets). **This phase is the version bump D-012 was waiting for.** Recommendation: schedule the retire-and-replace inside 15.3 and exercise it against the canary first. D-012 records the orphaned-resource collision (Retain KMS aliases), so migrating the retained resources with `--resources-to-import` is the path to test. Founder decision **F-2**.
2. **Service-managed StackSets never deploy to the management account.** "A PlatformDeployer in every account via StackSets" therefore misses it. That is consistent with 15.3 step 4 (a separate `PlatformMgmtDeployer`), but the prompt should say so.
3. **`PlatformMgmtDeployer` widens D-012 and design 02:30-33** ("the management account holds nothing"). Design 02:32-33 notes Identity Center administration can be delegated. Only account creation, Cost Categories, cost allocation tags and organization-level `EnablePolicyType` genuinely need the management account.
   - Delegate Identity Center to a member account, or record why not.
   - Record the role as a D- item, or extend D-012.
4. **RCP vs OIDC: possible lockout, unproven.**
   - The current RCP denies `sts:AssumeRoleWithWebIdentity` and `sts:AssumeRoleWithSAML` under `StringNotEqualsIfExists aws:PrincipalOrgID` + `BoolIfExists aws:PrincipalIsAWSService=false`. A web-identity or SAML federated principal carries neither key. Both `IfExists` conditions therefore evaluate true, and the deny would apply.
   - If that's right:
     - (a) GitHub OIDC into `PlatformPipelineDeployer` fails once 15.4 puts the RCP on Infrastructure.
     - (b) Every tenant AppDeployer fails once it's on Members.
     - (c) Identity Center sign-in to any account under the RCP fails. That includes Sandbox **today**, and every account after 15.4, the 15.5 break-glass path among them.
   - Nothing in the repository shows either path has been exercised under the RCP. **Test OIDC and SAML into a Sandbox account as the first step of 15.2's harness work, before anything else in the phase depends on either.** If the deny is real, fix it with the AWS data-perimeter pattern: exclude federated principals by `aws:FederatedProvider` or by action.
5. **Role names differ from the design.** `PlatformPipelineDeployer`/`PlatformDeployer`/`PlatformMgmtDeployer` vs `BaselineDeployer` → D- item.
6. **Pipeline differs from the design.** GitHub Actions vs CodePipeline, and the repository on GitHub rather than in Tooling → D- item. The founder decision exists (phase header line 12). The design has not been amended.
7. **The approvals model is not design 08's.**
   - Design 08 reconciles deploys against a Jira change record (08:39). GitHub environment approvals are a second approval system that reconciliation cannot see.
   - Decide:
     - either the workflow files a Jira change record before deploy (the tenant path already does this, B-069/B-077),
     - or record GitHub approvals as the platform's change evidence in a D- item.
   - "Production: 2 reviewers": I believe GitHub environment protection needs only *one* approval from the reviewer list. Two-approval control would need branch protection review counts or a custom deployment protection rule. **Unverified — check before relying on it.**
   - Design 08's interim state is a sole approver (B5 is open). Two reviewers requires B5 to be named first.
8. **`PlatformDeployer` scope must exclude quorum actions.** Otherwise the pipeline is a quorum bypass (design 03:110-114). That means `kms:ScheduleKeyDeletion`, vault deletion and Vault Lock changes, and CloudTrail/Config stop/delete.
9. **The `--break-glass` flag name clashes with design 03**, which uses "break-glass" for Entra-independent emergency accounts. Suggest `--use-org-access-role`.
10. **Missing from the prompt:** drift detection (08:144-147), bake periods, `MaxConcurrentCount`, and separately logged merge and deploy evidence. Design 08:140 asks for "automatic rollback on stack failure". That is CloudFormation deployment behaviour, not remediation, so the hard rule is not engaged. State it in the template comments so an auditor does not read it as one.
11. **`OrganizationAccountAccessRole` count.** 51 occurrences across 35 files in `scripts/`, 36 of them actual assumptions (`--role-arn`/`RoleArn=`). One agent counted 54 including `__pycache__`/docs. 51 is right for source.
12. **Only one script uses the shared helper.** Most scripts inline their own `sts assume-role` (e.g. `deploy-to-account.sh:87-93`, `sync-cost-categories.sh:47-51`); only `sync-pagerduty-tenants.sh` uses `in_account()` (`common.sh:614-633`). Changing `common.sh` alone (step 5) changes almost nothing. Every script's assume must go through one helper first.

**Stop conditions.** Delegation policy in management (step 3), `PlatformMgmtDeployer` (step 4), any StackSet retire and replace (F-2).

---

### 15.4 — Promote guardrails, extend the RCP, declarative and tag policies

**Touches.**
- `policies/scp/attachments.tsv`, `policies/rcp/00`, new `policies/declarative/` and `policies/tag/`.
- `scripts/deploy-policies.sh`: it supports only `SERVICE_CONTROL_POLICY`/`RESOURCE_CONTROL_POLICY` (`:71-77`). Add the declarative and tag types, and the swap mode from §1 15.2 item 1.
- `scripts/test-guardrails.sh`.
- `scripts/bootstrap-management-account.sh`: EC2 declarative policy type enablement.

**Design.**
- 04:43-57: control-protection SCP on Members.
- 11:75, 81-82: region restriction is the priority bypass control.
- 10:122-132: required tags Owner, Application, DataClassification; CostCenter optional.
- 11:39-42: boundary requirement.
- 15:61-68: per-partner/per-client SCPs at OU level, which supports "exceptions per OU".
- 02:114-125: egress profiles.
- The "a conversation" wording is `attachments.tsv` commentary, not design.

**Conflicts and findings.**
1. **Existing bug.** `deploy-policies.sh:248-250` checks existing attachments with `--filter SERVICE_CONTROL_POLICY` only. An already-attached RCP is never detected, so a re-run with `--attach` re-attaches it. I infer this raises `DuplicatePolicyAttachmentException`; I haven't run it. Fix before 15.4 relies on idempotent re-runs.
2. **RCP extension to S3/KMS: known break candidates.**
   - `logs.<region>.amazonaws.com` on KMS keys: `alerting/10-alert-topic.yaml:164-169` (whose comment says Logs does *not* send `aws:SourceOrgID`), `baseline/20-log-data-protection.yaml:125`, `reporting/10-digest-sender.yaml:131`.
   - AWS's published confused-deputy RCP statement allows service principals only with a matching `aws:SourceOrgID`/`SourceAccount`. Test each of the three in Sandbox before promotion.
   - No bucket, key, queue or secret today grants an external principal, and there are no inbound webhooks, Function URLs or CloudFront OAC.
3. **Truveon path: the review is correct.** It is an outbound HTTPS POST from Audit (`evidence/src/sender.py:175-190`), and the RCP does not see it. B-133 records that any future Truveon read of raw logs would be a cross-org principal and so needs an allow-list entry, which the step's empty external allow-list would carry.
4. **Order with 15.7.** An S3/KMS RCP that permits services only under `aws:SourceOrgID` depends on what 15.7 establishes about log delivery. The alert-topic comment at `alerting/10-alert-topic.yaml:129-131` claims `SourceOrgID` is "confirmed for delivery.logs elsewhere in this repository". Flow logs have never delivered, so that confirmation is suspect. **Run 15.7 step 1 (the matrix) before 15.4 step 2.**
5. **Region restriction on every OU.** Cross-check the three approved regions against the dump for Security and Infrastructure. Detective services are enabled in all three (B-013), so nothing outside them should be running.
6. **VPC Block Public Access.**
   - Bidirectional mode breaks NAT egress for controlled, dns-filtered and public-facing. The prompt is right to exclude it.
   - Ingress-only mode keeps NAT egress working, and would affect only public-facing ingress, which isn't built (B-011). Worth recording as an option; it is not needed now.
   - `baseline/40-network.yaml:178-226` creates the IGW and public subnets **for every profile, including `isolated`** (B-047).
7. **IMDSv2 is enforced nowhere today.** No `MetadataOptions`, launch template, guard rule or Config rule sets it. The declarative policy is the first control.
8. **Tag policy.**
   - "The four values in 04-require-tags.json" means the four DataClassification *values*, not four tags.
   - Include `platform-layer` (SCP 04 `PlatformLayerMustBePresentAndOneOfTwo`, B-127). Decide CostCenter (design 10: optional).
9. **Triggers fire.** Widening SCP 04 to Members is the trigger for **B-129** (layer-tag provenance check). The first AppDeployer deploy after that is **B-128**'s trigger. 15.4 must say so in both items.
10. **The per-target SCP quota** decides whether Members can take 04–07 alongside 01–03 and FullAWSAccess (8), and whether the parallel v2 swap needs a spare slot. Read the live quota first (§0).
11. **Federation under the RCP on every OU.** §1 15.3 item 4. A lockout-class risk.

**Stop conditions.** Everything outside Sandbox (the prompt already says so), plus root-level `EnablePolicyType` for declarative policies, which is a management-account change.

---

### 15.5 — Break-glass, root credentials, JIT

**Touches.**
- `identity/` (new break-glass permission set; PlatformAdmin and SecurityResponder permission sets, neither of which exists).
- `baseline/70-event-forwarding.yaml`, `alerting/20-router.yaml`, `design/06a` (see F-4).
- `scripts/bootstrap-management-account.sh`, `scripts/test-alert-path.sh`, `policies/guard/baseline.guard`.
- A new runbook under `docs/runbooks/`.
- A new repeatable mechanism for the `OrganizationAccountAccessRole` trust (not StackSet-managed).

**Design.**
- 03:24-31: JIT with reason, time box, expiry and evidence of requester, reason, duration, target and **approval**.
- 03:48-57: break-glass "must not depend on Entra ID": emergency accounts outside the federation path, hardware MFA, split credentials, "any authentication attempt pages immediately, successful or not", scheduled test (V5).
- 03:61-68 and `design/open-items.md:67` (D1): Entra ID P2 deferred.
- 15:113: member roots "secured with hardware MFA".

**Conflicts.** Several of these are blocking.
1. **Break-glass through Identity Center contradicts design 03:50-51.** `PlatformBreakGlass` is a permission set, so it sits on the Entra federation path. The design requires break-glass to be *independent* of it. The prompt makes the Entra-independent path (management root) the secondary plan. This is a D- item at minimum. Founder decision **F-3**.
2. **Entra licensing (blocking).** Step 4 uses Entra PIM for Groups, which needs Entra ID P2 or ID Governance. D1 defers P2. Conditional Access authentication strengths need at least P1. Fund it, or drop PIM and build JIT another way. Founder decision **F-5**.
3. **Hardware MFA vs D-006.** Steps 1 and 3 call for hardware MFA and a second hardware device. D-006 (`deviations.md:518-576`) records "Hardware tokens are not being ordered" (2026-09-16), with the target of factors on two other partners' devices. The prompt contradicts a recorded founder decision rather than closing it. Founder decision **F-3**.
4. **The guard rule blocks a break-glass permission set with `AdministratorAccess`.** `SSO_PERMISSION_SET_NO_ADMINISTRATOR_ACCESS` (`baseline.guard:324-338`) blocks it. A break-glass set needs either a scoped policy or a recorded guard exception.
5. **Paging severity breaks the emitter rule.** "Pages at the highest severity" in step 2 would have the emitter decide urgency, against the CLAUDE.md hard rule and W-010. The router decides from 06a `severity_by_tier` plus the account's tier, so a Sandbox or dev assumption would not page, while design 03 requires paging for *every* attempt. The compliant route is a 06a `metric: event` entry with PagerDuty at every tier. 06a is authoritative and inside `design/`. Founder decision **F-4**.
6. **Step 2 misses failed attempts.** Design 03 pages on unsuccessful attempts too. The step detects only `AssumeRole` of `OrganizationAccountAccessRole`.
7. **Forwarding is Sandbox-only.**
   - B-087's still-open half: `platform-event-forwarding` is deployed to the Sandbox OU only. Step 2's alert cannot fire from any other account until forwarding is estate-wide.
   - That StackSet is management-owned (§1 15.3 item 1).
   - `DeploymentForwarding` forwards `AssumeRoleWithWebIdentity` only, not `AssumeRole` (`70-event-forwarding.yaml:277-297`). Add a separate rule, as B-087 argues.
   - **Unproven:** which region records `AssumeRole` when the global STS endpoint is used. The rule must exist in every region that can record it.
8. **JIT evidence.** SCIM group-membership events from Identity Center CloudTrail do not carry the PIM justification or the approver, which design 03:30-31 requires. I believe Entra SCIM runs on a provisioning cycle of about 40 minutes, so elevation and expiry lag, and a live session outlasts group removal until `SessionDuration` ends. **Unverified.** Evidence needs the Entra audit log as well as, not instead of, CloudTrail.
9. **D-007's close condition** needs JIT for **both** `PlatformAdmin` and `SecurityResponder` (`deviations.md:611-613`). Step 4 builds PlatformAdmin only, and no `PlatformAdmin` permission set exists in `identity/`.
10. **"Closes D-008" is wrong.** D-008 is "Delayed test message — CLOSED" (2026-09-16). It is most likely a typo for **D-001** (root mailbox is a personal alias). That fits alongside root-credential work, but 15.5 does nothing to close D-001 either. Founder to confirm the intended item.
11. **Root credential removal.**
    - Removing member-account root passwords and MFA departs from design 15:113 ("secured with hardware MFA"). Record it as a D- item. It is the better control, but it is not what the design says.
    - Enabling centralized root access is a management-account change (STOP).
    - SCP 03 `DenyMemberAccountRootUser` already blocks member root actions, so this is defence in depth.
12. **Root-use detection** exists nowhere. `DenyMemberAccountRootUser` is preventive only. Step 2 should include root `ConsoleLogin` and root sessions, and the "privileged root task" sessions step 3 itself uses.

**Stop conditions.**
- Every trust change to `OrganizationAccountAccessRole` (lockout).
- Enabling centralized root access.
- Credential removal.
- Deleting `PlatformBootstrapAdmin`.
- Any 06a edit.

---

### 15.6 — AWS Config everywhere, Security Hub central configuration, scope-change detection

**Touches.**
- `baseline/10-config-recorder.yaml`, plus a new management-account recorder stack.
- `scripts/enable-security-services.sh`, `scripts/verify-security-coverage.sh`, `config/tiers.yaml`, `scripts/lib/tiers.py`.
- New `docs/hipaa-control-mapping.md`.
- Macie configuration, and the router (`alerting/src/router.py` / `commercial/src/route.py`, which has a `scope-change` class at `:69`).

**Design.**
- 04:5: the detective stack runs "in every environment, without exception".
- 04:12: Security Hub "CIS/PCI" standards.
- 04:18-20: conformance packs "HIPAA operational best practices, PCI-DSS, AltDigital baseline".
- 04:27-30: evaluation "within minutes".
- 02:65-67 and 14:79: detective controls uniform, "Full" in every tier.
- 04:35 and 11:235: Macie.
- 11:256-275 and 12:82: scope change in dev and test, routed to commercial.

**Conflicts and findings.**
1. **The recorder gap is confirmed and unrecorded.**
   - The dump shows recorders only in Sandbox Canary and Sandbox Exercise, us-east-2, CONTINUOUS.
   - There are zero recorders in the management, Log Archive, Infra Tooling, Audit, Forensics and Backup accounts, in any region. Forensics us-east-1 has 12 `securityhub-*` rules and no recorder.
   - **No B- or D- item records this.**
   - B-013 says "the detective services are already enabled in all three allowed regions". That is true for GuardDuty, Security Hub, Macie and Inspector, but misleading for Config. Correct B-013 when this is fixed.
2. **The aggregator does not exist.** B-013, D-011 and `10-log-archive.yaml:492-493` all refer to a Config aggregator in Audit. No repository code creates one, and the dump cannot see one. Audit is only registered as Config delegated admin. Add it to 15.6, and to 15.1's dump.
3. **Daily recording in non-production contradicts the design.** It conflicts with 04:27-30, 02:65-67 and 14:79. Dev is where "developer does something unwise" detection matters most. Founder decision **F-6**. It is also a cost lever, which is presumably why it's there.
4. **The tier axis does not fit.** Design 14's axes are *data handling* and *resilience* (14:37-40). `tiers.yaml` has no "platform" value, so "continuous for platform accounts" has nowhere to live. Platform accounts are mapped to `prod` by `sync-account-tiers.sh:84`. Use that, or add an account-kind input, and record which. B-121 (tier semantics enumerated, not derived) is the adjacent item.
5. **Standards.**
   - FSBP plus NIST 800-53 r5, with no CIS and no PCI, departs from 04:12 and 04:18-20. Using NIST in place of the HIPAA conformance pack is also a departure. Record both as one D- item, and amend design 04 for the founders' PCI-out decision.
   - Security Hub is **locally** configured today: `enable-security-hub --enable-default-standards` in Audit and management (`enable-security-services.sh:173-174, 210-215`). There is no central configuration policy.
6. **"355 rules" are Security Hub's own service-linked rules, not a conformance pack.** In each Sandbox account there are 353 `securityhub-*` rules plus `platform-secret-max-age` and `platform-secret-uses-cmk`. Step 4's "keep only the delta as the platform conformance pack" is therefore **two rules**. Conformance packs exist only as unverified names in `vesting/derivation-rules.yaml:40-50`, and nothing deploys them.
7. **The cardholder tripwire inverts the pattern the prompt tells it to follow.**
   - `regulated_data_is_finding` is `true` only for `non_production` (`tiers.yaml:70-73`) and `false` for `production_equivalent` and `production` (`:88`, `:106`). Following it would stay silent in UAT and prod, which is exactly where cardholder data would land.
   - "Cardholder data anywhere is a finding" is a different concept: a platform-scope rule, not a tier attribute. It needs its own attribute or a frameworks-based rule.
   - Also, PCI is a *supported* questionnaire framework today: `derivation-rules.yaml:44-45, 77`, `derive-vesting.py:123-148, 347-350`, `docs/client-onboarding-questionnaire.md:220, 245-257`. 15.10 addresses that. 15.6 must not assume it is already blocked.
8. **Macie automated discovery reverses a recorded choice.** `enable-security-services.sh:29-34` explicitly leaves it off ("Per-tenant discovery belongs to the questionnaire's data classification"). Record the reversal.
9. **Emitter rule.** Macie matches must arrive at the router as a *classification* (`scope-change`) that the router turns into urgency, not as an emitter-declared incident severity.
10. **StackSet ownership.** `platform-config` is management-owned (§1 15.3 item 1). The management-account recorder is a plain stack, since StackSets cannot reach the management account.
11. **Ordering with 15.8.** 15.8 wants the secrets Config rules deployed to platform accounts. They evaluate nothing until 15.6 puts recorders there. The template says so itself (`baseline/30-kms-and-secrets.yaml:307-309`), and B-062 records the same ordering failure. **15.6 before 15.8.**
12. **Coverage checks.** `verify-security-coverage.sh:84-105` checks member counts for GuardDuty, Security Hub, Macie and Inspector only. It has no Config check at all.

**Stop conditions.**
- The management-account recorder.
- Security Hub central configuration from Audit applied to Members, Security and Infrastructure.
- The Macie cost threshold (the prompt already stops here). This needs a founder figure, **F-7**.

---

### 15.7 — Flow logs (B-012), time-boxed

**Touches.**
- `security/10-log-archive.yaml`, `baseline/40-network.yaml`.
- Probe resources in Sandbox Exercise.
- `baseline/20-log-data-protection.yaml` is **mis-cited**: it holds CloudWatch Logs data protection only, with no flow-log configuration.

**Design.**
- 11:167-212: flow logs are the liveness and attribution signal; port 22 inbound is a finding.
- 11:53: developers can read their flow logs.
- 04:190-194: retention floor is "derived, not asked".

**Conflicts and findings.**
1. **The review's leading hypothesis is already disproved.** B-012's table (`build-backlog.md:871-880`) records these as tested and not the cause:
   - "`aws:SourceOrgID` on the KMS grant"
   - "`aws:SourceOrgID` on the bucket grants"
   - "SSE-KMS with the platform key"
   - Object Lock, DenyInsecureTransport, versioning, and the PrincipalArn delete-deny

   Most of step 1's matrix re-runs recorded negatives.
2. **B-012 already says where to start instead.** The CLI-created probe bucket works, and a CloudFormation bucket with the "same" settings does not. The next step recorded is to diff them exhaustively (`get-bucket-*` across every sub-resource). The candidates never compared are ObjectOwnership, the public access block, the bucket ACL and ownership controls.
   - The probe is live: `altdig-flowlog-probe-<acct>` in Log Archive us-east-2, not in any template.
   - `FlowLogBucket` has no `OwnershipControls` (`10-log-archive.yaml:640-696`). Its policy keeps the `x-amz-acl: bucket-owner-full-control` condition (`:718-731`).
   - **Recommend replacing step 1's matrix with that diff.** It is the recorded next step, and 15.1 item 2 makes the dump able to produce half of it.
3. **One genuinely untested variable.** B-012 doesn't say whether the probe tested **cross-account** delivery (member VPC to the Log Archive bucket) or same-account delivery. That belongs in the matrix.
4. **A fixed 400-day retention conflicts with the derived-floor principle** (04:190-194). HIPAA derives 2,190 days (`derivation-rules.yaml:76`). The repository's recorded position is that flow logs are "operational rather than evidentiary" (`20-archive-replicas.yaml:138-139`; `10-log-archive.yaml:661-662`), which is why they aren't locked. Either keep that position and record 400 days as operational retention, or derive it from the tenant floor. Today there is no expiration at all (Glacier IR at 90 days, then kept forever).
5. **No `LogFormat`, so no v5 fields** (`40-network.yaml:365-380`). The comment at `:361` calls the destination "immutable"; it isn't (tagged `Immutable: false`).
6. **Adjacent items.** B-101 (port-22 detection, Athena over S3 flow logs) is blocked behind this. Flow logs are not covered by log custody (B-134). The `FlowLogBucket` policy grants `delivery.logs` with no source condition (`:718-731`), which is worth scoping once the cause is known.

**Stop conditions.**
- Changing `platform-network` (management-owned StackSet).
- Any change to the Log Archive in the Security OU.

---

### 15.8 — Secrets hygiene and retention

**Touches.**
- `baseline/30-kms-and-secrets.yaml`.
- `scripts/lib/common.sh` (`store_secret_everywhere`, `:655-720`; no `--kms-key-id` anywhere).
- `scripts/set-jira-token.sh`, `set-pagerduty-token.sh`, `sync-pagerduty-secrets.sh`, `sync-pagerduty-tenants.sh`, `pagerduty-apply.sh`, `pagerduty-drift.sh`, `truveon-tenant-handoff.sh`, `pagerduty/`.
- `security/10-log-archive.yaml`, `security/20-archive-replicas.yaml`.
- `vesting/derivation-rules.yaml`, `scripts/derive-vesting.py`, `scripts/vest-client.sh`.
- `alerting/40-cadence.yaml`.
- **Every consumer role's KMS grant** (item 3 below).

**Design.**
- 04:77, 111: a per-account secrets CMK.
- 04:114-118: rotate where supported; otherwise a documented cadence, a named owner and max-age detection.
- 13:259: "rotated".
- `open-items.md:21` (B10): Jira, PagerDuty, **Truveon**.

**Conflicts and findings.**
1. **The retention premise is probably wrong.**
   - D-024 (Object Lock, formerly D-010) records "**Deployed as COMPLIANCE/2190** on the shared archive". The 365 is only the template default (`10-log-archive.yaml:26-35`). `deploy-to-account.sh:5-7` shows `RetentionDays=2190`, and the replicas default to 2190 (`20-archive-replicas.yaml:37-41`).
   - The irreversible default change and the Batch Operations job may be unnecessary.
   - **Before planning anything irreversible, confirm with a read-only `get-object-lock-configuration`**, which 15.1 item 2 makes part of the dump.
2. **A COMPLIANCE Config archive is not possible on the source bucket.** `altdig-config-archive` has no Object Lock because Config cannot deliver to a locked bucket (`10-log-archive.yaml:459-498`). Only the replica is locked, and it already is at 2190 (`20-archive-replicas.yaml:103-120`).
   - Replicas have **no lifecycle rule** and no destination storage class, so they sit in STANDARD for the whole period. The "existing Glacier transitions" in the cost estimate don't apply to them.
   - Design 04:185 ("Deep Archive to retention floor") is not built.
3. **The CMK already exists, and re-encrypting breaks every consumer.**
   - `alias/platform-secrets` is in `baseline/30-kms-and-secrets.yaml:192-229`. It is deployed only in the two Sandbox accounts, because `platform-kms-secrets` is a management-owned StackSet targeting Sandbox.
   - None of the secret-reading roles has `kms:Decrypt` on a secrets key:
     - reconciliation (`change/10-reconciliation.yaml:256-258, 349-351`)
     - quorum (`quorum/10-quorum.yaml:228-230, 323-325, 412-414`)
     - commercial (`commercial/10-commercial-routing.yaml:212-214`)
     - evidence (`evidence/10-evidence.yaml:455-457, 553-555`)
     - router (`alerting/20-router.yaml:256-263`)
   - CloudFormation `{{resolve:secretsmanager}}` in `10-alert-topic.yaml` also needs the deployer to decrypt.
   - **Grant first, then re-encrypt.** Otherwise alerting stops, and that includes the path that would report the outage.
4. **Distinct credentials vs a single copy.** The copies are a documented workaround: the AWS-managed key cannot be shared across accounts (`set-jira-token.sh:64-67`; `common.sh:584-588`; B-110 option 1). A CMK makes B-110 option 2 possible: one copy, read cross-account. Step 2 (distinct credentials per account) is the stronger control for attribution, but it doubles the tokens to manage. Decide it explicitly rather than by default.
5. **The PagerDuty OAuth scope is misstated.**
   - The router uses Events API routing keys, not the REST token (`pagerduty/README.md:37-44`).
   - The REST token's consumers are Terraform apply, drift checking and provisioning.
   - Whether provider `~> 3.0` (`pagerduty/versions.tf`) supports a scoped OAuth client is **unverified**. Check before promising it.
6. **"Closes B10" overclaims.** It closes the PagerDuty third only. Jira is B-070 (a personal Atlassian identity posing as a service account), and Truveon's identity is untouched.
7. **Expiry watching is half built.**
   - B-071 already watches Jira token expiry (`JiraTokenDaysRemaining`, alarm below 30 days, `change/10-reconciliation.yaml:538-561`).
   - Nothing watches PagerDuty token age.
   - `alerting/40-cadence.yaml` is a schedule-liveness sweep with no token logic. Extend the B-071 pattern rather than putting reminders in the cadence sweep.
8. **The max-age rule may not measure a manual re-issue.** `SECRETSMANAGER_SECRET_PERIODIC_ROTATION` keys off Secrets Manager's rotation date, not `put-secret-value`. If so, it will flag a manually re-issued token that is new, or never measure it correctly. **Unverified.**
9. **Two small fixes.**
   - The vesting check (step 6) doesn't exist; `derive-vesting.py:364-371` only warns at ≥2190.
   - `vest-client.sh:405-412` prints "immutable for ${RETENTION} days", but the object gets the *bucket default*, not the derived floor. The message is inaccurate. Fix it with step 6.
10. **B-016 still applies.** Routing keys sit in cleartext in the local `pagerduty/terraform.tfstate`. It is gitignored, but it is outside any CMK story.

**Stop conditions.**
- Anything irreversible (Object Lock). Likely **none needed** if item 1 confirms.
- Changing `platform-kms-secrets` (management-owned).
- Re-encryption before every consumer has its grant (it would stop alerting).

---

### 15.9 — FinOps account and cost allocation

**Touches.**
- `scripts/create-platform-account.sh`: `--dry-run` supported. It has no `finops` role in its documented list (`:35`), but the generic slug check (`common.sh:444`) allows it. Pass `--name`. The header still shows the stale `ad-` alias (`:9`).
- `scripts/sync-cost-categories.sh`, `scripts/lib/cost_categories.py`, `cost/src/report.py`.
- `identity/00-commercial-readonly.yaml`: its NotAction deny (`:237-262`) denies Athena, S3, Glue and KMS, so retargeting means deliberately widening it.
- `scripts/deploy-commercial-access.sh`, `commercial/`, `alerting/30-runaway.yaml`.
- A new Data Exports / Athena template.

**Design.**
- 12:30-32: CUR to S3 in a **dedicated reporting account** plus delegated Cost Explorer. 15.9's FinOps account *is* this account.
- 10:68-84: shared platform cost is "**absorbed into the percentage uplift**", never allocated.
- 10:100 and 12:79: budgets and anomaly detection per account, routed by tenant.
- 12:59-61: commercial views reveal nothing about content.
- 12:117: raw cost, flat fee and uplift shown separately.

**Conflicts and findings.**
1. **D-009 (`CommercialReadOnly`) cannot fully close.** Its close condition (`deviations.md:655-672`) needs a reporting account, CUR landing there, **and a billing delegated administrator registered**. 15.9 doesn't mention the third.
   - D-009 also records that invoices, payment instruments, credits and tax are payer-only.
   - Step 3 is therefore a **split**, not a move: the analytical surface goes to FinOps, and a narrowed invoice-and-payer grant stays in the management account. D-009 then narrows; it does not close.
2. **Split-charge rules (step 5) contradict design 10.**
   - Allocating management, Security OU and Infra OU cost across tenants in proportion to spend reverses design 10's "absorbed into the uplift".
   - If allocated cost reaches a tenant's raw AWS cost, the uplift is charged on shared cost as well. The percentage then reads as cost-plus, which open item X3 exists to prevent.
   - `report.py:227-233` implements "never allocated".
   - Keep it internal-only (B-090's shared-services report), or get a founder decision, **F-8**.
   - I believe split-charge results appear only on the Cost Categories page, not in the Cost Explorer API or CUR. If so, the step wouldn't feed `report.py` anyway. **Unverified.**
3. **Step 8 is borderline on scope.** It is framed as an input to the flat-fee decision (open item N3). CLAUDE.md scopes commercial work to "billing mechanics only; pricing … out of scope". Measuring baseline cost by service is arguably mechanics, but per-account spend must stay out of the repository and out of session reports (§0.8). Report ratios only.
4. **Activating `DataClassification` as a cost-allocation tag (step 7)** exposes which resources are `regulated` to every billing viewer, against 12:59-61. Design 10:113-120 says account-derived dimensions need no tags. Activate Owner and Application, and decide DataClassification explicitly.
5. **SCP 04 is Sandbox-only until 15.4**, so tenant resources won't reliably carry the tags until then. **15.4 before 15.9 step 7.**
6. **Already present.**
   - A `Tenant` cost category exists (`cost_categories.py:39-56`), along with six others, all REGULAR on `LINKED_ACCOUNT`.
   - The commercial router already defines `cost-anomaly` and `budget-breach` (`commercial/src/route.py:67-68`), and a cost-anomaly event was verified end to end (`build-backlog.md:4636`). Only the monitors and budgets are missing.
   - `alerting/30-runaway.yaml` holds CloudWatch rate alarms, not Budgets or Anomaly Detection.
7. **Items that close or move.** B-102 records "CUR export to S3 in a dedicated reporting account — Not built", which **closes** here. D-020 and B-126 are unaffected: moving the permission set doesn't make Cost Explorer partner-scopable.
8. **Account creation** is a STOP (the prompt already says so). The FinOps account must be added to 15.6's recorder coverage and the verification sweep in the same session, or `verify-security-coverage.sh` fails once 15.6 extends it.

**Stop conditions.**
- Account creation.
- Data Exports in the management account.
- The billing delegated-admin registration (management account).
- Cost-allocation tag activation (payer).

---

## 2. Dependency check

### 2.1 "The SCP exemption rewrite (15.2) must name the pipeline role created in 15.3": partly refuted

**What's right.**
- The rewrite must leave room for the pipeline's principals.
- Naming a role that does not yet exist in an `ArnNotLike` list is harmless, so 15.2 can safely run before 15.3.

**What's wrong.** It names the wrong role.
- `PlatformPipelineDeployer` lives in Infra Tooling and only assumes roles. It performs no SCP-denied action itself, so **it should be exempt from nothing**.
- The principals that act in target accounts are:
  1. **`stacksets-exec-*`** for every StackSet deployment. It is already exempt and must stay exempt. Every baseline StackSet, including the IGW and NAT in `platform-network`, deploys as this role (§1 15.2 item 4).
  2. **`PlatformDeployer`** (per account) for everything `deploy-to-account.sh` does over OAAR today. That covers every Audit, Tooling, Log Archive and Forensics stack. These stacks create Platform\* roles and write key and bucket policies on `platform-managed` resources.
- So `PlatformDeployer` must appear in 01 and 02 (key and bucket policies via CloudFormation), and in **03's `DenyPlatformRoleNameSquatting` and `DenyPlatformRoleModification`**. Without that, no direct stack can create or update a Platform\* role once OAAR is narrowed.
- It must **not** appear in `DenyQuorumExecutorCreationEvenByOperators` (B-125).
- `PlatformMgmtDeployer` needs no SCP entry, because SCPs don't apply to the management account.

**The dependency the claim misses.** 03 is attached to Members, Security and Infrastructure as one shared object. Adding `PlatformDeployer` to it is therefore **not a Sandbox-only change** (§1 15.2 item 1). It needs the v2-policy swap and a STOP, before 15.3 can deploy anything outside Sandbox through the pipeline.

### 2.2 "`OrganizationAccountAccessRole` must not be narrowed (15.5) until the pipeline path is proven, or deployments lock out": confirmed, and the condition is broader than stated

**Confirmed.**
- Every workstation run starts as `PlatformBootstrapAdmin` in the management account (`common.sh:44-45` → `PLATFORM_AWS_PROFILE=msp-mgmt`; `bootstrap-runbook.md:115-117`). Every cross-account action then assumes OAAR (36 assumption sites across 35 files).
- Narrowing OAAR's trust to the `PlatformBreakGlass` SSO role makes every one of those fail. That includes `deploy-to-account.sh`, all 13 `publish-*.sh`, `test-guardrails.sh` (`:111-113`), `estate_dump.py` (`:847`), `create-platform-account.sh` (`:151`), `sync-cost-categories.sh` (`:47-51`) and the vesting scripts.

**"The pipeline path is proven" must mean all of these, not the one Sandbox deploy 15.3 names.**
1. `PlatformDeployer` is deployed to **every** member account, including Security and Infrastructure, not only Sandbox.
2. The v2 SCP exemptions naming `PlatformDeployer` are attached to every OU (§2.1).
3. Every script that assumes OAAR has either moved to the pipeline or to `PlatformDeployer`, or is explicitly a break-glass script. The whole list, not a sample: 15.5's precondition says "STOP and list them", and that list is §1 15.3 item 11.
4. GitHub OIDC works **under the RCP** in Infrastructure (§1 15.3 item 4), if 15.4 has attached it there.
5. The baseline StackSets can be updated without the management account. **Done 2026-09-30:** Tooling operates them as delegated admin. Still via OAAR into Tooling until `PlatformDeployer` exists.
6. `test-guardrails.sh` itself no longer needs OAAR. It currently bootstraps through it (`:111-113`).
7. A tested break-glass path exists *before* the narrowing. Otherwise the narrowing removes the only recovery path at the moment it's most likely to be needed.

**A second lockout the claim doesn't cover.** The RCP-vs-federation question (§1 15.3 item 4). If the RCP denies `AssumeRoleWithSAML`, attaching it to every OU in 15.4 locks every human out of every member account, the break-glass permission set included.

### 2.3 Other dependencies found

| Must happen first | Before | Why |
|---|---|---|
| ~~Fix B-014~~ done 2026-09-29 (9ba9b41) | 15.5, 15.8, 15.9 | Those prompts close or cite D-009 and D-010, and each ID currently resolves to two entries. |
| Federation-under-RCP test (OIDC and SAML) | 15.3, 15.4 | A possible lockout; the result decides both prompts' designs. |
| 15.1 (root attachments, Object Lock, role paths) | 15.2, 15.8 | 15.2's harness baseline and 15.8's retention premise both need data the dump can't show today. |
| 15.2 v2 policies incl. `PlatformDeployer` in 03 | 15.3 outside Sandbox | §2.1 |
| F-2 decision (retire and replace the management-owned StackSets) | 15.3 | Decides whether 15.6–15.8 baseline changes go through the pipeline at all. |
| 15.7 step 1 (log-delivery source keys) | 15.4 step 2 (S3/KMS RCP) | §1 15.4 item 4 |
| 15.4 (SCP 04 on Members) | 15.9 step 7 | Tags must be enforced before they are billed on. |
| Event forwarding estate-wide (B-087 remainder) | 15.5 step 2 | The alert cannot fire outside Sandbox without it. |
| 15.6 (recorders in platform accounts) | 15.8 step 4 | The Config rules evaluate nothing without a recorder. |
| 15.8 step 3 (consumer KMS grants) | 15.8 step 1 (re-encrypt) | Otherwise alerting stops. |
| 15.9 account creation | 15.6 coverage check closing | A new account without a recorder fails the extended check. |
| 15.3 | 15.11–15.13 | Stated in 15.11; the comment-only redeploys go through the pipeline. |

### 2.4 Order (as decided 2026-09-25; see §6)

**Before any prompt:**
1. Apply the §0 corrections to the standing-rules block.
2. Fix B-014 (renumber the duplicate D-009 and D-010).
3. Resolve the §6.10 questions. Q-1 and Q-2 gate the first steps below.

**Then:**
1. **Federation test (new, first).** Test OIDC (`AssumeRoleWithWebIdentity`) and Identity Center SAML (`AssumeRoleWithSAML`) into a Sandbox account under the current RCP. The result is read-only evidence, plus an RCP fix in Sandbox if the deny is real.
2. **15.1:** dump extension, with F-1's move of the dump off git.
3. **15.2 step 1**, extended: harness cases, plus the squatting variants.
4. **15.7 step 1:** the bucket diff (B-012's recorded next step). Cheap, and it informs 15.4.
5. **15.2 steps 2–3:** v2 policies, Sandbox only.
6. **15.3:** ~~including F-2's retire and replace of all ten StackSets~~. The StackSet part is done (D-012 closed, 2026-09-30); 15.3 is now the pipeline only.
7. **15.4:** promotion, including the v2 swap of 01–03 on the other OUs.
8. **15.5a:**
   - root credentials: centralized root access, credential removal, and D-001's mailbox move;
   - 06a record-only entries (OAAR assumption);
   - every-tier entries for root sign-in and root sessions (F-4);
   - the break-glass IAM users and their detection (F-3).

   The OAAR trust is **not** narrowed here.
9. **15.6:** before 15.8.
10. **15.7** remainder.
11. **15.8.**
12. **15.9.**
13. **Entra cutover (proposed, Q-5):** its own session. Break-glass is proven first; the permission sets are redeployed after.
14. **15.5b (last):**
    - JIT via quorum (F-5);
    - narrowing the OAAR trust, which flips the OAAR entry to every-tier paging in the same change (F-4);
    - deleting `PlatformBootstrapAdmin`.

    Its precondition is the whole of §2.2.

The prompts document puts 15.5 fourth. On the evidence above, running all of it there would lock out deployments. That is the risk the dependency claim is about, and it is why 15.5 is split.

---

## 3. Open items this phase closes, and items it creates

### 3.1 The expected closure list, checked

| Item | Expected | Actually |
|---|---|---|
| **B-010** (`locked` egress) | deprioritised | Already deprioritised in B-010's own text ("materially less urgent"; build when questionnaire 3.2 requires it and it is priced). 15.10, not 15.1–15.9, adds the vesting block that makes it a deliberate decision. **Update the trigger in 15.10.** |
| **B-012** (flow logs) | closed | Closed by 15.7 only if the cause is found. Otherwise it stays open with the matrix and the Security Lake write-up. |
| **B10** (service identities) | closed | **Partially.** PagerDuty only, and only if a scoped OAuth client is supported (§1 15.8 item 5). Jira is B-070; Truveon is untouched. |
| **D-006** (root virtual MFA) | partially | **Decided (F-3):** passkeys stand in for design 03's hardware MFA, with a new D- item consistent with D-006. D-006 itself closes only if design 03 is amended to permit it (its own close condition). Otherwise it stays open, now alongside the new item. |
| **D-007** (standing `PlatformBootstrapAdmin`) | closed | **Decided (F-5):** closes when JIT via quorum works for both `PlatformAdmin` and `SecurityResponder`, and `PlatformBootstrapAdmin` is deleted, in 15.5b. |
| **D-008** | closed | **Dropped** (founder error). |
| **D-001** (root mailbox is a personal alias) | *not listed* | **Added by founder decision.** 15.5a moves the management root email to a shared, access-controlled role mailbox. D-001 also covers `mspr@` for member roots. Removing member root credentials (15.5a) reduces what that mailbox matters for, but it does not by itself satisfy D-001's close condition (both mailboxes shared). |
| **D-009** | closed | Ambiguous (B-014). The `CommercialReadOnly` D-009 **narrows**, it does not close (§1 15.9 item 1). The alias entry (now D-023) isn't touched by any prompt; it closes when design 15 and open item B15 are updated. |
| **D-012** (management-owned StackSets) | *not listed* | **Closed 2026-09-30.** `deploy-stackset.sh` operates from Infra Tooling as delegated admin; the premise that a StackSet can't be operated from another account was wrong. |

### 3.2 Items the phase also closes or changes (not in the expected list)

- **B-014.** Should close first (§2.3).
- **B-087** (remainder). Closes when event forwarding is deployed estate-wide, which 15.5 needs.
- **B-102** (CUR in a reporting account). Closes in 15.9.
- **D-012.** Stale: ten StackSets, not four. Closes if F-2 chooses retire and replace in 15.3. Otherwise it must be restated with the true count.
- **B-013.** Its Config claim needs correcting in 15.6.
- **B-125.** Its reasoning is reversed by 15.2 (§1 15.2 item 11). Annotate it.
- **B-128, B-129.** Triggers fire in 15.4 (SCP 04 on Members).
- **B-001.** 15.2 makes it live (explicit lists grow 01–03).
- **B-110.** Option 2 (single copy under a CMK) becomes available in 15.8.
- **B-101.** Unblocks only if 15.7 succeeds.
- **D-003.** 15.4 extends RCP use. Design 04 and 11 amendments still owed.
- **D-024** (Object Lock, formerly D-010). Its "deployed COMPLIANCE/2190" is what 15.8 must verify, not overwrite.
- **Open item B15** (alias prefix "`ad-` assumed"). Stale against D-023 (alias). Not a Phase 15 item, but found here.

### 3.3 Items to create (proposed; numbering from B-135 / D-025, assigned when written)

**Backlog:**
1. Config recorders absent from every platform account and from us-east-1/us-west-2. The gap is confirmed and unrecorded; 15.6 closes it.
2. The Config aggregator referenced in B-013, D-011 and `10-log-archive.yaml` is never created.
3. `deploy-policies.sh` never detects an attached RCP (`:248-250`).
4. `stacksets-exec-*` and `OrganizationAccountAccessRole<suffix>` name squatting (15.2).
5. RCP vs federated principals (OIDC/SAML): unproven lockout risk.
6. Scripts inline their own OAAR assumption; `common.sh` is not the single path (§1 15.3 item 12).
7. `estate/estate.20260925.json` is untracked and not ignored (§0.4); dump output policy (F-1).
8. `estate_dump.py:15` claims no raw API responses, but it stores several.
9. `vest-client.sh:405-412` claims per-tenant immutability it doesn't apply.
10. `FlowLogBucket` lacks the `platform-layer` tag and a source condition; the network template comment says "immutable".
11. `alerting/10-alert-topic.yaml:129-131` claims `delivery.logs` `SourceOrgID` is confirmed, which is unsupported.
12. IMDSv2 enforced nowhere (closed by 15.4 step 3).
13. PagerDuty token age unwatched (B-071 covers Jira only).
14. Archive replicas have no lifecycle (STANDARD for 2,190 days), and design 04:185 Deep Archive is not built.

**Deviations:**
1. App deployer naming: `PlatformAppDeployer-` then `TenantAppDeployer-` vs design `AppDeployer`.
2. GitHub Actions vs CodePipeline, and repository location (design 08).
3. `PlatformPipelineDeployer`/`PlatformDeployer`/`PlatformMgmtDeployer` vs `BaselineDeployer` (design 03, 04).
4. ~~Break-glass via Identity Center~~. Superseded by F-3: break-glass is two management-account IAM users with **passkeys in place of design 03's hardware MFA**, consistent with D-006 (§6.3).
5. Member root credentials removed vs design 15:113 "secured with hardware MFA".
6. Security Hub standards FSBP + NIST vs design 04 CIS/PCI; HIPAA pack replaced by NIST mapping.
7. ~~Non-production daily recording~~. Dropped by F-6 (continuous everywhere).
8. Macie automated discovery enabled, reversing `enable-security-services.sh:29-34` (a recorded choice, arguably a backlog annotation instead).
9. Flow log retention: "operational, not evidentiary", 400-day expiry, raised by vesting when a contract requires it (decided, §6.9).
10. Shared-cost allocation exists only as an internal FinOps Athena margin view, never invoiced; design 10 unchanged (F-8, §6.8).
11. Guard exceptions for administrator policies: the break-glass IAM users (F-3), and `PlatformAdmin` if it can't be scoped (§6.5).
12. The 06a amendment is a founder-authorised edit to the design package (F-4). Record it so the edit to `design/` is traceable.

---

## 4. What the review missed or got wrong

Stated plainly, most consequential first.

1. **SCPs 01–03 are shared objects across four OUs.** Any "Sandbox-only" change to them is a production change. The review's 15.2 plan would have changed Members, Security and Infrastructure in one API call.
2. **The flow-log hypothesis was already tested and ruled out** (B-012). So was most of the proposed matrix. B-012 names the actual next step (bucket sub-resource diff), which the review didn't use.
3. **The Log Archive is already COMPLIANCE/2190** per D-024 (formerly D-010). The 365 is the template default only. The review proposed an irreversible change and a Batch Operations job to reach a state that is recorded as already deployed.
4. **Infra Tooling's StackSet delegation is unused.** All ten StackSets, not four, are management-owned, so the pipeline cannot update the baseline. The review treated delegation as the working mechanism.
5. **The pipeline acts as `PlatformDeployer` and `stacksets-exec-*`, not `PlatformPipelineDeployer`**, so the exemption the review asked 15.2 to reserve is for the wrong role (§2.1).
6. **The exemption exposure is wider than Finding B.** `stacksets-exec-*` squatting escapes 01–03 as well as 04–07, and `OrganizationAccountAccessRole*` has a trailing wildcard. The review tested neither.
7. **The current RCP may already deny OIDC and SAML federation.** That affects the pipeline, the tenant AppDeployer and Identity Center sign-in, including 15.5's break-glass. The review extends the RCP without checking this.
8. **The platform-secrets CMK already exists** (`alias/platform-secrets`), and re-encrypting under it without consumer grants stops alerting.
9. **The "355 rules" are Security Hub service-linked rules plus two platform rules.** The "delta" pack is two rules.
10. **`regulated_data_is_finding` is false in UAT and prod.** Following it, as 15.6 instructs, would make the cardholder tripwire silent exactly where it matters.
11. **15.5 contradicts three recorded or designed positions:** D-006 (no hardware tokens), open item D1 (Entra P2 deferred, which PIM needs) and design 03 (break-glass independent of Entra). Its "page at highest severity" also breaks the emitter hard rule.
12. **15.9's split-charge contradicts design 10's "absorbed into the uplift"**, and D-009 cannot fully close by moving the grant.
13. **The dump's verify step already exists** and sweeps the whole document, so 15.1's new-field check is already covered.
14. **The `docs/open-items.md` path and the B-129/D-019 numbering are wrong**, and **D-008 is closed** (§0, §3.1).
15. **Things the review got right, confirmed:**
    - Finding A (app deployer exempt).
    - Root attachments not captured.
    - The Config recorder gap.
    - The Truveon push direction.
    - The AWS-managed-key secrets and their copies.
    - The General Access key.
    - The 51 OAAR references.

---

## 5. Founder decisions required

All resolved 2026-09-25. See §6 for the decisions and what each changes. §6.10
lists the questions the decisions themselves raise.

| # | Question | Resolved as |
|---|---|---|
| F-1 | Commit dump output or not | Stop committing; dumps go to an Object Lock bucket (§6.1) |
| F-2 | Management-owned StackSets | ~~Retire and replace all ten~~ **Withdrawn 2026-09-30**: Tooling operates them as delegated admin; no migration (§6.2) |
| F-3 | Break-glass identity; hardware MFA | Two management-account IAM users, passkeys, split credentials (§6.3) |
| F-4 | 06a paging entries | Amend 06a: every-tier for root and break-glass; record-only for OAAR until narrowing (§6.4) |
| F-5 | PIM or not | No PIM; JIT via quorum (§6.5) |
| F-6 | Config recording frequency | Continuous everywhere (§6.6) |
| F-7 | Macie ceiling | Set outside the repository, as a ratio in SSM (§6.7) |
| F-8 | Split-charge | None; internal Athena margin report (§6.8) |
| — | "Closes D-008" | Dropped; D-001 added to 15.5 (§6.9) |

---

## 6. Founder decisions, 2026-09-25, and what they change

Each decision below is recorded as given, followed by its effect on the prompts
and anything it raises. Items are created by the prompt that implements them,
not here.

### 6.1 F-1: dump output leaves git

**Decision.**
- Untrack `estate/estate.json` and gitignore `estate/*.json`.
- Write dumps to the Object Lock evidence bucket and update `estate/README.md`.
- No history rewrite; the committed copy stays in history, covered by repository access control.

**Effect.**
- 15.1 gains: `git rm --cached estate/estate.json`, the `.gitignore` rule, an upload step in `dump-estate.sh`, and a README rewrite.
- The upload runs only after `verify_no_leak` passes. Today that check already exits before anything is written (`estate_dump.py:876-881`). The order must survive the change, because a leak written to a COMPLIANCE bucket cannot be deleted by anyone until retention expires.
- `estate/estate.20260925.json` (§0.4) becomes ignored under the new rule. It is byte-identical to the committed copy.
- **Raises Q-1.** There is no "evidence bucket" with Object Lock. The only locked bucket is `altdig-log-archive-<acct>`, the CloudTrail archive (`security/10-log-archive.yaml:395-405`). The evidence pipeline has no bucket of its own; it reads the archives (`evidence/10-evidence.yaml:109-119`). See §6.10.

### 6.2 F-2: retire and replace all ten StackSets

> **Withdrawn 2026-09-30 (founder decision).** The premise, from D-012 and repeated by
> this plan untested, was that a StackSet can only be operated from the account
> that created it. Wrong for service-managed StackSets: Infra Tooling ran an update of
> `platform-canary-artifacts` with `--call-as DELEGATED_ADMIN`, and it succeeded.
> `deploy-stackset.sh` now operates every StackSet from Tooling, and D-012 is closed.
> Nothing was retired, imported or recreated. The text below is kept as the record of
> what was planned.

**Decision.**
- All ten management-owned StackSets become Infra Tooling-owned in 15.3. Every instance targets the two Sandbox accounts only.
- Fix the failed or outdated `platform-canary-smoke` instance first. The dump shows it `OUTDATED` in Sandbox Exercise and `CURRENT` in Canary.
- Prove retain-and-import (`--resources-to-import`) on `canary-artifacts` first, including the Retain KMS alias collision D-012 records.
- Close D-012 when done.

**Effect.**
- 15.3 gains the migration as its own ordered sub-step. `deploy-stackset.sh` moves to `--call-as DELEGATED_ADMIN` from Infra Tooling, and the `require_account mgmt` at `:82` goes.
- Deleting the old StackSets' instances with `--retain-stacks` is the irreversible moment. It is a rule-5 STOP per StackSet.
- `vest-client.sh:153` expects `platform-kms-secrets` to target Members. It must follow the new ownership.
- **Raises Q-2.** "Sandbox accounts only" is what the ten target today, so the migration preserves reach. But three later steps need baseline StackSets beyond Sandbox:
  - 15.5a root and OAAR detection needs `platform-event-forwarding` estate-wide (B-087's open half).
  - 15.6 needs `platform-config` in every account and region (F-6).
  - 15.8 needs `platform-kms-secrets` in the platform accounts.

  The decision doesn't say which prompt widens them.

### 6.3 F-3: break-glass is two IAM users in the management account

**Decision.**
- Two IAM users with passkey MFA and no Entra dependency.
- Password with one founder, passkey with another.
- The OAAR trust is narrowed to those two users.
- Passkeys replace design 03's hardware MFA; record that as a D- item consistent with D-006, together with the guard exception for their admin policy.
- Rename `--break-glass` to `--use-org-access-role`.

**Effect and checks.**
- **CLAUDE.md's IAM-user rule is not breached.** The rule is "no IAM users and no long-lived access keys for human principals **in member accounts**", and these users are in the management account. They must still have **no access keys** (console plus passkey only). Enforce that with a guard rule on the template, and have the 15.1 dump report per-user access-key count; the dump already counts IAM users.
- **SCPs never apply to the management account**, so these users are constrained only by their own policy. That is expected for break-glass. It makes detection (F-4) the whole control.
- **The trust principals must come from template parameters**, not literals (§0.7).
- **Detection needs a plain stack in the management account.** StackSets cannot reach it, and `ConsoleLogin` for IAM users is recorded there. That is a management-account change (STOP).
- **Credential custody** (which founder holds what) belongs in the gitignored `config/identity.env` pattern, or outside the repository. The runbook names roles, not people.

### 6.4 F-4: 06a amended (founder-authorised design edit)

**Decision.**
- Every-tier PagerDuty entries for root sign-in and root sessions (including privileged root tasks), and for break-glass user authentication attempts, success and failure.
- A record-only entry for OAAR assumption now, becoming every-tier paging on the day the OAAR trust is narrowed.
- The router decides severity; the emitter classifies only.

**Effect.**
- 15.5a edits `design/06a-alarm-specification.yaml`. This is the one authorised exception to "build work does not edit `design/`"; cite this decision in the commit.
- The instrumentation Lambda consumes 06a directly (`scripts/publish-instrumentation.sh`), so the publish step follows.
- **The flip must not depend on memory.** Make the 06a change from record-only to every-tier paging part of the same change set that narrows the OAAR trust in 15.5b. Add a harness assertion that fails if the trust is narrowed while the entry is still record-only, so the gap can't open silently (the "every decision model needs an else" rule).
- Root events come from three places:
  - member-account root sign-in, recorded in the member account and forwarded;
  - privileged root task sessions (`sts:AssumeRoot`), called from the management account and recorded there;
  - management root sign-in, recorded in the management account.

  The last two need the management-account detection stack from §6.3.

### 6.5 F-5: JIT via quorum, no PIM

**Decision.**
- D1 stands.
- JIT uses time-limited Identity Center account assignments with PT1H sessions, removed on expiry.
- `PlatformAdmin` needs two approvals. `SecurityResponder` allows one approval, or self-approval with mandatory review within 24 hours.
- Build both permission sets. D-007 closes when both work.

**Effect and checks.**
- **Removing an assignment on expiry is not automated remediation**, so the CLAUDE.md delete rule isn't engaged. It revokes access that was granted for a fixed term, and the next elevation recreates it. State that reasoning in the code comment, because an auditor will ask.
- **The review has no else branch yet.** "Self-approval with mandatory review within 24h" needs an explicit path for when the review doesn't happen. Recommend: an unreviewed self-approval at 24 hours raises a finding that pages through the router. Otherwise a missed review is invisible. See Q-3.
- **Live sessions outlast removal.** Removing an assignment doesn't end an active session; it lasts until `SessionDuration` ends. PT1H bounds that exposure. Record it.
- **Reuse the existing quorum.** The Jira-approval quorum in `quorum/` and design 03:110-114 is the approval engine. The two-approval rule for `PlatformAdmin` uses the same quorum membership.
- **The guard rules apply.** `SSO_PERMISSION_SET_NO_ADMINISTRATOR_ACCESS` (`baseline.guard:324-338`) applies to `PlatformAdmin` too, so it needs a scoped policy or the same kind of recorded exception as §6.3.

### 6.6 F-6: continuous Config recording everywhere

**Decision.** Continuous recording in every account and region, all tiers. Platform accounts use the existing prod mapping (`sync-account-tiers.sh:84`); record that choice.

**Effect.**
- 15.6 drops the frequency-by-tier axis entirely. There is no `tiers.yaml` change for recording.
- The management-account recorder is a plain stack (STOP).
- The platform-accounts-as-prod choice goes in the 15.6 report, and in a D- item only if it departs from design 14. It is an existing mapping, so a backlog note is probably enough.

### 6.7 F-7: Macie ceiling

**Decision.** The commercial owner sets the ceiling outside this session, as a ratio of the account's monthly AWS cost, stored in SSM. No figures in the repository or in session reports.

**Effect.**
- 15.6 reads the ratio from an SSM parameter under `/platform/`.
- It refuses to enable automated discovery when the parameter is **absent**, rather than defaulting. Absence means no one has decided.
- It reports "within" or "exceeds" only, never the figures.

### 6.8 F-8: no split-charge rules

**Decision.** No Cost Category split-charge rules. Shared-cost allocation is computed in the FinOps Athena views as an internal margin report (B-090), never used in invoicing. Design 10 is unchanged; record a D- item.

**Effect.**
- 15.9 step 5 becomes an Athena view.
- `report.py`'s "shared cost is absorbed, never allocated" (`:227-233`) stays true for invoices.
- **The D- item is about a report, not a billing change.** Design 10 says nothing about an internal allocation view. The D- item records that one exists and must never reach a tenant-facing output. B-126's testable-exclusion idea applies: assert in a test that no invoice path reads the view.

### 6.9 Other decisions

- **D-008 dropped; D-001 in 15.5a.** The management root email moves to a shared, access-controlled role mailbox. That is a management-account root change (STOP). The mailbox name goes in `config/contacts.env`, not the repository.
- **Cardholder tripwire.** A platform-scope rule, `cardholder_data_is_scope_change: true` in every tier, separate from `regulated_data_is_finding`. It lands in 15.6 as a new `tiers.yaml` attribute. It is mirrored by 15.10's vesting block.
- **B-110 option 2.** One copy per secret under the CMK, read cross-account. The order is consumer KMS grants first, re-encryption second (§1 15.8 item 3). Resource policies on the secrets grant the named consumer roles only. That is within the 15.4 RCP, since it is same-org.
- **Cost allocation tags.** Activate `Owner`, `Application` and `platform-layer`. Do not activate `DataClassification`. This resolves §1 15.9 item 4.
- **Platform change evidence.**
  - The workflow files a Jira change record before deploy, as the tenant path does. There is no second approval system.
  - Two-person control is branch protection (one approval from a non-author) plus environment approval with prevent-self-review.
  - Both settings are verified before they are relied on.
  - **Raises Q-4** (B-070).
- **Flow logs.** Keep "operational, not evidentiary", with a 400-day expiry, recorded as a D- item. Vesting raises it if a tenant contract requires more. 15.7 step 3 then becomes a lifecycle expiration plus a vesting rule in `derivation-rules.yaml`.
- **Archive replicas.** Add lifecycle transitions to Deep Archive (design 04:185), in 15.8.
  - A storage-class transition is permitted on COMPLIANCE-locked objects.
  - Deep Archive has a 180-day minimum storage charge. Set the transition day with that in mind.
  - This closes proposed backlog item 14 (§3.3).

### 6.10 Questions the decisions raise

| # | Question | Why it matters | Gates |
|---|---|---|---|
| Q-1 | Which bucket receives dumps? The only Object Lock bucket is the CloudTrail archive. The options are a prefix in `altdig-log-archive`, or a new dedicated locked bucket in Log Archive. | A prefix mixes content that log custody (B-134) and evidence collection read as CloudTrail. Any bucket policy change is SCP 02-protected and is a Security OU change. A dedicated bucket needs its own retention: 2190-day COMPLIANCE makes any redaction miss permanent. **Recommendation:** a dedicated bucket, GOVERNANCE mode or a short COMPLIANCE period, decided explicitly. | 15.1 |
| Q-2 | Which prompt widens the Tooling-owned baseline StackSets beyond Sandbox? Candidates: `platform-event-forwarding`, `platform-config`, `platform-kms-secrets`. | 15.5a (root/OAAR detection outside Sandbox), 15.6 (recorders everywhere) and 15.8 (platform-account CMK) can't meet their goals otherwise. **Recommendation:** 15.4 widens forwarding along with the SCP promotion; 15.6 and 15.8 widen their own StackSet. Each is a STOP. | 15.4, 15.5a, 15.6, 15.8 |
| Q-3 | What happens when a `SecurityResponder` self-approval isn't reviewed within 24 hours? | Without an else branch a missed review is invisible. **Recommendation:** a finding that pages through the router. | 15.5b |
| Q-4 | The platform's Jira change records would be filed by the Jira credential, which B-070 records as a personal identity. | Platform change evidence attributed to one person's identity is weak evidence. **Recommendation:** close B-070 (a real service account) before 15.3's workflow files records, or record the interim state in 15.3's D- item. | 15.3 |

### 6.11 Entra licensing (affects 15.5)

**Decision, as given.** The founders are confirming the tenant licence (Entra admin center > Overview).

- **If P1 is present or purchased:**
  - a Conditional Access policy requiring the phishing-resistant authentication strength for the AWS app;
  - sign-in log export to the Object Lock evidence bucket;
  - group-based assignment;
  - D1 recorded as narrowed: P1 funded, P2 still deferred.
- **If not:**
  - the authentication methods policy allows passkeys only for the AWS-access group;
  - Identity Center CloudTrail sign-in events are the access-evidence system of record, shipped to the evidence ledger;
  - both recorded as D- items against design 03.
- **Either way,** 15.5's JIT evidence must not depend on Entra audit logs being retained.

**The repository changes the premise.** Entra is **not federated into Identity Center yet.** D-010 (the Identity Center one, `deviations.md:695-731`) records that the identity source is still the **built-in Identity Center directory**. Its users and groups are created by `deploy-commercial-access.sh` from `config/identity.env`. Entra federation is listed under "Later" in the bootstrap runbook (`bootstrap-runbook.md:270`). So today:

- **There is no AWS enterprise app doing Identity Center sign-in.** The proposed test ("if group assignment to the AWS enterprise app works today, P1 is likely present") can't be run against AWS access, because nothing assigns users to such an app. The licence has to be read from the tenant itself. Group-based assignment to an enterprise app does need P1, so the test works in principle if some other enterprise app uses group assignment.
- **Neither branch has any effect until the Entra cutover happens.** Conditional Access and the Entra authentication methods policy govern Entra sign-in only, and AWS sign-in doesn't go through Entra yet.
- **The cutover itself is destructive.** D-010 records that switching the identity source "deletes every directory user and group and every assignment made to them". Permission sets survive. That is a lockout-class STOP, and it isn't scheduled in any Phase 15 prompt.

**What this means for 15.5.**

1. **JIT evidence no longer depends on Entra at all, under F-5.**
   - F-5's JIT is quorum-approved, time-limited Identity Center *account assignments*. The system of record is:
     - the quorum's own approval record (Jira, design 03:110-114);
     - Identity Center's `CreateAccountAssignment`/`DeleteAccountAssignment` CloudTrail events, made by the quorum pipeline role;
     - `sso` sign-in events.
   - All three are AWS-side and ship to the evidence ledger. That meets "must not depend on Entra audit logs" in both branches.
   - It supersedes §1 15.5 item 8, which assumed PIM and SCIM group-membership evidence.
2. **Break-glass (F-3) is unaffected.** It is management-account IAM users, outside both Entra and Identity Center.
3. **The Entra-side controls in either branch** (Conditional Access, or the passkey-only methods policy) are **gated on the cutover**.
   - Until then, the equivalent control lives in Identity Center: the built-in directory's MFA settings can require MFA on every sign-in and allow only security keys and built-in authenticators.
   - I believe that setting exists as described, but I haven't verified it against the current console. Confirm in the 15.5b session before relying on it.
   - Recommendation: configure it in 15.5b regardless of the licence answer. It is the only control that governs AWS sign-in today.
4. **The P1 branch's "sign-in log export to the Object Lock evidence bucket"** has the same unresolved destination as Q-1.
   - Entra exports through diagnostic settings to Azure Storage or Event Hubs, not directly to S3. That needs a transfer path, which is new build work.
   - Entra's own retention is short (I believe 7 days on Free and 30 on P1). That is the reason for "must not depend on Entra audit logs being retained".
   - **Unverified:** the retention figures, and whether diagnostic-settings export requires P1. Check both against current Microsoft documentation.
5. **The D- items differ by branch**, and both are recorded against design 03:
   - **P1:** D1 narrowed; Conditional Access with phishing-resistant strength. Its evidence is Entra-side until the export exists, so record that too.
   - **Not P1:** the passkey-only methods policy (Entra); Identity Center CloudTrail as the access-evidence system of record.

**What I could not do.** I didn't read the tenant licence: this session has no Entra access, and a read-only planning session shouldn't reach into the identity provider. The plan records the branches; the founders' reading of Entra admin center > Overview picks one.

### 6.12 Questions added by §6.11

| # | Question | Why it matters | Gates |
|---|---|---|---|
| Q-5 | When does the Entra → Identity Center cutover (D-010, Identity Center) happen, and in which prompt? | Every Entra-side control in §6.11 is inert until then. The cutover deletes all directory users, groups and assignments, including F-5's JIT assignments and the `CommercialReadOnly` group. **Recommendation:** its own session before 15.5b, with the permission sets redeployed from the existing templates (the group id is already a parameter for this reason), and the break-glass users (F-3) proven first as the recovery path. | 15.5b |
| Q-6 | The tenant licence reading (P1 or not). | It picks the §6.11 branch and its D- items. It does not block anything before the cutover. | Q-5 |

---

*Stop. No prompt after 15.0 has been run. No register has been edited. The
items in §3.3, and those §6 implies, are proposals. The prompts that create them
write them into `docs/build-backlog.md` and `docs/deviations.md`.*
