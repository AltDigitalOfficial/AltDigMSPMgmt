# PagerDuty

Terraform for the PagerDuty side of platform alerting. Design doc 13 calls this
"near-total" automation and asks for it to be treated like the AWS baseline —
versioned, staged, drift-detected. A manual console change to anything in here
is a finding, not a shortcut.

## What this creates

| Resource | Name | Why |
|---|---|---|
| Schedule | `AltDigital Platform On-Call` | Design 13 wants the escalation policy to reference a schedule. One person in it today. |
| Escalation policy | `AltDigital Platform Escalation` | Two rules: schedule, then every responder directly. |
| Service | `AltDigital Platform` | Carries the **platform's own** alarms, not any tenant's. |
| Integration | `AWS CloudWatch` | Vendor integration, so the incident title is the alarm name. |

**Users are looked up, not created.** They must already exist in PagerDuty. If
this configuration created them, `terraform destroy` would delete people.

## Running it

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit
../scripts/set-pagerduty-token.sh              # once; stores the API key
../scripts/pagerduty-apply.sh                  # plan
../scripts/pagerduty-apply.sh --apply
../scripts/sync-pagerduty-secrets.sh           # routing key -> Secrets Manager
```

`PAGERDUTY_TOKEN` no longer needs exporting. `pagerduty-apply.sh` reads the
API key from Secrets Manager (`platform/pagerduty/api-token`) and re-reads it
on every run, so rotation is one command and nothing downstream redeploys. An
explicit `PAGERDUTY_TOKEN` in the environment still wins, which is the path
for running a plan as a different or more restricted identity without
disturbing the stored value.

**Two different PagerDuty credentials live in Secrets Manager and they are not
interchangeable.** Confusing them produces errors that point nowhere near the
cause:

| | secret | direction |
|---|---|---|
| **API key** | `platform/pagerduty/api-token` | this repository → PagerDuty, to *manage* configuration |
| **Routing keys** | `platform/pagerduty/routing-key`, `-low` | alarms → PagerDuty, to *raise* incidents |

The API key is stored once by hand. The routing keys are Terraform *outputs* —
they exist only after an apply, which is why `sync-pagerduty-secrets.sh` runs
after it and not before.

Then redeploy `alerting/10-alert-topic.yaml` with
`PagerDutySecretName=platform/pagerduty/routing-key` so it creates the
subscription, and prove the whole path end to end:

```bash
../scripts/test-alert-path.sh \
  --alarm platform-replication-failed-flow-logs \
  --alarm-account altdig-security-logarchive
```

## Two things that will bite

**A wrong routing key looks exactly like a working one.** PagerDuty returns
`200` to the SNS subscription confirmation POST regardless, so the subscription
shows `Confirmed` and every page is then discarded. The only signal is that no
incident appears. This is why `test-alert-path.sh` asserts on the receiving
side rather than on the alarm firing.

**Rotating the key is a two-step operation and the second step is easy to
miss.** CloudFormation resolves `{{resolve:secretsmanager:...}}` at deploy
time into the subscription endpoint and never re-resolves it. Writing a new key
to Secrets Manager changes nothing until the alerting stack is redeployed.
`sync-pagerduty-secrets.sh` warns about this on update, because the failure
mode is silent and the window is however long it takes someone to notice no
pages have arrived.

## Why the platform service is separate from tenant services

Design doc 13 creates a PagerDuty service per application during onboarding.
This is not one of those. A tenant's application going down is that tenant's
incident; the evidence archive failing to replicate is AltDigital's. Routing
both to one service means the noisiest tenant buries the alarm saying the
platform can no longer prove anything about any tenant.

The per-application module is phase 12.2 and lands beside this, not inside it.

## State

Local, and gitignored twice — `.gitignore` and the pre-commit hook. State holds
integration keys in cleartext, which is not a Terraform defect: a routing key
*is* the credential, and anything that can create one can read it back.

Local state means one operator at a time and no locking. A remote backend
belongs in the Log Archive account with the same encryption and versioning as
everything else there — tracked as **B-016**, not done, because pointing at a
backend that does not exist fails `init` outright rather than degrading.
