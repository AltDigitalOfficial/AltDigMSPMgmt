"""Detects an agent stopped on a live instance. Prompt 10.3.

design/11-developer-access.md, "Detecting loss of visibility":

    A developer with root on their own EC2 instance can stop the CloudWatch
    agent. No SCP prevents this. It is detectable, because the signal is not
    absence of data -- it is ABSENCE OF DATA FROM SOMETHING DEMONSTRABLY ALIVE.

That sentence is the whole module. Absence of telemetry is ambiguous and
common: instances stop, get patched, get scaled in, sit inside a HOOP window.
Absence of telemetry from an instance that is passing status checks and moving
traffic is not ambiguous at all.

The table is the implementation, not a description of it
---------------------------------------------------------
design 11 gives a discriminator table and the prompt says to implement it
exactly, with the other scenarios NOT producing this alarm. So the table is
transcribed as data and evaluated row by row, rather than collapsed into a
condition that happens to agree with it today.

The difference matters when somebody edits this later: a row is visibly a row,
and a missing row is visibly missing. An `if` chain that means the same thing
loses that the moment anybody adds a clause.

Two rows are identical, and that is reported rather than hidden
----------------------------------------------------------------
    Network or IAM path broken   Pass / Traffic / Lost / Stops
    Both agents killed           Pass / Traffic / Lost / Stops

Across all four signals these are the same. So when SSM ping is also lost, this
module cannot tell a killed agent from a broken IAM path, and does not pretend
to: the finding names both. Both are a genuine loss of visibility on a live
instance and both want a human, so the alarm is correct either way -- it is the
CAUSE that is undetermined, and saying which is unknown costs nothing while
guessing costs trust the first time the guess is wrong.

Noise control is a requirement, not a refinement
-------------------------------------------------
design 11 is explicit: agents restart, instances reboot during patching, and an
environment stopped by its HOOP "must be a known state, not left to look like
an anomaly -- otherwise every turndown is a false positive".

This codebase has learned that twice already (B-043, B-081). A detector that
fires on every patch window is one somebody mutes, and it takes the real
signal with it.
"""
import datetime
import json
import os

import boto3

cloudwatch = boto3.client("cloudwatch")
events_client = boto3.client("events")
logs = boto3.client("logs")
ssm = boto3.client("ssm")
sts = boto3.client("sts")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
ACCOUNT_TIER_PARAM = os.environ.get("ACCOUNT_TIER_PARAM",
                                    "/platform/routing/account-tiers")
MEMBER_ROLE = os.environ.get("TELEMETRY_READ_ROLE", "PlatformQuorumReader")

# Longer than a normal agent restart, and longer than a reboot during patching.
#
# design 11: "Grace period longer than a normal agent restart." An agent
# reinstalled by the SSM association is back within a couple of minutes; a
# reboot cycle is longer. Thirty minutes is comfortably past both and still far
# short of the daily association run that provides the real escalation signal.
GRACE_MINUTES = int(os.environ.get("TELEMETRY_GRACE_MINUTES", "30"))

# The sentinel. design 11: "mem_used_percent is a good sentinel -- it exists
# only if the agent runs."
#
# CPU would not work: the hypervisor publishes it whether or not anything is
# running inside the guest, so it stays green on an instance with no agent at
# all. Memory is guest-internal and unobtainable from outside.
SENTINEL_METRIC = os.environ.get("TELEMETRY_SENTINEL", "mem_used_percent")
SENTINEL_NAMESPACE = os.environ.get("TELEMETRY_NAMESPACE", "CWAgent")


# ---------------------------------------------------------------------------
# design 11's discriminator table, as data
# ---------------------------------------------------------------------------
#
# Columns: status checks, flow logs, SSM ping, CloudWatch telemetry.
#
#   alive     status checks passing
#   traffic   flow logs show recent traffic
#   ssm       SSM ping healthy
#   telemetry sentinel metric or log ingestion is recent
#
# `None` means the signal does not participate in matching that row -- used
# where design 11's table says a state event settles it before the other
# columns are consulted.

SCENARIOS = [
    {
        "name": "instance-stopped-or-terminated",
        "alive": False, "traffic": None, "ssm": None, "telemetry": False,
        "alarm": False,
        "note": "The instance is not running. Telemetry stopping is the "
                "expected consequence, not a finding.",
    },
    {
        "name": "os-hung-or-crashed",
        "alive": True, "status_checks": False,
        "traffic": None, "ssm": None, "telemetry": False,
        "alarm": False,
        "note": "Status checks are failing. That is an availability incident "
                "and is alarmed by the instrumentation layer; raising a "
                "visibility finding on top would double-report one event.",
    },
    {
        "name": "cloudwatch-agent-killed",
        "alive": True, "status_checks": True,
        "traffic": True, "ssm": True, "telemetry": False,
        "alarm": True,
        "note": "Instance provably alive, passing traffic, SSM responding, "
                "and the CloudWatch agent silent. Nothing else produces this "
                "signature.",
    },
    {
        "name": "both-agents-killed-or-path-broken",
        "alive": True, "status_checks": True,
        "traffic": True, "ssm": False, "telemetry": False,
        "alarm": True,
        # The row design 11 splits in two and the signals do not.
        "note": "Instance alive and passing traffic with neither SSM nor "
                "CloudWatch responding. design 11 lists this signature twice "
                "-- 'both agents killed' and 'network or IAM path broken' -- "
                "and they are indistinguishable from here. Both are a real "
                "loss of visibility on a live instance; the cause is "
                "undetermined and is not guessed at.",
    },
]


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class Signals:
    """What is observably true about one instance, before interpretation.

    Deliberately a plain record of four booleans plus provenance. Gathering and
    judging are separate so that a wrong verdict can be traced to a wrong
    signal rather than to a tangle of conditions.
    """

    def __init__(self, instance_id, account_id, region):
        self.instance_id = instance_id
        self.account_id = account_id
        self.region = region
        self.alive = None
        self.status_checks = None
        self.traffic = None
        self.ssm = None
        self.telemetry = None
        self.unreadable = []

    def as_dict(self):
        return {
            "instance_id": self.instance_id,
            "account_id": self.account_id,
            "region": self.region,
            "alive": self.alive,
            "status_checks": self.status_checks,
            "traffic": self.traffic,
            "ssm": self.ssm,
            "telemetry": self.telemetry,
            "unreadable": self.unreadable,
        }


def classify(signals):
    """Which row of design 11's table this instance matches.

    Returns (scenario, note). A signal that could not be read yields no match
    at all rather than a default, because every row of the table is a positive
    statement about four signals and an unknown is not a value any of them
    take.
    """
    if signals.unreadable:
        return None, (
            f"{len(signals.unreadable)} signal(s) could not be read "
            f"({', '.join(signals.unreadable)}), so no row of the "
            "discriminator table applies. This is NOT 'no problem found' -- "
            "it is a question that could not be asked.")

    for scenario in SCENARIOS:
        if not _matches(scenario, signals):
            continue
        return scenario, scenario["note"]

    # design 11's table is not exhaustive over all sixteen combinations, and
    # that is fine: the unlisted ones are healthy or uninteresting. Saying so
    # explicitly beats falling through to a default that looks like a verdict.
    return None, "no row of the discriminator table matches; nothing to report"


def _matches(scenario, signals):
    for field in ("alive", "status_checks", "traffic", "ssm", "telemetry"):
        expected = scenario.get(field)
        if expected is None:
            continue
        if getattr(signals, field) is not expected:
            return False
    return True


# ---------------------------------------------------------------------------
# Gathering the four signals
# ---------------------------------------------------------------------------


def gather(ec2, ssm_client, cw, logs_client, instance, region):
    """Observe one instance. Reads only; judges nothing.

    A signal that cannot be read is recorded as unreadable rather than assumed.
    Assuming False for an unreadable telemetry check would manufacture the
    exact signature this module exists to detect -- the platform reporting an
    agent killed because it could not see the metric. B-042 and its four
    successors, in the place where that mistake would be most expensive.
    """
    s = Signals(instance["InstanceId"], instance.get("_account"), region)

    state = (instance.get("State") or {}).get("Name")
    s.alive = state == "running"
    if not s.alive:
        # design 11's first row settles on the state event. No point asking the
        # other three, and asking would produce noise on every scale-in.
        s.telemetry = False
        return s

    try:
        checks = ec2.describe_instance_status(
            InstanceIds=[s.instance_id], IncludeAllInstances=True)
        st = (checks.get("InstanceStatuses") or [{}])[0]
        s.status_checks = (
            (st.get("InstanceStatus") or {}).get("Status") == "ok"
            and (st.get("SystemStatus") or {}).get("Status") == "ok")
    except Exception:                              # noqa: BLE001
        s.unreadable.append("status_checks")

    try:
        ping = ssm_client.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [s.instance_id]}])
        info = (ping.get("InstanceInformationList") or [{}])[0]
        s.ssm = info.get("PingStatus") == "Online"
    except Exception:                              # noqa: BLE001
        s.unreadable.append("ssm_ping")

    # Telemetry: the sentinel metric, and log ingestion as the second half.
    # design 11 asks for both -- a metric can lapse while logs still flow if
    # only part of the agent configuration was removed.
    since = _now() - datetime.timedelta(minutes=GRACE_MINUTES)
    try:
        points = cw.get_metric_statistics(
            Namespace=SENTINEL_NAMESPACE, MetricName=SENTINEL_METRIC,
            Dimensions=[{"Name": "InstanceId", "Value": s.instance_id}],
            StartTime=since, EndTime=_now(), Period=300,
            Statistics=["Average"])
        s.telemetry = bool(points.get("Datapoints"))
    except Exception:                              # noqa: BLE001
        s.unreadable.append("sentinel_metric")

    # The second half of the telemetry signal. design 11: "Log stream
    # staleness check on lastIngestionTime per expected stream. CloudWatch
    # Logs does not alarm on absence natively, so this is a scheduled check."
    #
    # OR, not AND. Either channel still flowing means the agent is alive, and
    # requiring both would report a kill whenever somebody trimmed the log
    # config. The two together answer "is the agent running"; telling apart
    # which half stopped is a different question and not this alarm's.
    if s.telemetry is False:
        try:
            if _recent_log_ingestion(logs_client, s.instance_id, since):
                s.telemetry = True
        except LookupError:
            # No stream for this instance. Common and not an error: an
            # instance can legitimately ship metrics and no logs. The sentinel
            # metric has already answered.
            pass
        except Exception:                          # noqa: BLE001
            s.unreadable.append("log_ingestion")

    # Flow logs: traffic is what makes "alive" mean more than "the hypervisor
    # says so". The question is whether packets moved, not whether logging is
    # configured.
    try:
        s.traffic = _recent_traffic(cw, s.instance_id, since)
    except Exception:                              # noqa: BLE001
        s.unreadable.append("flow_logs")

    return s


def _recent_log_ingestion(logs_client, instance_id, since):
    """Has any log stream for this instance ingested since `since`?

    lastIngestionTime rather than reading records: the question is whether
    anything arrived, which does not need the contents, and a Logs Insights
    query per instance per run would be slow and costly for a boolean.
    """
    group = os.environ.get("APP_LOG_GROUP_PREFIX", "/platform/")
    cutoff = int(since.timestamp() * 1000)
    found = False
    paginator = logs_client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=group):
        for lg in page.get("logGroups") or []:
            streams = logs_client.describe_log_streams(
                logGroupName=lg["logGroupName"],
                logStreamNamePrefix=instance_id, limit=5).get("logStreams") or []
            for st in streams:
                found = True
                if (st.get("lastIngestionTime") or 0) >= cutoff:
                    return True
    if not found:
        raise LookupError(f"no log stream for {instance_id}")
    return False


def _recent_traffic(cw, instance_id, since):
    """Has this instance moved packets since `since`?

    NetworkPacketsIn from AWS/EC2, NOT VPC flow logs -- and the substitution is
    deliberate rather than convenient.

    design 11 names flow logs, and the property its table actually relies on is
    the one in the signal-dependency table above it: flow logs do not require
    the agent. `NetworkPacketsIn` is published by the hypervisor and shares that
    property exactly, while being one CloudWatch call instead of an Athena
    query.

    Flow logs on this platform land in S3 (baseline/40-network.yaml,
    LogDestinationType: s3), so there is no log group to read staleness from.
    The first version of this function looked for one, found nothing, and
    marked the signal unreadable for every instance -- which would have meant
    no instance ever matched a row and the detector silently never fired.
    Exactly the failure it exists to catch, in itself. B-100.

    Flow logs remain the authoritative record and are what answers the port 22
    question, which needs Athena over S3 and is tracked separately.
    """
    points = cw.get_metric_statistics(
        Namespace="AWS/EC2", MetricName="NetworkPacketsIn",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=since, EndTime=_now(), Period=300, Statistics=["Sum"])
    datapoints = points.get("Datapoints") or []
    if not datapoints:
        # No datapoints at all is not "no traffic" -- it is no evidence. The
        # caller records it as unreadable rather than as silence.
        raise LookupError(f"no NetworkPacketsIn datapoints for {instance_id}")
    return any(d.get("Sum", 0) > 0 for d in datapoints)


# ---------------------------------------------------------------------------
# Noise control. design 11: required, not optional.
# ---------------------------------------------------------------------------


def suppressed(instance, ssm_client):
    """Is this instance in a state where silence is expected?

    Returns a reason string, or None.

    design 11: "Recognise scheduled-down state explicitly. An environment
    stopped by its HOOP must be a known state, not left to look like an
    anomaly -- otherwise every turndown is a false positive."
    """
    tags = {t["Key"]: t["Value"] for t in (instance.get("Tags") or [])}

    # HOOP turndown. The scheduling layer tags what it stops, so this reads the
    # same fact that turned the instance off rather than inferring it from the
    # hour -- which would be wrong in every timezone but one.
    if tags.get("platform:scheduled-down", "").lower() == "true":
        return "instance is in a HOOP scheduled-down window"

    # Recently launched: an instance that booted two minutes ago has not had
    # time to install an agent, and the association may not have run yet.
    launch = instance.get("LaunchTime")
    if launch and (_now() - launch).total_seconds() < GRACE_MINUTES * 60:
        return ("instance launched less than " + str(GRACE_MINUTES)
                + " minutes ago; the agent association may not have run yet")

    # An active maintenance window. Patching reboots instances, and a reboot
    # looks exactly like a stopped agent for as long as it takes.
    try:
        windows = ssm_client.describe_maintenance_windows(
            Filters=[{"Key": "Enabled", "Values": ["true"]}])
        for w in windows.get("WindowIdentities") or []:
            if w.get("Enabled") and _window_active(w):
                return "maintenance window " + str(w.get("Name")) + " is active"
    except Exception:                              # noqa: BLE001
        # Unreadable windows must NOT silently disable suppression -- that
        # would turn every patch cycle into a page. Treated as suppressing,
        # and the reason says so rather than reporting a clean scan.
        return ("maintenance windows could not be read; suppressing rather "
                "than risking a page during patching")

    return None


def _window_active(window):
    nxt = window.get("NextExecutionTime")
    if not isinstance(nxt, datetime.datetime):
        return False
    duration = int(window.get("Duration") or 0)
    started = nxt - datetime.timedelta(hours=duration)
    return started <= _now() <= nxt


def survived_association(ssm_client, instance_id, since):
    """Has the agent association run since the gap opened, with the gap held?

    design 11: "Escalate against the SSM association -- the association
    reinstalls on schedule, so a gap that SURVIVES the next association run is
    the real signal."

    That is the difference between "somebody killed the agent" and "somebody
    killed the agent and it stayed dead", and it separates an immediate
    low-severity notice from a page.

    Returns True, False, or None when it cannot be determined -- which is not
    the same as False and must not escalate on its own.
    """
    try:
        statuses = ssm_client.describe_instance_associations_status(
            InstanceId=instance_id).get("InstanceAssociationStatusInfos") or []
    except Exception:                              # noqa: BLE001
        return None
    for a in statuses:
        name = (a.get("AssociationName") or "").lower()
        if "cloudwatch" not in name:
            continue
        when = a.get("ExecutionDate")
        if isinstance(when, datetime.datetime) and when >= since:
            # The association ran after the gap opened and telemetry is still
            # absent, so a reinstall did not fix it.
            return True
    return False


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def attribute(ct, instance_id, since):
    """Who started a session on this instance before the telemetry stopped?

    design 11: "Session Manager sessions are CloudTrail events. StartSession
    carries principal, instance and timestamp -- so a correlation rule
    (session started, telemetry ceased within N minutes, instance still alive)
    gives an attributed finding."

    And the other half of that paragraph, which is why this returns None so
    often and should: SSH is NOT a CloudTrail event. OS auth logs ship via the
    agent, so if the agent died the login evidence died with it. An
    unattributed finding here is therefore not a failure of this function --
    it may be the most interesting thing about the finding.
    """
    try:
        events = ct.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventName",
                               "AttributeValue": "StartSession"}],
            StartTime=since, MaxResults=50).get("Events") or []
    except Exception:                              # noqa: BLE001
        return None, "CloudTrail could not be read; attribution unavailable"

    for e in events:
        try:
            detail = json.loads(e.get("CloudTrailEvent") or "{}")
        except ValueError:
            continue
        target = ((detail.get("requestParameters") or {}).get("target") or "")
        if instance_id not in target:
            continue
        identity = detail.get("userIdentity") or {}
        return {
            # accountId and ARN, never a name or an address. Same rule as the
            # quorum evidence: this record is retained and read later.
            "principal_arn": identity.get("arn"),
            "session_started": detail.get("eventTime"),
            "event_id": detail.get("eventID"),
        }, None

    return None, (
        "no Session Manager session on this instance in the window. That is "
        "not an absence of cause: SSH is not a CloudTrail event, and if the "
        "agent died the OS auth log died with it. Flow logs show a session; "
        "they do not show an identity.")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _tier(account_id):
    """dev / test / uat / prod. Unknown returns prod, which pages.

    Same direction as the router's: treating an unknown account as dev would
    silence a real production finding, and treating it as prod produces a page
    that says the tier was unknown. B-073.
    """
    try:
        raw = ssm.get_parameter(Name=ACCOUNT_TIER_PARAM)["Parameter"]["Value"]
        return (json.loads(raw) or {}).get(str(account_id), "prod")
    except Exception:                              # noqa: BLE001
        return "prod"


def _emit(finding):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.telemetry",
        "DetailType": "telemetry-gap",
        "Detail": json.dumps(finding, default=str),
    }])


def _accounts():
    """Accounts to scan, from the tier map. B-086: raise rather than return
    empty, because a scan over zero accounts reports a clean estate."""
    raw = ssm.get_parameter(Name=ACCOUNT_TIER_PARAM)["Parameter"]["Value"]
    mapping = json.loads(raw) or {}
    out = [a for a in mapping if str(a).isdigit()]
    if not out:
        raise RuntimeError(
            f"{ACCOUNT_TIER_PARAM} lists no accounts. Nothing was scanned, "
            "which is not the same as nothing being wrong.")
    return out


def _clients(account_id, region):
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{MEMBER_ROLE}",
        RoleSessionName="telemetry-gap-scan")["Credentials"]
    kw = {"region_name": region,
          "aws_access_key_id": creds["AccessKeyId"],
          "aws_secret_access_key": creds["SecretAccessKey"],
          "aws_session_token": creds["SessionToken"]}
    return (boto3.client("ec2", **kw), boto3.client("ssm", **kw),
            boto3.client("cloudwatch", **kw), boto3.client("logs", **kw),
            boto3.client("cloudtrail", **kw))


def scan(event=None, context=None):
    """One pass over every instance that should be publishing telemetry.

    The expectation registry is `platform-managed=true` -- the same tag the
    CloudWatch agent association targets. Derived rather than maintained
    separately: an instance the association installs onto is exactly an
    instance that should be publishing, and two lists of that would drift.
    """
    region = os.environ.get("AWS_REGION", "us-east-2")
    since = _now() - datetime.timedelta(minutes=GRACE_MINUTES)

    try:
        accounts = _accounts()
    except Exception as exc:                       # noqa: BLE001
        out = {"record_type": "telemetry-gap-scan", "scanned": False,
               "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    findings, examined, suppressed_count, unreadable = [], 0, 0, []

    for account_id in sorted(accounts):
        try:
            ec2, ssm_c, cw, logs_c, ct = _clients(account_id, region)
        except Exception as exc:                   # noqa: BLE001
            unreadable.append({"account_id": account_id,
                               "problem": f"{type(exc).__name__}"})
            continue

        try:
            reservations = ec2.describe_instances(Filters=[
                {"Name": "tag:platform-managed", "Values": ["true"]},
            ]).get("Reservations") or []
        except Exception as exc:                   # noqa: BLE001
            unreadable.append({"account_id": account_id,
                               "problem": f"{type(exc).__name__}"})
            continue

        for r in reservations:
            for inst in r.get("Instances") or []:
                inst["_account"] = account_id
                examined += 1

                reason = suppressed(inst, ssm_c)
                if reason:
                    suppressed_count += 1
                    continue

                sig = gather(ec2, ssm_c, cw, logs_c, inst, region)
                scenario, note = classify(sig)
                if not scenario or not scenario.get("alarm"):
                    continue

                who, why_not = attribute(ct, sig.instance_id, since)
                persisted = survived_association(ssm_c, sig.instance_id, since)
                tier = _tier(account_id)

                finding = {
                    "scenario": scenario["name"],
                    "note": note,
                    "signals": sig.as_dict(),
                    "attribution": who,
                    "attribution_note": why_not,
                    "survived_association": persisted,
                    "tier": tier,
                }
                findings.append(finding)

                # design 11: "In dev, this is a digest line and a manager
                # notification, not a page. It is a visibility failure, not an
                # outage." And: "Immediate low-severity notice; page only on
                # persistence or recurrence."
                #
                # So two conditions have to hold before this pages: production,
                # AND a gap that outlived the association reinstall.
                if tier == "dev":
                    routing = "digest"
                elif persisted is True:
                    routing = "pagerduty"
                else:
                    routing = "digest_and_jira"

                _emit({
                    "record_type": "telemetry-gap-finding",
                    "recorded_at": _iso(_now()),
                    "current_state": "ALARM",
                    "routing": routing,
                    "alarm": {"name": f"platform-telemetry-{scenario['name']}",
                              "resource_id": sig.instance_id,
                              "account_id": account_id},
                    "assessment": {"probable_cause": note},
                    "telemetry_gap": finding,
                })

    for gapped in unreadable:
        _emit({
            "record_type": "telemetry-gap-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": "review_queue",
            "alarm": {"name": "platform-telemetry-scan-incomplete",
                      "resource_id": gapped["account_id"],
                      "account_id": gapped["account_id"]},
            "assessment": {"probable_cause": (
                f"Instances in {gapped['account_id']} could not be examined "
                f"({gapped['problem']}). No conclusion can be drawn about "
                "that account, and this scan covers less of the estate than "
                "its other results imply.")},
        })

    out = {
        "record_type": "telemetry-gap-scan",
        "scanned": True,
        "scanned_at": _iso(_now()),
        "accounts": len(accounts),
        "accounts_unreadable": len(unreadable),
        "instances_examined": examined,
        "suppressed": suppressed_count,
        "findings": len(findings),
        "grace_minutes": GRACE_MINUTES,
    }
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"scan": scan}


def handler(event, context):
    return scan(event, context)
