# Management Account Bootstrap Runbook

Account `738815759702` · root email `msp-mgmt@altdigital.ai` · home region `us-east-2`

Two parts. **Part A** is console work as root — the things only root can do.
**Part B** is workstation work under federated credentials.

The ordering deliberately avoids ever creating an IAM user with static access
keys. An earlier draft of this runbook did that to get the CLI working; it was
the weakest credential in the whole bootstrap and it is gone.

---

# Part A — console, as root

## 1. Secure the root user ✅ *done 2026-09-16*

A brand-new management account has one credential: root. It has unlimited power
over every account that will ever sit beneath it, and it cannot be constrained
by an SCP or RCP.

- [x] Long unique generated password, stored in the password manager. There is
      no IAM password policy governing root — the only discipline is yours
- [x] Root email left unchanged. `msp-mgmt@altdigital.ai` is the base every
      member account's plus-addressed root email derives from
- [x] **No root access keys.** Verified empty. A root access key cannot be
      scoped, cannot be constrained by any policy, and cannot be attributed to a
      person. CloudFront key pairs and signing certificates also empty
- [x] MFA registered. Device name identifies the *device*, not the person. Two
      **consecutive** codes required — the same code twice fails with an
      unhelpful error
- [ ] **Second MFA device on separate hardware.** AWS permits up to 8 on root.
      **Open as of 2026-09-16** — pending Art's availability. Target end state
      is factors on Art's and Wayne's devices with the password held by Jamie,
      so no one person can sign in as root alone. Until then a single phone is
      the only factor, and its loss puts recovery through the root email, which
      is itself deviation D-001
- [ ] Record which physical devices hold the factors

> **Open deviation (D-006).** Root MFA is a virtual authenticator, not a
> hardware token, by deliberate decision. See [deviations.md](deviations.md).

## 2. Create the Organization ✅ *done 2026-09-16*

AWS Organizations console → **Create an organization** → all features.

- [x] Organization created
- [x] Confirm feature set is **ALL**, not consolidated billing only. Verified in
      step 7 — the bootstrap script refuses to continue otherwise, because SCPs
      and RCPs require all features

Everything else the Organization needs — policy types, trusted access, the
alias, SSM parameters — is done by script in step 7, not by hand.

## 3. Billing data access ✅ *done 2026-09-16*

Two separate things. Do 3a now; 3b is a verification that has to wait for
federated credentials.

### 3a — Enable Cost Explorer

Billing and Cost Management console → **Cost and Usage Analysis → Cost
Explorer**. Open it once. First access is what enables it, and AWS then takes
**up to 24 hours** to prepare the data. Doing it early means it is populated
when needed rather than discovering the lag at the point of use.

- [x] Opened

### 3b — IAM access to billing information

Historically this was a root-only toggle: *IAM user and role access to billing
information*, on the **Account** page (top-right account menu → Account — a
different console from Billing, which is where people look first). Without it,
no permission set could read Cost Explorer or the CUR **regardless of its IAM
policy**, blocking the `CommercialReadOnly` path and every per-client cost
report.

**It may not exist on this account.** AWS enables IAM billing access by default
for recently created accounts, and the 2023 migration to fine-grained billing
IAM actions (`billing:`, `ce:`, `cur:`, `payments:`, replacing `aws-portal:`)
made the toggle legacy. Do not spend time hunting for it.

Verify by behaviour instead, once step 5 gives you a federated role:

```bash
aws ce get-cost-and-usage \
  --region us-east-1 \
  --time-period Start=2026-09-01,End=2026-09-16 \
  --granularity MONTHLY --metrics UnblendedCost
```

The Cost Explorer API only lives in `us-east-1` regardless of the home region.

- Data returned, or an empty result on a new account → access is on
- `AccessDeniedException` → find and activate the toggle, and you will know it
  actually mattered

- [x] Verified after step 5

## 4. Enable IAM Identity Center ✅ *done 2026-09-16*

**Set the region selector to US East (Ohio) `us-east-2` before you enable it.**
The home region is whatever region you are in at the moment of enablement, and
it cannot be changed afterwards without deleting and recreating the instance —
which destroys every permission set and assignment with it.

- [x] Region selector reads `us-east-2`
- [x] IAM Identity Center → **Enable**. With an Organization present it enables
      organization-wide
- [x] **Customise the access portal URL** — Settings → Identity source →
      Actions → Customise. Set the subdomain (e.g. `altdigital`) so the portal
      is `https://altdigital.awsapps.com/start` rather than a `d-` string.
      One-time: changing it later invalidates every bookmark and SSO profile
- [x] Create a user for yourself in the Identity Center directory
- [x] Create permission set **`PlatformBootstrapAdmin`** — `AdministratorAccess`,
      session duration 1 hour
- [x] Assign it to yourself on account `738815759702` only
- [x] Sign in at the portal URL and confirm it works

> **Why `PlatformBootstrapAdmin` and not `PlatformAdmin`.** Design doc 03
> reserves `PlatformAdmin` for a JIT-only, time-boxed role with a stated reason
> and an elevation record. None of that machinery exists yet. Using the design's
> name for a standing admin grant would quietly redefine the control. This is a
> temporary, differently-named role that gets deleted when JIT elevation lands.
> See deviation D-007.

> **Identity source.** Starting on the built-in Identity Center directory is
> fine for one person, but know the cost: switching the identity source to Entra
> later **deletes the directory users and their assignments**. Permission sets
> survive. With one user that is a two-minute redo; do it before there is a team.

---

# Part B — workstation, under federated credentials

## 5. Configure the SSO profile ✅ *done 2026-09-16*

```bash
aws configure sso
```

| Prompt | Value |
|---|---|
| SSO session name | `altdigital` |
| SSO start URL | `https://altdigital.awsapps.com/start` |
| SSO region | `us-east-2` |
| SSO registration scopes | `sso:account:access` |
| Account | `738815759702` |
| Role | `PlatformBootstrapAdmin` |
| CLI default client Region | `us-east-2` |
| CLI default output format | `json` |
| CLI profile name | `msp-mgmt` |

```bash
export AWS_PROFILE=msp-mgmt
aws sts get-caller-identity
```

Expect an `assumed-role/AWSReservedSSO_PlatformBootstrapAdmin_*` ARN in account
`738815759702`. Re-authenticate any time with `aws sso login --profile msp-mgmt`.

- [x] `get-caller-identity` returns the expected account and an SSO role

## 6. Alternate contacts and mail verification ✅ *done 2026-09-16*

### 6a — Alternate contacts

```bash
cp config/contacts.env.example config/contacts.env
```

Gitignored and hard-blocked by the pre-commit hook: it holds names, direct
emails and phone numbers.

| Type | AWS sends | Holder |
|---|---|---|
| `BILLING` | Invoices, payment failures, billing notices | Tina |
| `OPERATIONS` | Service events, maintenance, operational advisories | Jamie |
| `SECURITY` | Abuse reports, security bulletins, vulnerability notices | Art |

Three different people deliberately — that is the D-001 mitigation, stopping
every AWS notice terminating in one personal mailbox. Phone numbers must be
E.164 (`+16125550123`). AWS validates email *format* only, never deliverability;
a typo is silent permanent loss of that notice stream.

- [x] Filled in

### 6b — Verify plus-addressed mail actually arrives

Every member account needs a unique root email, and the design plus-addresses
them all off one base:

```
msp-mgmt@altdigital.ai                          management account
msp-mgmt+ad-oeight-arc8-prod@altdigital.ai      member accounts
```

Exchange Online supports plus-addressing but it can be disabled org-wide, and it
behaves differently when the plus-address collides with a real recipient. Worth
two minutes now: a bounced root address during account creation is an ugly
failure, and the address is effectively permanent once set.

```powershell
Get-OrganizationConfig | Select-Object DisablePlusAddressInRecipients
```

`False` means plus-addressing is on.

- [x] Test message to `msp-mgmt+ad-test-test-dev@altdigital.ai` arrives — verified
      2026-09-16 from both an internal (`jamie@altdigital.ai`) and an external
      sender. External is the one that counts: AWS mail traverses connectors,
      transport rules and anti-spam that internal mail skips
- [x] Not silently filtered to Junk or a rule-driven folder

## 7. Run the bootstrap script ✅ *done 2026-09-16*

```bash
scripts/bootstrap-management-account.sh --dry-run
scripts/bootstrap-management-account.sh
```

Always dry-run first. Read-only calls still execute during a dry run, so the
report reflects real current state rather than an assumption.

| Step | Effect |
|---|---|
| Organization | Detects the one created in step 2; verifies feature set is `ALL` |
| Policy types | SCP, **RCP**, tag, backup, AI opt-out |
| Trusted access | 18 service principals — CloudTrail, Config, GuardDuty, Security Hub, Macie, Inspector, Access Analyzer, Backup, Identity Center, StackSets and others |
| Account alias | `altdigital-msp-mgmt` |
| Alternate contacts | From `config/contacts.env` |
| SSM | Org id, root id, home region, allowed regions, naming config |

Resource Control Policies are enabled deliberately. They evaluate on the
resource side regardless of which principal acts — a stronger instrument than an
SCP for open item V1, *"a member account with full local administrator cannot
remove detective controls."* The policies themselves come in phase 1.2.

- [x] Dry run reviewed
- [x] Applied

## 8. Deploy the OU skeleton ✅ *done 2026-09-16*

```bash
scripts/deploy-org-structure.sh --dry-run    # produces a real changeset
scripts/deploy-org-structure.sh
```

```
Root
├── Security          Log Archive, Audit, Forensics accounts land here
├── Infrastructure    Platform Tooling, Shared Services, Backup, Network
├── Members
│   └── direct        AltDigital's own clients, no partner
└── Sandbox           baseline canary, guardrail test target
```

Partner and client OUs are **not** in this stack. They are created per partner
and per client so a change to one never touches another.

- [x] Deployed

---

# Later — not part of bootstrap

## Entra federation

Identity Center → Settings → Identity source → change to **External identity
provider**. SAML for authentication, SCIM for provisioning, so joiners and
leavers flow automatically and deprovisioning happens in one place.

Remember this **deletes the Identity Center directory users and their
assignments**. Permission sets survive. Do it while the user count is one.

Design doc 03 is the reference: no standing administrative access, JIT elevation
for `PlatformAdmin` and `SecurityResponder`, break-glass outside the federation
path. This step establishes the federation path only — the elevation machinery
is a later phase, and `PlatformBootstrapAdmin` survives until it exists.

---

## Verification

Confirm state independently of the scripts:

```bash
aws organizations describe-organization \
  --query 'Organization.[Id,FeatureSet,MasterAccountId]' --output table

aws organizations list-roots \
  --query 'Roots[0].PolicyTypes' --output table

aws organizations list-organizational-units-for-parent \
  --parent-id "$(aws ssm get-parameter --name /platform/org/root-id \
    --query Parameter.Value --output text)" \
  --query 'OrganizationalUnits[].[Name,Id]' --output table

aws ssm get-parameters-by-path --path /platform --recursive \
  --query 'Parameters[].[Name,Value]' --output table
```

Expect: feature set `ALL`, five policy types `ENABLED`, four OUs under root, and
a populated `/platform/` namespace.

---

## What this does not do

Stated plainly so the gap stays visible rather than being assumed closed:

- **No SCPs or RCPs.** The policy types are enabled; no policy is attached.
  Nothing is being prevented yet. Phase 1.2
- **No guardrail test harness.** Open item V1 is untested. Phase 1.3 — and per
  the ordering note, **no account vesting until it passes**
- **No accounts.** Log Archive, Audit, Forensics, Platform Tooling, Shared
  Services and Backup do not exist
- **No detective controls.** No CloudTrail org trail, no Config, no GuardDuty
- **No Cost Categories.** The OU tree makes the billing roll-up derivable; the
  derivation itself is phase 9.2
- **No registry**, so the partner precondition in `scripts/create-client-ou.sh`
  warns where design doc 13 requires it to block (D-004)
