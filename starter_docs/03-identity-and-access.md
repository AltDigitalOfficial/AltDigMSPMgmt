# 03 — Identity and Access

## Model

**Entra ID → IAM Identity Center → permission sets → member accounts.**

**One directory.** AltDigital staff who also hold partner addresses (e.g.
`@oeight.io`) have a **single Entra identity** with the partner address as a
secondary SMTP alias. There is no second identity system and no separate partner
staff to federate — see [15 — Partner Model](15-partner-model.md).

- Entra ID is the source of truth for AltDigital staff identity
- SAML for authentication, SCIM for provisioning — joiners and leavers flow
  automatically, deprovisioning happens in one place
- IAM Identity Center is the single front door to every AWS account
- Permission sets are defined centrally and assigned to Entra groups; access is
  granted by group membership, never per-account configuration

**No IAM users in member accounts. Ever.** No long-lived access keys for human
principals.

---

## No standing administrative access

Day-to-day, engineers hold **read-only** or a constrained **operator** role.
Administrative access is obtained through **just-in-time elevation**: a stated
reason, a time box, and an automatic expiry.

The elevation event is itself evidence — it flows to Truveon with requester,
reason, duration, target account and approval.

### Role tiers

| Role | Standing? | Scope |
|---|---|---|
| `PlatformReadOnly` | Yes | All member accounts, read-only |
| `PlatformOperator` | Yes | Restart, scale, run approved SSM documents; no IAM, no KMS, no deletion |
| `PlatformAdmin` | JIT only | Full account admin, time-boxed |
| `SecurityResponder` | JIT only | Containment actions, forensics account access |
| `BaselineDeployer` | Pipeline only | StackSet execution; not assumable by humans |
| `CommercialReadOnly` | Yes | Billing and cost data only; **no member account access** — see [12](12-commercial-access.md) |
| `DeveloperDev` / `DeveloperProd` | Yes | Scoped per environment — see [11](11-developer-access.md) |
| `ProvisioningOrchestrator` | Pipeline only | Cross-system onboarding; service-account credentials — see [13](13-client-provisioning.md) |

---

## Break-glass

Emergency access must not depend on Entra ID. If Entra is unavailable or
compromised, we still need a way in.

- One or two emergency accounts in AWS itself, outside the federation path
- Hardware MFA tokens, credentials split and stored physically
- **Any authentication attempt pages immediately**, successful or not
- Use requires a post-incident review and a Truveon record
- Tested on a schedule — an untested break-glass path is not a path

---

## Microsoft Entra P2

Not required at present. Identity Center provides the elevation and time-boxing
where it matters, and Truveon holds access review evidence.

**Revisit when:** AltDigital begins managing identities for member organisations
rather than only its own staff. At that point manual access review stops
scaling and PIM / access reviews earn their cost.

---

## Customer access

Member organisation staff do not receive access to AltDigital's Identity Center.
Where a customer requires console or API access to their own account (typical
under Model A), it is provisioned as a federated path from *their* identity
provider into *their* accounts only, with a permission set that excludes:

- Modification or deletion of platform-managed resources
- CloudTrail, Config recorder, or log destination configuration
- KMS key deletion or key policy modification
- Backup vault or vault lock configuration

Enforced by SCP, not by permission set alone. See
[04 — Security Controls](04-security-controls-and-evidence.md) and
[11 — Developer Access](11-developer-access.md), which covers the developer case
in full including the permissions boundary requirement.

---

## Quorum-controlled actions

The following require **two approvals from {Jamie, Wayne, Art}**, with requester
and approvers being distinct people:

- `kms:ScheduleKeyDeletion`
- Backup vault deletion or vault lock modification
- Removal of a member account from the Organization
- Disabling CloudTrail or the Config recorder

**Dual-hatted staff.** The quorum above is an **intra-AltDigital** control and is
unaffected by the partner layer — all three act as AltDigital.

Where a control claims separation between AltDigital and a partner, one
individual acting for both entities is **not** two-party control. For any such
action, the requesting and approving individuals must be different people
regardless of which entity they act for. See
[15 — Partner Model](15-partner-model.md).

These are denied outright in the relevant resource policies and SCPs. The only
execution path is a platform pipeline that verifies two recorded approvals
before assuming a permitted role.

**Approval workflow lives in Jira. Execution and outcome are recorded in
CloudTrail. Truveon holds both and reconciles them.** See
[08 — Change & Release](08-change-and-release.md) for the verification detail.

Any attempt against these actions — successful or denied — pages immediately.
A denied attempt is either confusion or something worse; both warrant knowing
within minutes.
