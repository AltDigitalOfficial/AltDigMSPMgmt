# 11 — Developer Access

Applies to developers working on member applications, whether AltDigital staff or
customer staff. The controls are identical; only the consequence of violation
differs (see [Prod data in dev](#prod-data-in-dev)).

**Governing position:** a developer may do broadly what they like in dev, may
not act directly in test or production, and may never modify the controls that
support the environment — in any environment.

---

## Three layers

Any one layer leaks. All three together hold.

### Layer 1 — SCP: what nobody in the account can do

Applies to every principal in the member account **including account-local
administrators**. Extends the control-protection SCP in
[04](04-security-controls-and-evidence.md):

- Config recorder, delivery channel, conformance packs
- CloudTrail configuration and log destinations
- Platform-managed alarms and SSM associations
- KMS keys and key policies
- Backup vaults, plans, vault lock
- Platform IAM roles (containment, deployer, instrumentation)
- Leaving the Organization

Excluded by `aws:PrincipalArn` for platform roles only.

### Layer 2 — Permissions boundary: they cannot escalate out

**The layer most often forgotten.** A developer able to create IAM roles — which
in dev they should be — can otherwise create a role more privileged than
themselves and pass it to a service.

- SCP condition on `iam:CreateRole` and `iam:PutRolePolicy` requiring a
  platform-managed permissions boundary be attached
- **No boundary, no role**
- The boundary carries the same denies as Layer 1

### Layer 3 — Permission set: the day-to-day grant

Broad in dev, narrow in test, minimal in production. See the
[environment gradient](#environment-gradient).

---

## Read-only on platform controls is deliberate

Developers **can see** their Config rules, alarms, findings, flow logs, and the
state of every control operating on their account. They cannot modify any of it.

This is a design choice, not an oversight:

- It makes developers better at self-diagnosis
- It reduces tickets to the AltDigital team — which matters given minimal staffing
- It removes the "invisible black box breaking my stuff" dynamic

**Present it positively in developer-facing documentation** — *here is everything
you can see about how your account is monitored* — because it converts a
restriction into a feature.

---

## Bypass controls

Disabling controls is the obvious risk. **Evading them is the likelier one**, and
is usually accidental.

| Bypass | Control |
|---|---|
| **Region sprawl** — resources created where no recorder, GuardDuty or alarms exist | SCP denying all regions except the approved set |
| **Log groups without data protection** | Account-level data protection policy; verified, because this is the stdout control |
| **Networking escaping the egress profile** | Deny VPC peering, Transit Gateway attachment, IGW/NAT creation outside the baseline VPC |
| **Cross-account trust** — a role trusting an external principal is an exfiltration path with no alarm | Deny creating roles with external principals in the trust policy |
| **Cost- or scope-heavy service enablement** (Bedrock, SageMaker, etc.) | Not denied — but surfaces in the digest and triggers a budget alarm |

Region sprawl is the one to prioritise. It is entirely innocent in intent and
produces a total visibility blind spot.

---

## Environment gradient

| | dev | test | uat | prod |
|---|---|---|---|---|
| Console access | Full within guardrails | Read-only + limited ops | Read only | **Read + observe (see below)** |
| Create / delete resources | Yes | Via pipeline | Via pipeline | Via pipeline |
| IAM role creation | Yes, boundary enforced | No | No | No |
| Path to change | Direct | `AppDeployer` | `AppDeployer` | `AppDeployer` |
| Change reconciliation meaningful? | No | Yes | Yes | Yes |

**UAT is treated as production for access purposes**, because it holds real
data — see [14 — Environment Tiers](14-environment-tiers.md).

Dev is expected to be chaotic. That is acceptable because it is isolated by the
account boundary and holds no regulated data.

---

## Production visibility

The initial instinct was **logs and service health only** — *"if you want more
information, write better logging."*

The principle is right for application behaviour and wrong for infrastructure
state. Three reasons:

**1. It fails where it matters most.** The failure modes where an application
*cannot* log: OOM kill (process is gone), task evicted by the scheduler, health
check failing before startup completed, container exiting on a non-zero code,
connection pool exhausted at the proxy. The ECS stopped reason and exit code are
the entire story, and no amount of better logging produces them.

**2. The timing is backwards.** Improving logging requires a code change, a
deployment and a reproduction. During an incident, none are available. The
restriction bites hardest when it is most expensive.

**3. The compliance argument runs the opposite way.** Logs are the *most*
sensitive artifact — they are where PHI and cardholder data actually leak, which
is why data protection policies mask them at write time. Metrics, stopped
reasons, task definition revisions, deployment history and traces carry almost
no payload data. Granting logs while withholding those grants the risky thing
and withholds the safe things.

### Production read access — granted

| Access | Rationale |
|---|---|
| CloudWatch logs (their application) | As originally intended |
| CloudWatch metrics — all, not a health roll-up | Latency distribution and saturation cannot be reconstructed from logs |
| ECS task stopped reasons, exit codes, event stream | The failure modes where the app cannot log |
| Running configuration — task definition revision, env var **keys**, parameter **names** | "What is actually deployed" is unanswerable from logs |
| Deployment and change history | Our own runbooks make this the first question asked |
| X-Ray / Application Signals traces | Not practically reconstructible from logs |
| Their own alarms and alarm state | They should see what wakes us up on their behalf |

### Production — denied, and held firm

- **Any write action**
- **Any data plane access** — no reading S3 objects, no querying the database, no
  retrieving Secrets Manager values
- Environment variable **values**, parameter **values**
- Any visibility into another tenant's accounts

The restriction on *doing* stays absolute. Only the restriction on *seeing*
relaxes.

### Keep the standard

*"Write better logging"* remains the right answer — attached to a standard rather
than used as a refusal. Structured JSON, correlation IDs, configuration logged at
startup, log levels adjustable without redeploy. That converts a refusal into a
requirement and improves the thing being complained about.

---

## Detecting loss of visibility

A developer with root on their own EC2 instance can stop the CloudWatch agent.
No SCP prevents this. **It is detectable**, because the signal is not absence of
data — it is *absence of data from something demonstrably alive.*

### Signal dependencies

| Signal | Requires the agent? |
|---|---|
| EC2 status checks (hypervisor level) | No |
| EC2 state-change events | No |
| VPC flow logs | No |
| SSM ping status | SSM agent only |
| CloudWatch metrics + log ingestion | CloudWatch agent |

### Discriminating the cases

| Scenario | Status checks | Flow logs | SSM ping | CW telemetry |
|---|---|---|---|---|
| Instance terminated / stopped | State event | Stops | Lost | Stops |
| OS hung or crashed | **Fail** | Stops | Lost | Stops |
| Network or IAM path broken | Pass | Traffic | Lost | Stops |
| **CloudWatch agent killed** | **Pass** | **Traffic** | Pass | **Stops** |
| **Both agents killed** | **Pass** | **Traffic** | **Lost** | **Stops** |

Instance provably alive and passing traffic, telemetry silent. Nothing else
produces that signature.

### Implementation

- **Missing-data alarms** with `treatMissingData: breaching` on an agent-published
  metric. `mem_used_percent` is a good sentinel — it exists only if the agent runs.
- **Log stream staleness check** on `lastIngestionTime` per expected stream.
  CloudWatch Logs does not alarm on absence natively, so this is a scheduled check.
- **Expectation registry** — absence is only detectable against a declared
  expectation of what should be publishing. This comes from the instrumentation
  layer ([06](06-observability.md)) and is the same completeness pattern as the
  Truveon heartbeat.

### Attribution

- **Session Manager sessions are CloudTrail events.** `StartSession` carries
  principal, instance and timestamp — so a correlation rule (session started,
  telemetry ceased within N minutes, instance still alive) gives an attributed
  finding.
- **SSH is not.** OS auth logs ship via the agent; if the agent died, the login
  evidence died with it. Only VPC flow logs remain, showing a session rather than
  an identity.
- **Therefore:** there is no SSH path in this architecture. Inbound on 22
  appearing in flow logs is a finding in its own right, independent of the
  telemetry question, and is alarmed directly.

### Avoiding noise

Agents restart; instances reboot during patching.

- **Recognise scheduled-down state explicitly.** An environment stopped by its
  HOOP must be a known state, not left to look like an anomaly — otherwise every
  turndown is a false positive. See [14](14-environment-tiers.md).
- Suppress during maintenance windows
- Grace period longer than a normal agent restart
- **Escalate against the SSM association** — the association reinstalls on
  schedule, so a gap that *survives the next association run* is the real signal
- Immediate low-severity notice; page only on persistence or recurrence
- **In dev, this is a digest line and a manager notification, not a page.** It is
  a visibility failure, not an outage.

---

## Prod data in dev

### Detection — identical regardless of whose developers

- **Macie in dev exactly as in production.** This is why running it in
  non-production matters.
- The API calls that move production data sideways, which are **denied at SCP
  level** rather than merely detected — none has a legitimate use in this
  architecture, because promotion happens through the deployer role and
  artifacts, never through cloned data:
  - `ModifyDBSnapshotAttribute` — RDS snapshot sharing
  - `ModifySnapshotAttribute` — EBS
  - `ModifyImageAttribute` — AMI sharing
  - S3 replication or bucket policy granting a dev-account principal

### What no control catches

A developer with legitimate read access to production dumping a table and
loading it into dev. Macie in dev is the backstop.

### AltDigital developers

Enforced through privileges, monitoring, and corrective action. The detection
above supplies the evidence.

### Customer developers

The useful framing is that this is **not a policy violation — it is a scope
change.**

Regulated data in a **dev or test** account means that account is now in audit
scope and must be treated as production-equivalent — full retention, full
controls, full cost — until remediated and attested.

> **UAT is excluded from this.** Regulated data in UAT is expected, not a scope
> change — UAT is provisioned in compliance scope from the outset precisely
> because real users bring real data. See [14](14-environment-tiers.md).

That is both the honest technical position and considerably more persuasive than
*"please don't"*, because it arrives with a price attached.

**Procedure:** finding raised → customer notified with timestamp → remediation
clock running → if unremediated, the account's posture is **upgraded** rather
than the finding left open. Same pattern as the CVE SLA in
[09](09-vulnerability-and-patching.md).

The notification record is what protects AltDigital if it ever matters.

Requires an explicit intake acknowledgement and a line in the
[Responsibility Matrix](../intake/responsibility-matrix.md).

---

## Manager digest

Extends the instrumentation digest in [06](06-observability.md) rather than
adding a second email — same batching discipline, or it is filtered within a week.

Beyond "resources appeared", the dev-team version carries:

- **Services newly enabled in the account** — the line a manager actually reacts
  to, because it is where surprise spend and surprise compliance scope both
  originate
- Roles created, and what they can reach
- Anything that failed a guardrail
- Running cost against budget
- Telemetry gaps (agent stopped, log stream stale)