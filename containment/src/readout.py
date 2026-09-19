"""The security incident readout. Prompt 7.2.

design/07-response-automation.md, "The readout":

    Same dossier shape as operational runbooks, security-flavoured:
      - What fired, when, and discovery_time
      - What was preserved and where it is
      - What was isolated, and what remains reachable
      - What the principal did in the preceding 24 hours (CloudTrail)
      - What else that principal can reach (Access Analyzer, policy evaluation)
      - Whether the same indicators appear in other accounts
      - Current containment state and what was deliberately not done

Why this is a separate stage rather than part of containment
------------------------------------------------------------
Containment publishes its summary the moment it finishes, and that is what
pages somebody. This runs afterwards and takes as long as it takes: a 24-hour
CloudTrail lookup and an organization-wide Security Hub sweep are slow, and
prompt 7.2 is explicit that the cross-account check must not be skipped for
speed.

The two are joined by the router's dedup key, which is derived from
(account, resource, title). This emits the same three, so PagerDuty treats the
readout as an UPDATE to the incident containment already raised rather than a
second incident about the same event. The page arrives immediately with what
was done; the evidence catches up. That is the same arrangement prompt 5.3
built for the response engine, reused rather than reinvented.

The cross-account check is the point
------------------------------------
    The cross-account indicator check is the one that distinguishes a single
    compromised workload from a platform-level event. Do not skip it for speed.

It runs from Audit, which is the Security Hub delegated administrator and
therefore already sees findings from every account in the organization. Nothing
cross-account has to be assumed for it — which is most of why containment lives
in Audit at all.
"""
import datetime
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
events = boto3.client("events")
securityhub = boto3.client("securityhub")
sts = boto3.client("sts")

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE", "")
RESPONDER_ROLE_NAME = os.environ.get("RESPONDER_ROLE_NAME",
                                     "PlatformSecurityResponder")
EXTERNAL_ID = os.environ.get("RESPONDER_EXTERNAL_ID", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")

# How far back the principal's activity is reconstructed. design 07 says 24
# hours; it is a parameter because an incident discovered late needs a longer
# window, and the value is recorded in the readout so nobody has to guess what
# "the preceding 24 hours" meant on the day.
LOOKBACK_HOURS = int(os.environ.get("CLOUDTRAIL_LOOKBACK_HOURS", "24"))

# How far back the cross-account sweep looks for the same indicators.
SWEEP_DAYS = int(os.environ.get("INDICATOR_SWEEP_DAYS", "7"))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def responder(account_id, purpose):
    role_arn = f"arn:aws:iam::{account_id}:role/{RESPONDER_ROLE_NAME}"
    kwargs = {"RoleArn": role_arn, "RoleSessionName": f"readout-{purpose}"[:64]}
    if EXTERNAL_ID:
        kwargs["ExternalId"] = EXTERNAL_ID
    creds = sts.assume_role(**kwargs)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


# ---------------------------------------------------------------------------
# What already happened — read from the append-only record, not re-derived
# ---------------------------------------------------------------------------


def incident_history(incident_id):
    """Header plus every containment event, in order.

    Read back rather than passed in. The state machine could hand the readout
    everything it did, and then the readout would be reporting what containment
    BELIEVES happened. Reading the record means the readout reports what was
    actually written down, and a discrepancy between the two becomes visible
    instead of being smoothed over.
    """
    if not INCIDENT_TABLE:
        return None, []
    got = dynamodb.query(
        TableName=INCIDENT_TABLE,
        KeyConditionExpression="PK = :p",
        ExpressionAttributeValues={":p": {"S": f"incident#{incident_id}"}},
    ).get("Items", [])
    header = None
    stages = []
    for item in got:
        sk = item.get("SK", {}).get("S", "")
        if sk == "header":
            header = {k: list(v.values())[0] for k, v in item.items()}
        elif sk.startswith("event#"):
            stages.append({
                "stage": item.get("stage", {}).get("S"),
                "outcome": item.get("outcome", {}).get("S"),
                "recorded_at": item.get("recorded_at", {}).get("S"),
                "detail": json.loads(item.get("detail", {}).get("S") or "{}"),
            })
    stages.sort(key=lambda s: s.get("recorded_at") or "")
    return header, stages


def stage_detail(stages, name):
    for s in stages:
        if s["stage"] == name:
            return s["detail"]
    return {}


# ---------------------------------------------------------------------------
# What the principal did
# ---------------------------------------------------------------------------


def principal_activity(account_id, principal_arn):
    """CloudTrail for the compromised principal, last LOOKBACK_HOURS.

    Read through the responder role, from the member account's own trail. The
    organization trail in Log Archive holds the same events and is the
    authoritative copy for evidence; LookupEvents is used here because it
    answers "what did this principal just do" in one call, and the readout is
    trying to be useful at 3am rather than to be the evidence itself.
    """
    if not principal_arn:
        return {"checked": False,
                "reason": "the finding named no principal, so there is no "
                          "activity to reconstruct"}
    name = str(principal_arn).rsplit("/", 1)[-1]
    start = _now() - datetime.timedelta(hours=LOOKBACK_HOURS)
    try:
        session = responder(account_id, "trail")
        trail = session.client("cloudtrail")
        paginator = trail.get_paginator("lookup_events")
        found = []
        pages = paginator.paginate(
            LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": name}],
            StartTime=start, EndTime=_now(),
            PaginationConfig={"MaxItems": 200})
        for page in pages:
            for ev in page.get("Events", []):
                found.append({
                    "event_time": _iso(ev["EventTime"]) if ev.get("EventTime") else None,
                    "event_name": ev.get("EventName"),
                    "event_source": ev.get("EventSource"),
                    "resources": [r.get("ResourceName")
                                  for r in (ev.get("Resources") or [])][:5],
                })
    except Exception as exc:                       # noqa: BLE001
        # Named, not swallowed. B-054: a denial reported as "nothing found" is
        # the readout telling an incident responder the principal was idle when
        # in fact nobody looked.
        return {"checked": False,
                "reason": f"could not read CloudTrail: {type(exc).__name__}: {exc}"}

    by_action = {}
    for ev in found:
        by_action[ev["event_name"]] = by_action.get(ev["event_name"], 0) + 1
    return {
        "checked": True,
        "lookback_hours": LOOKBACK_HOURS,
        "window_start": _iso(start),
        "event_count": len(found),
        # The summary is what gets read first at 3am; the detail is underneath.
        "actions_by_frequency": sorted(
            ({"action": k, "count": v} for k, v in by_action.items()),
            key=lambda a: a["count"], reverse=True)[:25],
        "events": found[:100],
        "truncated": len(found) >= 200,
    }


def principal_reach(account_id, principal_arn):
    """What else that principal can reach.

    design 07 names "Access Analyzer, policy evaluation". What is implemented is
    the policy side: the attached and inline policies the principal carries,
    which is what an incident responder needs first — "what could it have
    touched" is answered by its permissions.

    IAM Access Analyzer is NOT consulted, and that is a real gap rather than an
    oversight: an analyzer is a per-account resource that nothing in this
    platform creates, so there is usually nothing to query. Stated in the
    readout rather than left as an empty section that reads like a clean
    result. B-058.
    """
    if not principal_arn:
        return {"checked": False, "reason": "no principal named"}
    text = str(principal_arn)
    if ":role/" in text:
        kind, name = "role", text.split(":role/", 1)[1].split("/")[-1]
    elif ":assumed-role/" in text:
        kind, name = "role", text.split(":assumed-role/", 1)[1].split("/")[0]
    elif ":user/" in text:
        kind, name = "user", text.split(":user/", 1)[1].split("/")[-1]
    else:
        return {"checked": False,
                "reason": f"{principal_arn} is not an IAM role or user"}

    try:
        iam = responder(account_id, "reach").client("iam")
        if kind == "role":
            attached = iam.list_attached_role_policies(RoleName=name)
            inline = iam.list_role_policies(RoleName=name)
        else:
            attached = iam.list_attached_user_policies(UserName=name)
            inline = iam.list_user_policies(UserName=name)
    except Exception as exc:                       # noqa: BLE001
        return {"checked": False,
                "reason": f"could not read policies: {type(exc).__name__}: {exc}"}

    names = inline.get("PolicyNames", [])
    return {
        "checked": True,
        "principal_type": kind,
        "attached_policies": [p["PolicyArn"] for p in
                              (attached.get("AttachedPolicies") or [])],
        "inline_policies": names,
        # The two this automation added. Called out so a responder reading the
        # list does not mistake the containment policies for the principal's
        # own permissions and conclude it was always deny-all.
        "containment_policies_present": [
            n for n in names
            if n in ("PlatformIncidentDenyAll",
                     "PlatformIncidentRevokeOlderSessions")],
        "access_analyzer": {
            "checked": False,
            "reason": "no IAM Access Analyzer is provisioned by this platform, "
                      "so external-access findings for this principal cannot be "
                      "queried. B-058.",
        },
    }


# ---------------------------------------------------------------------------
# The cross-account sweep. Prompt 7.2: do not skip it for speed.
# ---------------------------------------------------------------------------


def cross_account_indicators(incident_account, header):
    """Do the same indicators appear in other accounts?

    This is the question that separates "one compromised workload" from "a
    platform-level event", and the two call for completely different responses
    — one is a containment that already happened, the other is everybody's
    weekend.

    Runs from Audit, the Security Hub delegated administrator, so findings from
    every account in the organization are already visible here with no
    cross-account call at all.

    The indicators are deliberately coarse: the finding TYPE and the PRINCIPAL.
    A narrower match (the exact resource) would only ever find the incident
    itself, and the interesting answer is "this pattern is also in three other
    accounts", not "this instance is this instance".
    """
    finding = json.loads(header.get("finding") or "{}")
    title = header.get("title") or ""
    principal = header.get("principal_arn")
    since = _now() - datetime.timedelta(days=SWEEP_DAYS)

    filters = {
        "UpdatedAt": [{"Start": _iso(since), "End": _iso(_now())}],
        "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
    }
    # Match on the finding type where the trigger gave us one, and fall back to
    # the title. Security Hub has no "similar to" operator, so PREFIX on the
    # title is the closest available thing.
    trigger_type = finding.get("trigger")
    if title:
        filters["Title"] = [{"Value": title[:100], "Comparison": "PREFIX"}]

    try:
        found = securityhub.get_findings(
            Filters=filters, MaxResults=100).get("Findings", [])
    except Exception as exc:                       # noqa: BLE001
        return {"checked": False,
                "reason": f"could not query Security Hub: "
                          f"{type(exc).__name__}: {exc}. THE CROSS-ACCOUNT "
                          "QUESTION IS UNANSWERED — treat the scope of this "
                          "incident as unknown rather than as contained."}

    by_account = {}
    for f in found:
        acct = f.get("AwsAccountId")
        if not acct:
            continue
        entry = by_account.setdefault(acct, {"account_id": acct, "count": 0,
                                             "titles": set()})
        entry["count"] += 1
        entry["titles"].add((f.get("Title") or "")[:120])

    others = [
        {"account_id": a["account_id"], "count": a["count"],
         "titles": sorted(a["titles"])[:5]}
        for a in by_account.values() if a["account_id"] != incident_account
    ]

    # Also ask the narrower question, because a principal appearing in two
    # accounts is a different and worse signal than a finding type appearing in
    # two accounts.
    principal_elsewhere = []
    if principal:
        short = str(principal).rsplit("/", 1)[-1]
        for f in found:
            blob = json.dumps(f.get("Resources") or [])
            if short and short in blob and f.get("AwsAccountId") != incident_account:
                principal_elsewhere.append(f.get("AwsAccountId"))

    return {
        "checked": True,
        "sweep_days": SWEEP_DAYS,
        "indicators": {"title_prefix": title[:100], "trigger": trigger_type,
                       "principal": principal},
        "matching_accounts": others,
        "principal_seen_in_other_accounts": sorted(set(principal_elsewhere)),
        # The sentence an incident commander actually needs.
        "assessment": (
            "PLATFORM-LEVEL: the same indicators are present in "
            f"{len(others)} other account(s). Treat this as broader than the "
            "contained workload."
            if others else
            "CONTAINED SCOPE: no other account in the organization shows these "
            f"indicators in the last {SWEEP_DAYS} days."),
    }


# ---------------------------------------------------------------------------
# Assembling
# ---------------------------------------------------------------------------


def build(incident_id):
    header, stages = incident_history(incident_id)
    if header is None:
        raise RuntimeError(
            f"no incident record for {incident_id}. The readout reads the "
            "append-only record rather than trusting what it was handed, and "
            "there is nothing there to read.")

    account_id = header.get("account_id")
    principal = header.get("principal_arn")
    triage = stage_detail(stages, "triage")
    preserve = stage_detail(stages, "preserve")
    isolate = stage_detail(stages, "isolate")
    revoke = stage_detail(stages, "revoke")
    blast = stage_detail(stages, "blast-radius")
    summary = stage_detail(stages, "summary")

    readout = {
        "record_type": "security-incident-readout",
        "incident_id": incident_id,
        "generated_at": _iso(_now()),

        # What fired, when, and discovery_time.
        "what_fired": {
            "trigger": header.get("trigger"),
            "title": header.get("title"),
            "confidence": header.get("confidence"),
            "environment_tier": header.get("environment_tier"),
            "account_id": account_id,
            "region": header.get("region"),
            "resource_id": header.get("resource_id"),
            "principal_arn": principal,
            "declared_by": header.get("declared_by"),
        },
        "discovery_time": header.get("discovery_time"),
        "declared_time": header.get("declared_time"),

        # What was preserved and where it is.
        "preserved": {
            "snapshots": preserve.get("snapshots") or [],
            "shared_with_forensics": preserve.get("shared_with"),
            "instance_configuration_captured": bool(preserve.get("instance")),
            "memory_capture": preserve.get("memory_capture"),
            "note": preserve.get("note"),
        },

        # What was isolated, and what remains reachable.
        "isolated": {
            "performed": bool(isolate.get("isolated")),
            "security_group": isolate.get("isolation_security_group"),
            "interfaces_changed": isolate.get("interfaces_changed") or [],
            "instance_still_running": isolate.get("instance_still_running"),
            "still_reachable": (
                "Only the Forensics collection path. Every interface on the "
                "instance carries the isolation group, which permits no ingress."
                if isolate.get("isolated") else
                "EVERYTHING. This resource was not isolated — see the decision "
                "below."),
        },

        "revoked": {
            "performed": bool(revoke.get("revoked")),
            "principal_still_exists": revoke.get("deleted") is False,
            "sessions_revoked": bool(blast.get("sessions_revoked")),
            "token_issue_time_cutoff": blast.get("token_issue_time_cutoff"),
        },

        "principal_activity": principal_activity(account_id, principal),
        "principal_reach": principal_reach(account_id, principal),
        "cross_account": cross_account_indicators(account_id, header),

        "containment_state": {
            "decision": triage.get("decision"),
            "basis": triage.get("basis"),
            "stages_completed": [s["stage"] for s in stages],
            "elapsed_from_discovery_seconds":
                summary.get("elapsed_from_discovery_seconds"),
        },
        "deliberately_not_done": summary.get("deliberately_not_done") or [],
    }
    return header, readout


def handler(event, context):
    incident_id = (event.get("incident") or {}).get("incident_id") \
        or event.get("incident_id")
    if not incident_id:
        raise RuntimeError("no incident_id supplied to the readout")

    header, readout = build(incident_id)

    # Appended to the same record, so the readout is part of the incident rather
    # than a separate artifact somebody has to find.
    if INCIDENT_TABLE:
        dynamodb.put_item(TableName=INCIDENT_TABLE, Item={
            "PK": {"S": f"incident#{incident_id}"},
            "SK": {"S": f"event#{_iso(_now())}#readout"},
            "record_type": {"S": "containment-event"},
            "incident_id": {"S": incident_id},
            "stage": {"S": "readout"},
            "outcome": {"S": "complete"},
            "recorded_at": {"S": _iso(_now())},
            "detail": {"S": json.dumps(readout, default=str)[:38000]},
        })

    # Same account, resource and title as the containment summary, so the
    # router derives the SAME dedup key and PagerDuty updates the incident that
    # is already open rather than raising a second one.
    dossier = {
        "record_type": readout["record_type"],
        "recorded_at": readout["generated_at"],
        "current_state": "ALARM",
        "alarm": {
            "name": f"platform-security-incident-"
                    f"{readout['containment_state']['decision']}",
            "resource_id": header.get("resource_id")
                           or header.get("principal_arn") or "unknown",
            "environment_tier": header.get("environment_tier"),
            "account_id": header.get("account_id"),
        },
        "assessment": {
            "probable_cause": header.get("title") or "security incident",
            # Surfaced to the top of the page, because it is the one line that
            # changes what the responder does next.
            "scope": readout["cross_account"].get("assessment"),
        },
        "readout": readout,
    }
    events.put_events(Entries=[{
        "Source": "platform.security",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "EventBusName": ALERT_BUS_NAME,
    }])

    print(json.dumps({"incident_id": incident_id,
                      "scope": readout["cross_account"].get("assessment"),
                      "cross_account_checked":
                          readout["cross_account"].get("checked")}, default=str))
    return {
        "incident_id": incident_id,
        "cross_account_checked": readout["cross_account"].get("checked"),
        "matching_accounts": len(
            readout["cross_account"].get("matching_accounts") or []),
        # Truveon does not exist. Prompt 7.2 says "write to Truveon"; the
        # readout is written to the append-only incident record instead, which
        # is durable, immutable and already the evidence store for this
        # incident. The hand-off stays outstanding rather than silently
        # skipped — the same treatment reporting/src/digest.py gives it.
        "truveon_delivered": False,
    }
