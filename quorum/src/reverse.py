"""Reverse reconciliation for quorum actions. Prompt 8.3.

    Reconciliation then runs in the opposite direction: every
    ScheduleKeyDeletion in CloudTrail must map to an approved Jira change. Any
    that does not is a finding. That is what makes it a control rather than a
    procedure -- proving the negative, not just documenting the positive.
                                        -- design/08-change-and-release.md

Everything else in Phase 8 starts from a record and asks whether it was
honoured. This starts from what actually happened and asks whether anybody
agreed to it. The difference matters because the forward direction can only
ever find paperwork that went unused; it cannot, even in principle, find an
action taken without paperwork. Only this direction can.

What makes it cheap
-------------------
The executor role's trust policy requires a session name matching `quorum-*`,
and execute.py sets it to `quorum-<issue-key>`. CloudTrail records that name on
every call. So the question "which change record authorised this deletion?" is
answered by AWS's own log rather than by asking the pipeline to describe its
own behaviour -- which is the only kind of answer worth having, since a
compromised or buggy pipeline would describe itself favourably.

An event therefore falls into one of four buckets, and three are findings:

    through the pipeline, record approves it      expected. Not emitted.
    through the pipeline, record does NOT         severe. The pipeline is the
                                                  only path, so this means the
                                                  path itself misbehaved.
    NOT through the pipeline                      the control was bypassed.
                                                  design 03 says these actions
                                                  are denied outright, so this
                                                  is either a gap in the SCPs
                                                  or somebody with a way round
                                                  them.
    Jira unreadable                               not a finding about anybody.
                                                  An exception record.

That last one, again
--------------------
If Jira cannot be read, this module does NOT conclude that deletions were
unapproved. It says the check could not be performed. Reported as a negative
result it would accuse whoever performed a legitimate, properly approved
deletion -- and the accusation would be loudest exactly when the platform is
least able to substantiate it. B-042, B-054, B-080.
"""
import datetime
import json
import os

import boto3

import jira
import verify

events_client = boto3.client("events")
ssm = boto3.client("ssm")
sts = boto3.client("sts")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
ACCOUNT_PREFIX = os.environ.get("ACCOUNT_REGISTRY_PREFIX",
                                "/platform/org/account/")
MEMBER_ROLE = os.environ.get("QUORUM_READ_ROLE",
                             "OrganizationAccountAccessRole")
LOOKBACK_HOURS = int(os.environ.get("QUORUM_REVERSE_LOOKBACK_HOURS", "26"))
REGIONS = [r.strip() for r in
           os.environ.get("PLATFORM_ALLOWED_REGIONS",
                          "us-east-1,us-east-2,us-west-2").split(",")
           if r.strip()]

# The CloudTrail event names for design 03's quorum-controlled actions.
#
# Keyed by event name because that is what CloudTrail carries; the values are
# the IAM action strings the change records declare, so a record saying
# "quorum-action: kms:ScheduleKeyDeletion" can be matched to an event saying
# "ScheduleKeyDeletion".
QUORUM_EVENT_NAMES = {
    "ScheduleKeyDeletion": "kms:ScheduleKeyDeletion",
    "DeleteBackupVault": "backup:DeleteBackupVault",
    "PutBackupVaultLockConfiguration":
        "backup:PutBackupVaultLockConfiguration",
    "DeleteBackupVaultLockConfiguration":
        "backup:DeleteBackupVaultLockConfiguration",
    "RemoveAccountFromOrganization":
        "organizations:RemoveAccountFromOrganization",
    "StopLogging": "cloudtrail:StopLogging",
    "DeleteTrail": "cloudtrail:DeleteTrail",
    "StopConfigurationRecorder": "config:StopConfigurationRecorder",
    "DeleteConfigurationRecorder": "config:DeleteConfigurationRecorder",
}

EXECUTOR_ROLE_NAME = os.environ.get("QUORUM_EXECUTOR_ROLE",
                                    "PlatformQuorumExecutor")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def platform_accounts():
    """Accounts to sweep, from the registry rather than from a list here.

    B-064: a hand-maintained account list was missing Forensics, so an entire
    account went unchecked and nothing said so. The registry is the source of
    truth and is updated by provisioning.
    """
    out = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=ACCOUNT_PREFIX):
        for param in page.get("Parameters") or []:
            alias = param["Name"].rsplit("/", 1)[-1]
            value = (param.get("Value") or "").strip()
            if value and value != "None":
                out[alias] = value
    return out


def _session_issue_key(event):
    """The change record named in the session, or None.

    CloudTrail records the assumed-role session name in the principal's ARN:

        arn:aws:sts::123456789012:assumed-role/PlatformQuorumExecutor/quorum-AMI-9

    A None here is the whole point of the check -- it means the call was NOT
    made through the quorum pipeline, whatever else it was.
    """
    identity = (event.get("userIdentity") or {})
    arn = identity.get("arn") or ""
    if f":assumed-role/{EXECUTOR_ROLE_NAME}/" not in arn:
        return None
    session = arn.rsplit("/", 1)[-1]
    if not session.startswith("quorum-"):
        # The trust policy forbids this, so seeing it means the trust policy
        # was changed. Worth not silently treating as "through the pipeline".
        return None
    return session[len("quorum-"):]


def _lookup(account_id, region, since):
    """Quorum-action events from one account and region.

    LookupEvents rather than reading the Organization trail out of S3. The
    trail is authoritative and is what an auditor would read, but parsing it
    means listing and decompressing a day of objects per account per region to
    find events that are almost always absent. LookupEvents answers the same
    question in one call, and the trail remains the record of last resort.

    Its 90-day limit is not a constraint here: this runs every few hours over a
    26-hour window.
    """
    try:
        creds = sts.assume_role(
            RoleArn=f"arn:aws:iam::{account_id}:role/{MEMBER_ROLE}",
            RoleSessionName="quorum-reverse-reconcile",
        )["Credentials"]
    except Exception as exc:                       # noqa: BLE001
        return None, f"could not assume into {account_id}: {exc}"

    client = boto3.client(
        "cloudtrail", region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])

    found = []
    for name in QUORUM_EVENT_NAMES:
        try:
            paginator = client.get_paginator("lookup_events")
            for page in paginator.paginate(
                    LookupAttributes=[{"AttributeKey": "EventName",
                                       "AttributeValue": name}],
                    StartTime=since,
                    PaginationConfig={"MaxItems": 200}):
                for record in page.get("Events") or []:
                    try:
                        detail = json.loads(record.get("CloudTrailEvent")
                                            or "{}")
                    except ValueError:
                        detail = {}
                    found.append({
                        "event_name": name,
                        "action": QUORUM_EVENT_NAMES[name],
                        "account_id": account_id,
                        "region": region,
                        "event_time": record.get("EventTime"),
                        "event_id": record.get("EventID"),
                        "resources": [r.get("ResourceName")
                                      for r in (record.get("Resources") or [])],
                        "request_parameters":
                            detail.get("requestParameters") or {},
                        "error_code": detail.get("errorCode"),
                        "issue_key": _session_issue_key(detail),
                        "principal_arn":
                            ((detail.get("userIdentity") or {}).get("arn")),
                    })
        except Exception as exc:                   # noqa: BLE001
            return None, (f"could not read CloudTrail in {account_id}/{region}"
                          f": {exc}")
    return found, None


def _assess(event):
    """Which of the four buckets this event falls into."""
    # A DENIED attempt is not an unapproved action -- nothing happened. It is
    # still worth surfacing, and 7.1's declare.py already alarms on every
    # attempt including denials, so this does not raise a second one. Recorded
    # here for completeness of the sweep and nothing more.
    if event.get("error_code"):
        return "denied_attempt", (
            f"{event['event_name']} was attempted and denied "
            f"({event['error_code']}). Nothing changed. Already alarmed by "
            "the security declaration path.")

    issue = event.get("issue_key")
    if not issue:
        return "outside_pipeline", (
            f"{event['event_name']} was performed in {event['account_id']} by "
            f"{event.get('principal_arn') or 'an unrecorded principal'}, which "
            "is not the quorum pipeline. design 03 denies these actions "
            "outright and names the pipeline as the only execution path, so "
            "this is either a gap in the SCPs or a principal that can get "
            "round them.")

    resource = _event_resource(event)
    try:
        result = verify.verify({"issue": issue, "action": event["action"],
                                "resource": resource})
    except jira.JiraUnavailable as exc:
        return "check_unavailable", (
            f"{event['event_name']} names change record {issue}, and Jira "
            f"could not be read to check it: {exc}. This is NOT a finding "
            "that the action was unapproved -- it is a finding that the "
            "platform cannot currently tell.")

    if result.get("verdict") == verify.VERDICT_APPROVED:
        return "approved", None
    if result.get("verdict") == verify.VERDICT_UNDETERMINABLE:
        return "check_unavailable", (
            f"{event['event_name']} names change record {issue}, which cannot "
            f"be evaluated: {result.get('reason')}")
    return "pipeline_without_approval", (
        f"{event['event_name']} was performed through the quorum pipeline "
        f"naming change record {issue}, but {issue} does not approve it: "
        f"{result.get('failures')}. The pipeline is the only path to this "
        "action and it verifies before acting, so this should not be "
        "reachable. Treat it as the pipeline itself being wrong.")


def _event_resource(event):
    """The ARN or identifier the event acted on.

    CloudTrail's `Resources` is populated inconsistently across services, so
    requestParameters is checked first -- it is what the caller actually sent.
    """
    params = event.get("request_parameters") or {}
    for field in ("keyId", "backupVaultName", "name", "trailName",
                  "configurationRecorderName", "accountId"):
        if params.get(field):
            return params[field]
    resources = [r for r in (event.get("resources") or []) if r]
    return resources[0] if resources else None


def _emit(finding):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "altdigital.quorum",
        "DetailType": "quorum-reverse-reconciliation",
        "Detail": json.dumps(finding, default=str),
    }])


def reverse(event=None, context=None):
    """Sweep CloudTrail for quorum actions and check each against Jira."""
    since = _now() - datetime.timedelta(hours=LOOKBACK_HOURS)
    accounts = platform_accounts()
    results = []
    unreadable = []

    only = (event or {}).get("accounts")
    for alias, account_id in sorted(accounts.items()):
        if only and account_id not in only and alias not in only:
            continue
        for region in REGIONS:
            found, problem = _lookup(account_id, region, since)
            if problem:
                # An account that cannot be swept is NOT an account with no
                # quorum actions. B-064's lesson in a different costume: the
                # gap has to be visible, or the sweep silently covers less of
                # the estate than it claims to.
                unreadable.append({"account_alias": alias,
                                   "account_id": account_id,
                                   "region": region, "problem": problem})
                continue
            for item in found:
                outcome, detail = _assess(item)
                results.append({**item, "outcome": outcome, "detail": detail})

    findings = [r for r in results
                if r["outcome"] in ("outside_pipeline",
                                    "pipeline_without_approval",
                                    "check_unavailable")]
    for finding in findings:
        _emit({
            "record_type": "quorum-reverse-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            # A quorum action outside the pipeline is the most serious thing
            # this codebase looks for: it means a control design 03 calls
            # absolute did not hold. It pages. The others are reviewed.
            "routing": ("pagerduty" if finding["outcome"] == "outside_pipeline"
                        else "review_queue"),
            "alarm": {
                "name": f"platform-quorum-{finding['outcome'].replace('_', '-')}",
                "resource_id": _event_resource(finding) or finding["event_id"],
                "account_id": finding["account_id"],
            },
            "assessment": {"probable_cause": finding["detail"]},
            "quorum_reverse": finding,
        })

    for gap in unreadable:
        _emit({
            "record_type": "quorum-reverse-sweep-gap",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": "review_queue",
            "alarm": {"name": "platform-quorum-sweep-incomplete",
                      "resource_id": gap["account_id"],
                      "account_id": gap["account_id"]},
            "assessment": {"probable_cause": (
                f"Quorum actions could not be swept in {gap['account_alias']} "
                f"({gap['region']}): {gap['problem']}. No conclusion can be "
                "drawn about that account, and this sweep covers less of the "
                "estate than its other findings imply.")},
            "quorum_reverse_gap": gap,
        })

    counts = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    out = {
        "record_type": "quorum-reverse-reconciliation",
        "reconciled_at": _iso(_now()),
        "window_hours": LOOKBACK_HOURS,
        "accounts_swept": len(accounts) - len({u["account_id"]
                                               for u in unreadable}),
        "accounts_unreadable": len({u["account_id"] for u in unreadable}),
        "events": len(results),
        "outcomes": counts,
        "findings": len(findings),
    }
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"reverse": reverse}


def handler(event, context):
    fn = HANDLERS.get((event or {}).get("action_type") or "reverse")
    if fn is None:
        raise ValueError(f"unknown action_type: {(event or {}).get('action_type')}")
    return fn(event, context)
