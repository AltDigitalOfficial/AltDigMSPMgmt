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

    # Flow logs: traffic is what makes "alive" mean more than "the hypervisor
    # says so". The question is whether packets moved, not whether logging is
    # configured.
    try:
        s.traffic = _recent_traffic(logs_client, s.instance_id, since)
    except Exception:                              # noqa: BLE001
        s.unreadable.append("flow_logs")

    return s


def _recent_traffic(logs_client, instance_id, since):
    """Has this instance's ENI logged traffic since `since`?

    Uses the log stream's lastIngestionTime rather than querying records.
    design 11 asks for a staleness check on lastIngestionTime anyway, and a
    Logs Insights query per instance per run would be slow and expensive for a
    question this coarse: "did anything arrive" does not need the contents.
    """
    group = os.environ.get("FLOW_LOG_GROUP", "/platform/vpc/flowlogs")
    cutoff = int(since.timestamp() * 1000)
    streams = logs_client.describe_log_streams(
        logGroupName=group, orderBy="LastEventTime", descending=True,
        limit=50).get("logStreams") or []
    for stream in streams:
        if instance_id in stream.get("logStreamName", ""):
            return (stream.get("lastIngestionTime") or 0) >= cutoff
    # No stream for this instance at all. That is not "no traffic" -- it is no
    # evidence either way, and the caller must not read a missing stream as
    # silence.
    raise LookupError("no flow log stream found for " + instance_id)


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
