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
export PAGERDUTY_TOKEN=...                     # not a variable, not a file
../scripts/pagerduty-apply.sh                  # plan
../scripts/pagerduty-apply.sh --apply
../scripts/sync-pagerduty-secrets.sh           # key -> Secrets Manager
```

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
