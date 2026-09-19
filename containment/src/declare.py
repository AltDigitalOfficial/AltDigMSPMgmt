"""Declaration. Prompt 7.1, design/07-response-automation.md "Declaration".

    Most damage comes from taking too long to decide something *is* a security
    incident. Declaration is therefore triggered without debate on defined
    conditions. [...] Bias toward over-declaration. Declaring is cheap;
    standing down is fine.

Declaring is cheap. CONTAINING is not, which is why this module's only job is
to create the incident object with an honest `discovery_time` and hand off.
Whether anything is isolated is decided by design 07's confidence table, in
contain.py's triage(), against the environment — so over-declaration costs a
record and a page, never a wrongly-isolated production service.

discovery_time
--------------
    discovery_time is legally significant. It is set by the system at detection
    and is never editable. Where a human declares on something detected
    earlier, both times are recorded and the earlier governs.

Three consequences, all implemented here:

  * It is read from the FINDING, not from the clock. GuardDuty's
    `service.eventFirstSeen` can be hours before the finding reaches us, and
    using our own receipt time would quietly restart a legally significant
    clock — in AltDigital's favour, which is exactly the direction that will
    not survive scrutiny.
  * A manual declaration may supply an earlier observation. The earlier of the
    two governs, and both are kept.
  * It is written once, on an item that has no update path. See
    contain.py's record_event() for why that is structural rather than a
    convention.
"""
import datetime
import hashlib
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
sfn = boto3.client("stepfunctions")
ssm = boto3.client("ssm")

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE", "")
STATE_MACHINE_ARN = os.environ.get("CONTAINMENT_STATE_MACHINE_ARN", "")
ACCOUNT_TIER_PARAM = os.environ.get("ACCOUNT_TIER_PARAM",
                                    "/platform/routing/account-tiers")

_TIER_CACHE = {}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value / 1000.0,
                                                   datetime.timezone.utc)
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None


def account_tier(account_id):
    """Environment tier for an account, from the map the router already uses.

    Same source as alerting/src/router.py — /platform/routing/account-tiers,
    written by scripts/sync-account-tiers.sh. Deliberately not a second
    lookup mechanism: B-041 was a routing decision made in two places that
    disagreed, and "which tier is this account" is the input to the confidence
    table that decides whether production gets isolated.
    """
    if not account_id:
        return None
    if not _TIER_CACHE:
        try:
            raw = ssm.get_parameter(Name=ACCOUNT_TIER_PARAM)["Parameter"]["Value"]
            _TIER_CACHE.update(json.loads(raw))
        except Exception as exc:                   # noqa: BLE001
            print(json.dumps({"TIER_LOOKUP_FAILED": str(exc)}))
            return None
    return _TIER_CACHE.get(account_id)


# ---------------------------------------------------------------------------
# What counts as a declaration
# ---------------------------------------------------------------------------
#
# design 07's list, one recogniser each. Each returns None (not this) or a dict
# describing the incident.
#
# On CONFIDENCE, which is the input that decides whether production is touched:
#
#   GuardDuty and Security Hub carry a severity the service computed, and that
#   is used directly.
#
#   The CloudTrail triggers — ScheduleKeyDeletion, CloudTrail disable, vault
#   lock modification — are recorded as MEDIUM, and the reasoning matters. The
#   EVENT is a fact; whether it is malicious is an inference. A legitimate,
#   quorum-approved key deletion looks identical at this layer, and prompt 8.3
#   (which reconciles these against approved changes) is not built. Medium
#   means production gathers and pages rather than auto-revoking, so a planned
#   key deletion wakes somebody instead of attaching a deny-all to the admin
#   who performed it. In dev and test, medium still auto-contains, which is
#   both harmless and good practice.


def _from_guardduty(event):
    d = event.get("detail") or {}
    severity = float(d.get("severity") or 0)
    if severity < 7.0:
        # design 07 declares on "GuardDuty finding at high severity". Lower
        # severities are real signals and are routed by the alerting layer;
        # they are not incidents.
        return None
    resource = d.get("resource") or {}
    instance = (resource.get("instanceDetails") or {}).get("instanceId")
    access_key = (resource.get("accessKeyDetails") or {})
    return {
        "trigger": "guardduty-high-severity",
        "title": d.get("title") or d.get("type") or "GuardDuty finding",
        "description": d.get("description"),
        "account_id": d.get("accountId") or event.get("account"),
        "region": d.get("region") or event.get("region"),
        "resource_id": instance,
        "principal_arn": access_key.get("principalId") and access_key.get("userName")
                         and _user_arn(d.get("accountId") or event.get("account"),
                                       access_key),
        "confidence": "high" if severity >= 7.0 else "medium",
        "severity": severity,
        # eventFirstSeen, not the event's arrival time. This is the clock.
        "detected_at": (d.get("service") or {}).get("eventFirstSeen")
                       or event.get("time"),
        "source_id": d.get("id"),
    }


def _user_arn(account_id, access_key):
    kind = (access_key.get("userType") or "").lower()
    name = access_key.get("userName")
    if not name or not account_id:
        return None
    if kind == "assumedrole":
        return f"arn:aws:iam::{account_id}:role/{name}"
    if kind == "iamuser":
        return f"arn:aws:iam::{account_id}:user/{name}"
    return None


def _from_security_hub(event):
    d = event.get("detail") or {}
    findings = d.get("findings") or []
    if not findings:
        return None
    f = findings[0]
    label = ((f.get("Severity") or {}).get("Label") or "").upper()
    types = " ".join(f.get("Types") or [])
    title = f.get("Title") or ""
    blob = f"{types} {title}".lower()

    # design 07's remaining declaration conditions, all of which reach us as
    # Security Hub findings: credential exposure, unexpected egress,
    # unauthorised data store access, ransomware indicators.
    # COMPLIANCE CONTROL FINDINGS ARE NOT SECURITY INCIDENTS.
    #
    # Found by deploying. An earlier version declared on any CRITICAL or HIGH
    # label when no keyword matched, and the first live run declared an
    # incident for "SSM.7 SSM documents should have the block public sharing
    # setting enabled" -- an AWS Foundational Security Best Practices control,
    # rated CRITICAL, describing a configuration default.
    #
    # It was harmless that time only because the finding named an ACCOUNT and
    # no instance, so containment had nothing to preserve, isolate or revoke.
    # A control finding that names an EC2 instance -- "EC2.8 instances should
    # use IMDSv2", say -- would have isolated a running production server over
    # a configuration setting. That is precisely the false positive design 07
    # says is "its own incident".
    #
    # design 07's declaration list is specific: credential exposure, unexpected
    # data egress, unauthorised access to a data store, ransomware indicators.
    # It is not "anything Security Hub rates highly". Severity now modulates
    # CONFIDENCE and never decides declaration on its own.
    if types.startswith("Software and Configuration Checks"):
        return None

    matched = None
    for needle, name in (
            ("credential", "credential-exposure"),
            ("exposed", "credential-exposure"),
            ("exfiltration", "unexpected-egress"),
            ("egress", "unexpected-egress"),
            ("ransom", "ransomware-indicator"),
            ("unauthorizedaccess", "unauthorised-data-access"),
            ("unauthorised", "unauthorised-data-access"),
            ("policy:s3", "unauthorised-data-access")):
        if needle in blob:
            matched = name
            break

    if matched is None:
        # The attack families. Security Hub groups findings by Types, and these
        # three describe observed adversary behaviour rather than configuration
        # state -- which is the line design 07 is drawing.
        for family, name in (("TTPs", "attack-technique-observed"),
                             ("Effects", "attack-effect-observed"),
                             ("Unusual Behaviors", "unusual-behaviour")):
            if types.startswith(family):
                matched = name
                break

    if matched is None:
        return None

    resources = f.get("Resources") or []
    resource_id = resources[0].get("Id") if resources else None
    confidence_score = f.get("Confidence")
    if confidence_score is not None:
        confidence = "high" if confidence_score >= 70 else "medium"
    else:
        confidence = "high" if label == "CRITICAL" else "medium"

    return {
        "trigger": matched or f"security-hub-{label.lower()}",
        "title": title or "Security Hub finding",
        "description": f.get("Description"),
        "account_id": f.get("AwsAccountId") or event.get("account"),
        "region": f.get("Region") or event.get("region"),
        "resource_id": resource_id,
        "principal_arn": _principal_from_resources(resources),
        "confidence": confidence,
        "severity": (f.get("Severity") or {}).get("Normalized"),
        "detected_at": f.get("FirstObservedAt") or f.get("CreatedAt")
                       or event.get("time"),
        "source_id": f.get("Id"),
    }


def _principal_from_resources(resources):
    for r in resources:
        rid = r.get("Id") or ""
        if ":role/" in rid or ":user/" in rid or ":assumed-role/" in rid:
            return rid
    return None


# The three CloudTrail attempts design 07 names explicitly. Declared on ANY
# attempt, successful or denied -- B-039's sibling logic: a denied attempt is
# either confusion or something worse, and both are worth a record.
_CLOUDTRAIL_TRIGGERS = {
    "ScheduleKeyDeletion": "kms-key-deletion-attempt",
    "DisableKey": "kms-key-disable-attempt",
    "StopLogging": "cloudtrail-disable-attempt",
    "DeleteTrail": "cloudtrail-disable-attempt",
    "UpdateTrail": "cloudtrail-modification-attempt",
    "PutBackupVaultLockConfiguration": "vault-lock-modification-attempt",
    "DeleteBackupVaultLockConfiguration": "vault-lock-modification-attempt",
    "DeleteBackupVault": "vault-deletion-attempt",
    "DeleteRecoveryPoint": "recovery-point-deletion-attempt",
}


def _from_cloudtrail(event):
    d = event.get("detail") or {}
    name = d.get("eventName")
    trigger = _CLOUDTRAIL_TRIGGERS.get(name)
    if not trigger:
        return None
    identity = d.get("userIdentity") or {}
    principal = identity.get("arn")
    # A platform principal performing these is the platform doing its job --
    # the response engine, a StackSet, an operator through SSO. Declared
    # anyway, because design 07 says "any attempt", but the responder role
    # cannot revoke a Platform* principal, so this can only ever produce a
    # record and a page for those.
    return {
        "trigger": trigger,
        "title": f"{name} attempted by {identity.get('arn') or identity.get('type')}",
        "description": json.dumps({
            "errorCode": d.get("errorCode"),
            "requestParameters": d.get("requestParameters"),
        }, default=str)[:2000],
        "account_id": d.get("recipientAccountId") or event.get("account"),
        "region": d.get("awsRegion") or event.get("region"),
        "resource_id": None,
        "principal_arn": principal,
        # Medium, deliberately. See the note above the recognisers.
        "confidence": "medium",
        "severity": None,
        "detected_at": d.get("eventTime") or event.get("time"),
        "source_id": d.get("eventID"),
        "denied": bool(d.get("errorCode")),
    }


def _from_manual(event):
    """Manual declaration by any engineer. design 07 allows it explicitly."""
    d = event.get("detail") or {}
    if event.get("detail-type") != "Platform Manual Declaration":
        return None
    return {
        "trigger": "manual",
        "title": d.get("title") or "Manually declared security incident",
        "description": d.get("description"),
        "account_id": d.get("account_id"),
        "region": d.get("region"),
        "resource_id": d.get("resource_id"),
        "principal_arn": d.get("principal_arn"),
        "confidence": (d.get("confidence") or "medium").lower(),
        "severity": None,
        # The declarer may know the condition existed earlier than now. If they
        # do, it governs -- see declare().
        "detected_at": d.get("observed_at") or event.get("time"),
        "declared_by": d.get("declared_by"),
        "source_id": d.get("source_id"),
        "mode": d.get("mode"),
    }


RECOGNISERS = (_from_manual, _from_guardduty, _from_security_hub, _from_cloudtrail)


def recognise(event):
    for fn in RECOGNISERS:
        found = fn(event)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Declaring
# ---------------------------------------------------------------------------


def incident_id(found, event):
    """Deterministic, so the same finding cannot declare twice.

    GuardDuty re-emits a finding every time it updates, and Security Hub
    re-emits on every workflow change. Without a stable id, one compromised
    instance would declare a fresh incident every few minutes and re-run
    containment against an already-isolated machine -- a self-inflicted alarm
    storm on the worst possible night.

    The id is derived from the SOURCE finding id where there is one, so the
    conditional write below collapses repeats into the single incident they
    are.
    """
    basis = found.get("source_id") or json.dumps({
        "t": found.get("trigger"), "a": found.get("account_id"),
        "r": found.get("resource_id"), "p": found.get("principal_arn"),
        # A manual declaration with no source id gets the hour, so two people
        # declaring the same thing minutes apart collapse, and a genuine second
        # incident an hour later does not.
        "h": _now().strftime("%Y%m%d%H"),
    }, sort_keys=True)
    digest = hashlib.sha256(basis.encode()).hexdigest()[:10]
    return f"{_now().strftime('%Y%m%d')}-{digest}"


def declare(event, context=None):
    found = recognise(event)
    if found is None:
        out = {"declared": False,
               "reason": "no declaration condition matched",
               "detail_type": event.get("detail-type")}
        print(json.dumps(out))
        return out

    now = _now()
    detected = _parse(found.get("detected_at")) or now
    # "Where a human declares on something detected earlier, both times are
    # recorded and the EARLIER governs." Implemented as a min, not as a
    # preference for whichever source we happened to read first.
    discovery = min(detected, now)

    iid = incident_id(found, event)
    tier = account_tier(found.get("account_id"))

    header = {
        "PK": {"S": f"incident#{iid}"},
        "SK": {"S": "header"},
        "record_type": {"S": "security-incident"},
        "incident_id": {"S": iid},
        # THE legally significant field. Written here, never updated anywhere.
        "discovery_time": {"S": _iso(discovery)},
        "declared_time": {"S": _iso(now)},
        "trigger": {"S": found["trigger"]},
        "title": {"S": (found.get("title") or "security incident")[:900]},
        "confidence": {"S": found.get("confidence") or "medium"},
        "environment_tier": {"S": tier or "unknown"},
        "finding": {"S": json.dumps(found, default=str)[:38000]},
    }
    for key, value in (("account_id", found.get("account_id")),
                       ("region", found.get("region")),
                       ("resource_id", found.get("resource_id")),
                       ("principal_arn", found.get("principal_arn")),
                       ("declared_by", found.get("declared_by")),
                       ("source_id", found.get("source_id"))):
        if value:
            header[key] = {"S": str(value)}

    already = False
    if INCIDENT_TABLE:
        try:
            dynamodb.put_item(
                TableName=INCIDENT_TABLE,
                Item=header,
                # Both halves of the immutability guarantee in one clause: a
                # repeat finding cannot create a second incident, and it cannot
                # overwrite the discovery_time of the first.
                ConditionExpression="attribute_not_exists(PK)")
        except dynamodb.exceptions.ConditionalCheckFailedException:
            already = True

    if already:
        out = {"declared": False, "incident_id": iid, "already_declared": True,
               "reason": "this finding has already been declared; containment "
                         "is not re-run against an incident in flight"}
        print(json.dumps(out))
        return out

    incident = {
        "incident_id": iid,
        "discovery_time": _iso(discovery),
        "declared_time": _iso(now),
        "trigger": found["trigger"],
        "title": found.get("title"),
        "account_id": found.get("account_id"),
        "region": found.get("region"),
        "resource_id": found.get("resource_id"),
        "principal_arn": found.get("principal_arn"),
        "confidence": found.get("confidence") or "medium",
        "environment_tier": tier,
        "source_id": found.get("source_id"),
        "declared_by": found.get("declared_by"),
    }

    started = None
    if STATE_MACHINE_ARN:
        started = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=iid[:80],
            input=json.dumps({
                "mode": found.get("mode") or "incident",
                "incident": incident,
            }, default=str),
        )["executionArn"]

    out = {"declared": True, "incident_id": iid,
           "discovery_time": incident["discovery_time"],
           "declared_time": incident["declared_time"],
           "trigger": incident["trigger"],
           "environment_tier": tier,
           "confidence": incident["confidence"],
           "execution": started}
    print(json.dumps(out, default=str))
    return out


def handler(event, context):
    return declare(event, context)
