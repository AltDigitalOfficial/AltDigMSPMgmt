"""The go-live gate. Prompt 14.3, design 13 step 9, design 15.

Why the partner is checked twice
----------------------------------
design 15: "Partner state is checked at client go-live as well as at client
provisioning start, because a partner relationship can lapse between the two."

Step 0 checks the partner before anything is built. That check can be weeks old
by the time a tenant is ready to go live -- the saga blocks on human tasks, a
verification sweep and a signature, and none of those is fast. In between, a
partner's BAA can expire, their contract can lapse, or their downstream
commitments can change because they signed a new client.

Re-checking is cheap. The failure it prevents is an account that is live,
billing, in the on-call rotation and SLA-measured, beneath a partner
relationship with no defensible notification path. design 15 calls that a
failure mode in its own right rather than a stale record.

What go-live actually starts, and why nothing here approves it
----------------------------------------------------------------
design 13: three things key off the transition to `live` -- SLA measurement
starts, billing starts, and the account enters the on-call rotation.

So this function never decides. It requires `go_live_approved_by` to already be
on the record, put there by a human, and its whole job is to refuse when
something else is not true. An automated path from a green verification sweep
to `live` would mean those three commitments beginning because a check passed,
which is the gate lost to a tick.

The order of the refusals is deliberate
-----------------------------------------
The partner is checked FIRST, before the tenant's own readiness. A tenant that
is perfectly built beneath a lapsed partner should be told about the partner,
not about itself -- reporting "verification incomplete" when the real problem
is an expired BAA sends somebody to read the wrong logs.
"""
import datetime
import json
import os

import boto3

import saga

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")


class NotReady(Exception):
    """Go-live refused, with the reason. Not a failure."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(when):
    return when.astimezone(datetime.timezone.utc).isoformat()


def partner_record(slug):
    if not slug:
        return None
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"PARTNER#{slug}"}, "SK": {"S": "META"}})
    item = got.get("Item")
    if not item:
        return None
    return {k: (v.get("S") or v.get("N") or v.get("BOOL"))
            for k, v in item.items()}


def check_partner(slug):
    """The step 0 check, re-run, plus the back-to-back ordering.

    Not identical to step 0 deliberately. Step 0 asks whether a partner is fit
    to build under; this asks whether it is fit to go LIVE under, which is a
    higher bar because the notification chain becomes real at that moment.
    """
    record = partner_record(slug)

    if record is None:
        return {"ok": False, "partner": slug,
                "why": f"partner '{slug}' has no registry record. It existed "
                       "at provisioning start or step 0 would have blocked, so "
                       "it has been removed since -- which is not a condition "
                       "to go live under."}

    state = record.get("state")
    if state != "active":
        return {"ok": False, "partner": slug, "state": state,
                "why": f"partner '{slug}' is '{state}', not active. design 15 "
                       "re-checks at go-live precisely because a relationship "
                       "can lapse between provisioning and this moment."}

    # The ordering, now that both numbers matter. A partner who signed a new
    # client since step 0 may have tightened their downstream commitment.
    import backtoback
    b2b = backtoback.verify(record)
    if b2b["verdict"] == backtoback.REFUSE:
        failing = [r for r in b2b["results"] if r["verdict"] == backtoback.REFUSE]
        return {"ok": False, "partner": slug, "back_to_back": b2b,
                "why": "the notification chain cannot be satisfied: "
                       + " ".join(f["why"] for f in failing)}

    return {"ok": True, "partner": slug, "state": state,
            "back_to_back": b2b["verdict"],
            "why": f"partner '{slug}' is active and its notification chain is "
                   "satisfiable."
                   + (" Commercial findings are flagged and do not block "
                      "go-live." if b2b["verdict"] == backtoback.FLAG else "")}


def assess(tenant_id):
    """Everything that must be true, checked in the order a reader needs."""
    record = saga.record(tenant_id)
    if not record:
        raise NotReady(f"no registry record for {tenant_id}.")

    checks = []

    # 1. The partner, first. See the module docstring on why.
    partner = check_partner(record.get("partner"))
    checks.append({"check": "partner", **partner})

    # 2. The tenant's own state.
    state = record.get("state")
    checks.append({
        "check": "state", "ok": state == "verifying", "state": state,
        "why": ("the tenant is 'verifying' and may transition to live."
                if state == "verifying" else
                f"the tenant is '{state}'. design 13 allows live only from "
                "'verifying', which is reached by passing the verification "
                "sweep.")})

    # 3. The sweep.
    verified = record.get("verified_at")
    checks.append({
        "check": "verified", "ok": bool(verified), "verified_at": verified,
        "why": ("the verification sweep passed and stamped the record."
                if verified else
                "the record carries no verified_at, so the verification sweep "
                "has not passed. design 13 fails closed: an undeterminable "
                "check has not passed.")})

    # 4. The human. Last, because it is the one thing this function will not
    #    supply for itself, and listing it last makes that visible.
    approver = record.get("go_live_approved_by")
    checks.append({
        "check": "approved", "ok": bool(approver), "approved_by": approver,
        "why": (f"go-live approved by {approver}."
                if approver else
                "nobody has approved go-live. design 13 makes this a blocking "
                "human task owned by Jamie, and three things start on this "
                "transition: SLA measurement, billing, and the on-call "
                "rotation. Nothing here approves it.")})

    # 5. Two people, not one. B12 / prompt 14.6, entity-blind by Jamie's
    #    decision of 2026-09-22. The requester is whoever owns the dev team,
    #    which is deliberately never the same person as the MSP-side approver.
    import separation
    sep = separation.check("go-live",
                           record.get("go_live_requested_by"), approver,
                           names=("go_live_requested_by",
                                  "go_live_approved_by"))
    checks.append({"check": "separation",
                   "ok": sep["verdict"] == separation.CLEAR,
                   "why": sep["why"]})

    return record, checks


def run(event, context=None):
    """Transition verifying -> live, or refuse and say which check failed."""
    tenant_id = (event or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("golive needs tenant_id")

    record, checks = assess(tenant_id)
    failed = [c for c in checks if not c["ok"]]

    out = {"record_type": "go-live-gate", "tenant_id": tenant_id,
           "assessed_at": _iso(_now()),
           # Always all four, passed or not. A gate that reported only its
           # first failure would be re-run four times to learn four things.
           "checks": checks,
           "blocked_by": [c["check"] for c in failed],
           "went_live": False}

    if failed:
        # A partner failure is the one worth emitting, because it is the one
        # nobody is watching for. The other three are states somebody is
        # actively working through.
        if any(c["check"] == "partner" for c in failed):
            events_client.put_events(Entries=[{
                "EventBusName": ALERT_BUS_NAME,
                "Source": "platform.provisioning",
                "DetailType": "go-live-blocked",
                "Detail": json.dumps({
                    "record_type": "go-live-finding",
                    "recorded_at": _iso(_now()),
                    "current_state": "ALARM",
                    "routing": "digest_and_jira",
                    "alarm": {"name": "platform-go-live-partner-lapsed",
                              "resource_id": tenant_id},
                    "assessment": {"probable_cause":
                                   "a tenant reached go-live beneath a partner "
                                   "relationship that has changed since "
                                   "provisioning began."},
                    "go_live": out,
                }, default=str),
            }])
        print(json.dumps(out, default=str))
        return out

    saga.transition(tenant_id, "live",
                    f"go-live approved by {record.get('go_live_approved_by')}; "
                    "partner re-checked at the gate")
    out["went_live"] = True
    out["state"] = "live"
    print(json.dumps(out, default=str))
    return out


def handler(event, context=None):
    return run(event, context)
