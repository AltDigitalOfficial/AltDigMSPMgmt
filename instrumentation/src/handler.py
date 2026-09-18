"""Auto-instrumentation: apply the standard alarm set to newly created resources.

Prompt 4.1. EventBridge triggers this on resource creation; it resolves the
alarm set for the resource type and environment tier from
design/06a-alarm-specification.yaml, creates the alarms, tags them
platform-managed, and records what it did.

------------------------------------------------------------------------------
The specification is the config. There is no second copy.
------------------------------------------------------------------------------
    "The alarm sets are specified in design/06a-alarm-specification.yaml. Load
    that file as configuration — do not transcribe it into code."

So the packaging step converts that exact file to JSON and ships it. An earlier
version of this handler used a separate instrumentation/alarm-sets.yaml written
before 06a existed; that file is gone. A second copy of an authoritative
document is worse than no copy, because the two disagree silently and the wrong
one is always the one someone edits.

------------------------------------------------------------------------------
Everything else follows from one sentence
------------------------------------------------------------------------------
    "Critical: anything it cannot handle produces an exception record. It must
    never fail silently. An unrecognised resource type is an exception, not a
    no-op."

That rules out the natural shape of this function — look up, return early if
absent — because returning early IS failing silently. Every path out either
created the alarms it was meant to, or wrote down why it did not.

The distinction the spec draws, and that this code has to preserve exactly:

  tier absent from threshold_by_tier   SUPPRESSED. A deliberate decision,
                                       recorded in the spec. Silent.
  threshold present but null           EXCEPTION. A [TUNE] value nobody has
                                       decided yet. The alarm cannot be
                                       created and that must be visible.
  applies_when false                   NOT APPLICABLE. Recorded, not silent —
                                       "this alarm does not apply to this
                                       resource" is different from "this
                                       resource has no alarms".
  applies_when unevaluable             EXCEPTION. We do not know whether it
                                       applies, so we do not know whether the
                                       resource is monitored.
  threshold_mode unresolvable          EXCEPTION. A relative threshold that
                                       cannot be resolved must not fall back to
                                       a fixed number — "a fixed number is
                                       wrong across instance classes".

The first three are the same outcome (no alarm) reached three different ways,
and collapsing them would destroy the only signal that distinguishes a
considered omission from an unmade decision.
"""

import datetime
import json
import os

import boto3
from botocore.exceptions import ClientError

SPEC_PATH = os.path.join(os.path.dirname(__file__), "alarm-spec.json")

EXCEPTION_LOG_GROUP = os.environ.get(
    "EXCEPTION_LOG_GROUP", "/aws/platform/instrumentation-exceptions"
)
# dev | test | uat | prod. The spec keys thresholds on the environment name
# directly, which is simpler and more honest than the full/reduced monitoring
# tiers an earlier version of this invented while design doc 14 was missing.
ENVIRONMENT_TIER = os.environ.get("ENVIRONMENT_TIER", "dev")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")

# Where alarms whose 06a routing does NOT page are sent.
#
# 06a routes dev to `digest` and test to `digest_and_jira`. Neither destination
# exists yet (phases 4.4 and 8), so those alarms were previously created with
# no actions — real, evaluating, heard by nobody.
#
# They now go to a low-urgency PagerDuty service instead. That is a different
# thing from pointing them at the paging topic: a low-urgency incident does not
# ring a phone, it lands in the UI under the responder's low-urgency
# notification rules. So the distinction 06a draws between paging and
# not-paging is preserved, and the alarms stop being silent.
#
# INTERIM. When the digest exists, dev and test move there and this becomes the
# fallback for anything with no destination.
LOW_URGENCY_TOPIC_ARN = os.environ.get("LOW_URGENCY_TOPIC_ARN", "")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "platform-auto")

# questionnaire 7.4, passed in by the vesting pipeline rather than read from the
# registry here.
#
# The prompt says to read it from the onboarding record, and the pipeline does
# exactly that — it then passes the value in as a stack parameter. The
# alternative was a cross-account DynamoDB read from a Lambda in every member
# account, which would mean granting every member account read access to the
# registry that describes every other tenant. The value is a number of
# milliseconds; the blast radius of that grant is the whole platform.
ACCEPTABLE_RESPONSE_MS = os.environ.get("ACCEPTABLE_RESPONSE_MS", "")

cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")

with open(SPEC_PATH, encoding="utf-8") as fh:
    SPEC = json.load(fh)

TIERS = SPEC.get("tiers", {})
ROUTING = SPEC.get("routing_destinations", {})
DEFAULTS = SPEC.get("defaults", {})
ALARM_SETS = SPEC.get("alarm_sets", {})
IN_SCOPE = set(SPEC.get("resource_types_in_scope", []))
NO_ALARMS = set(SPEC.get("resource_types_no_alarms", []))

# There is deliberately no hardcoded ignore list here.
#
# An earlier version carried CONFIG_PSEUDO_TYPES in code. Those types now live
# in 06a's resource_types_no_alarms, which is the mechanism the specification
# defines for "known, and deliberately not instrumented" — and keeping a second
# list in code would recreate the two-copies problem that deleting
# instrumentation/alarm-sets.yaml removed.


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def record_exception(reason, detail):
    """Write an exception record. Re-raises if it cannot.

    If the record cannot be written there is no remaining way to report
    anything, and a Lambda invocation error is the only signal left —
    CloudWatch counts it and the Lambda Errors alarm catches it. Swallowing
    this would reproduce, one level up, the exact silent failure the function
    exists to prevent.
    """
    record = {
        "record_type": "instrumentation-exception",
        "recorded_at": _now().isoformat(),
        "reason": reason,
        "environment_tier": ENVIRONMENT_TIER,
        "detail": detail,
    }
    stream = _now().strftime("%Y/%m/%d")
    try:
        try:
            logs.create_log_stream(logGroupName=EXCEPTION_LOG_GROUP, logStreamName=stream)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_log_events(
            logGroupName=EXCEPTION_LOG_GROUP,
            logStreamName=stream,
            logEvents=[{"timestamp": int(_now().timestamp() * 1000),
                        "message": json.dumps(record)}],
        )
    except Exception:
        print(json.dumps({"FATAL_could_not_record_exception": record}))
        raise
    print(json.dumps(record))


# ---------------------------------------------------------------------------
# Resolving the alarm set, including inheritance
# ---------------------------------------------------------------------------


def resolve_set(resource_type, _seen=None):
    """Return the alarm list for a type, following `inherits`.

    "inherits on a resource type means take the parent's set plus
    additional_alarms."

    Cycle-guarded. A spec with A inheriting B inheriting A would otherwise
    recurse until the Lambda dies, and the resulting timeout produces no
    exception record because the handler never reaches the code that writes
    one.
    """
    _seen = _seen or set()
    if resource_type in _seen:
        raise ValueError(f"inherits cycle at {resource_type}")
    _seen.add(resource_type)

    cfg = ALARM_SETS.get(resource_type)
    if cfg is None:
        return None, {}

    parent_name = cfg.get("inherits")
    alarms = []
    if parent_name:
        parent_alarms, _ = resolve_set(parent_name, _seen)
        if parent_alarms is None:
            raise ValueError(
                f"{resource_type} inherits {parent_name}, which has no alarm set"
            )
        alarms.extend(parent_alarms)
    alarms.extend(cfg.get("alarms") or [])
    alarms.extend(cfg.get("additional_alarms") or [])
    return alarms, cfg


# ---------------------------------------------------------------------------
# Live resource introspection
# ---------------------------------------------------------------------------
# Both applies_when and the relative threshold modes need the real resource.
# Each helper returns None when it cannot determine an answer, and None always
# becomes an exception rather than a default — not knowing whether an alarm
# applies is not the same as knowing it does not.


def _rds(resource_id):
    try:
        r = boto3.client("rds").describe_db_instances(DBInstanceIdentifier=resource_id)
        return r["DBInstances"][0]
    except (ClientError, IndexError, KeyError):
        return None


def _lambda_cfg(resource_id):
    try:
        return boto3.client("lambda").get_function_configuration(FunctionName=resource_id)
    except (ClientError, KeyError):
        return None


def resolve_threshold(spec, resource_type, resource_id, tier_value):
    """Turn a relative threshold into an absolute one against the live resource.

    Returns (value, None) or (None, reason_string).

    "A fixed number is wrong across instance classes" — so a mode that cannot
    be resolved returns a reason and the caller raises an exception. There is
    deliberately no fallback to the percentage as a literal.
    """
    mode = spec.get("threshold_mode")
    if not mode:
        return tier_value, None

    if mode == "percent_of_allocated":
        db = _rds(resource_id)
        if not db or "AllocatedStorage" in db and db["AllocatedStorage"] is None:
            return None, "could not read AllocatedStorage from the DB instance"
        if not db:
            return None, "could not describe the DB instance"
        # AllocatedStorage is GiB; FreeStorageSpace is bytes.
        return db["AllocatedStorage"] * 1024 ** 3 * (tier_value / 100.0), None

    if mode == "percent_of_configured_timeout":
        fn = _lambda_cfg(resource_id)
        if not fn or "Timeout" not in fn:
            return None, "could not read the function's configured timeout"
        # Timeout is seconds; the Duration metric is milliseconds.
        return fn["Timeout"] * 1000 * (tier_value / 100.0), None

    if mode == "percent_of_account_limit":
        try:
            limit = boto3.client("lambda").get_account_settings()[
                "AccountLimit"]["ConcurrentExecutions"]
        except (ClientError, KeyError):
            return None, "could not read the account concurrency limit"
        return limit * (tier_value / 100.0), None

    if mode in ("percent_of_max_connections", "percent_of_instance_memory"):
        # NOT RESOLVED, and deliberately not guessed.
        #
        # Both need the memory of the DB instance class. RDS exposes no API for
        # it, and max_connections defaults to the parameter-group FORMULA
        # {DBInstanceClassMemory/12582880} rather than a number — so reading
        # the parameter group returns the formula, not the value.
        #
        # The options were a hand-maintained db.*.* class-to-memory table,
        # which is silently wrong the first time AWS ships a class nobody has
        # added, or an exception. An exception is correct: it says the alarm
        # was not created and why, which a wrong threshold does not.
        #
        # The fix is to set an explicit numeric max_connections in the
        # parameter group, at which point this becomes resolvable. Tracked as
        # B-025.
        return None, (
            f"threshold_mode '{mode}' needs the DB instance class memory, which RDS "
            "does not expose and which max_connections defaults to a parameter-group "
            "formula for. Set an explicit numeric value in the parameter group, or "
            "give this alarm an absolute threshold. See B-025."
        )

    if mode == "reference_metric":
        # Handled by the caller as a metric math alarm, not a threshold.
        return tier_value, None

    return None, f"unknown threshold_mode '{mode}'"


APPLIES_WHEN_RESOLVERS = {
    "read_replica": lambda rt, rid: (
        None if _rds(rid) is None
        else bool(_rds(rid).get("ReadReplicaSourceDBInstanceIdentifier"))
    ),
    "dlq_configured": lambda rt, rid: (
        None if _lambda_cfg(rid) is None
        else bool((_lambda_cfg(rid).get("DeadLetterConfig") or {}).get("TargetArn"))
    ),
}


def applies(spec, resource_type, resource_id):
    """Evaluate applies_when. Returns (True|False, None) or (None, reason)."""
    conds = spec.get("applies_when")
    if not conds:
        return True, None
    for key, expected in conds.items():
        resolver = APPLIES_WHEN_RESOLVERS.get(key)
        if resolver is None:
            # Unevaluable, so we do not know whether this resource is meant to
            # carry this alarm. Not the same as "does not apply".
            return None, (
                f"applies_when condition '{key}' has no resolver. The alarm was not "
                f"created because it is not known whether it should be. Add a resolver, "
                f"or give the alarm an absolute applicability in the spec."
            )
        actual = resolver(resource_type, resource_id)
        if actual is None:
            return None, f"could not evaluate applies_when '{key}' against {resource_id}"
        if actual != expected:
            return False, None
    return True, None


def routing_for(spec):
    """severity_by_tier overrides the tier's default_routing."""
    sev = spec.get("severity_by_tier") or {}
    if ENVIRONMENT_TIER in sev:
        return sev[ENVIRONMENT_TIER]
    return (TIERS.get(ENVIRONMENT_TIER) or {}).get("default_routing", "digest")


def handler(event, context):
    detail = event.get("detail", {}) or {}
    ci = detail.get("configurationItem") or detail.get("configurationItemSummary") or {}
    resource_type = ci.get("resourceType") or detail.get("resourceType")
    resource_id = ci.get("resourceId") or detail.get("resourceId")

    if not resource_type:
        record_exception("unparseable-event", {
            "message": "No resource type in the event. The handler reads AWS Config "
                       "configuration items; guessing at CloudTrail responseElements "
                       "would create alarms on the wrong dimension.",
            "event": event})
        return {"status": "exception", "reason": "unparseable-event"}

    if resource_type in NO_ALARMS:
        # The spec's own words: "known, no alarms applicable". Recorded in the
        # return value, not raised, because the decision is in the spec.
        print(json.dumps({"status": "known-no-alarms", "resource_type": resource_type}))
        return {"status": "known-no-alarms", "resource_type": resource_type}

    if resource_type not in IN_SCOPE:
        record_exception("unrecognised-resource-type", {
            "resource_type": resource_type, "resource_id": resource_id,
            "message": "Not in resource_types_in_scope and not in "
                       "resource_types_no_alarms. This resource is running with no "
                       "platform monitoring. Add it to one of those lists in "
                       "design/06a-alarm-specification.yaml — the second list is how "
                       "a deliberate omission is recorded."})
        return {"status": "exception", "reason": "unrecognised-resource-type"}

    try:
        alarms, type_cfg = resolve_set(resource_type)
    except ValueError as exc:
        record_exception("spec-error", {"resource_type": resource_type, "error": str(exc)})
        return {"status": "exception", "reason": "spec-error"}

    if not alarms:
        record_exception("no-alarm-set", {
            "resource_type": resource_type,
            "message": "In scope per the spec but alarm_sets has no entry for it."})
        return {"status": "exception", "reason": "no-alarm-set"}

    created, suppressed, not_applicable, exceptions = [], [], [], []
    pending_destination, account_level = [], []

    for spec in alarms:
        aid = spec.get("id", "?")

        # Event-based entries are not metric alarms. 06a says so explicitly:
        # "EventBridge rule, not a metric alarm." They carry a source
        # (rds_event_category, cloudtrail, autoscaling_event) and a selector
        # (event_categories, event_names or event_types) instead of a
        # threshold.
        #
        # They are also not PER-RESOURCE. A CloudTrail rule matching
        # PutBucketAcl belongs once in the account, not once per bucket, and
        # creating one per resource would produce N identical rules all firing
        # together on the same event.
        #
        # So they are classified here and built at account level in the
        # baseline template. NOT an exception — an exception per resource for
        # something correctly handled elsewhere is the noise that buries real
        # ones. The account-level rules do not exist yet; that is B-026, not a
        # per-resource fault.
        if spec.get("metric") == "event":
            account_level.append(aid)
            continue

        by_tier = spec.get("threshold_by_tier") or {}

        # Tier omitted -> suppressed by decision. Silent, per the spec.
        if ENVIRONMENT_TIER not in by_tier:
            suppressed.append(aid)
            continue

        tier_value = by_tier[ENVIRONMENT_TIER]

        # Present but null -> [TUNE] nobody has set. Exception, not a skip.
        if tier_value is None:
            record_exception("threshold-unset", {
                "alarm_id": aid, "resource_type": resource_type,
                "resource_id": resource_id,
                "message": "threshold_by_tier gives null for this tier. The spec marks "
                           "such values [TUNE]: they are workload-specific and nobody "
                           "has decided this one. The alarm CANNOT be created, and that "
                           "is an exception rather than a skip so it shows as pending."})
            exceptions.append({"alarm": aid, "reason": "threshold-unset"})
            continue

        ok, why = applies(spec, resource_type, resource_id)
        if ok is None:
            record_exception("applies-when-unevaluable", {
                "alarm_id": aid, "resource_type": resource_type,
                "resource_id": resource_id, "message": why})
            exceptions.append({"alarm": aid, "reason": "applies-when-unevaluable"})
            continue
        if ok is False:
            not_applicable.append(aid)
            continue

        threshold, why = resolve_threshold(spec, resource_type, resource_id, tier_value)
        if threshold is None:
            record_exception("threshold-unresolvable", {
                "alarm_id": aid, "resource_type": resource_type,
                "resource_id": resource_id, "threshold_mode": spec.get("threshold_mode"),
                "message": why})
            exceptions.append({"alarm": aid, "reason": "threshold-unresolvable"})
            continue

        if spec.get("threshold_mode") == "reference_metric":
            # Needs a metric math alarm (this metric against a reference
            # metric), which is a different put_metric_alarm shape entirely —
            # Metrics=[...] rather than MetricName/Threshold. Not built, and
            # recorded rather than approximated, because approximating it would
            # produce an alarm that looks right and compares the wrong things.
            record_exception("threshold-mode-not-implemented", {
                "alarm_id": aid, "resource_type": resource_type,
                "resource_id": resource_id,
                "message": "reference_metric requires a metric math alarm, which this "
                           "handler does not yet build. See B-025."})
            exceptions.append({"alarm": aid, "reason": "reference-metric-unimplemented"})
            continue

        routing = routing_for(spec)
        pages = (ROUTING.get(routing) or {}).get("pages", False)

        if pages:
            actions = [ALERT_TOPIC_ARN] if ALERT_TOPIC_ARN else []
        elif LOW_URGENCY_TOPIC_ARN:
            actions = [LOW_URGENCY_TOPIC_ARN]
            pending_destination.append(
                {"alarm": aid, "routing": routing, "sent_to": "low-urgency"})
        else:
            # No destination at all. The alarm is still created — it evaluates
            # and is visible in the console — but nothing is notified, and that
            # is recorded rather than left to be inferred from an empty
            # AlarmActions list.
            actions = []
            pending_destination.append(
                {"alarm": aid, "routing": routing, "sent_to": "nowhere"})

        name = f"{ALARM_PREFIX}-{resource_id}-{aid}".replace("/", "-")[:255]
        params = {
            "AlarmName": name,
            "AlarmDescription": (
                f"{spec.get('note') or spec.get('metric')}\n\n"
                f"Auto-instrumented from design/06a-alarm-specification.yaml "
                f"({aid}) at tier '{ENVIRONMENT_TIER}', routing '{routing}'. "
                f"Edit the specification, not this alarm."),
            "Namespace": spec.get("namespace"),
            "MetricName": spec["metric"],
            "Dimensions": [{"Name": type_cfg.get("dimension") or _dimension_for(resource_type),
                            "Value": resource_id}],
            "Statistic": spec.get("statistic", "Average"),
            "Period": spec.get("period_seconds", DEFAULTS.get("period_seconds", 300)),
            "EvaluationPeriods": spec.get(
                "evaluation_periods", DEFAULTS.get("evaluation_periods", 2)),
            "Threshold": threshold,
            "ComparisonOperator": spec["comparison"],
            "TreatMissingData": spec.get(
                "treat_missing_data", DEFAULTS.get("treat_missing_data", "notBreaching")),
            "Tags": [
                {"Key": "platform-managed", "Value": "true"},
                {"Key": "platform-auto-instrumented", "Value": "true"},
                {"Key": "platform-alarm-id", "Value": aid},
                {"Key": "platform-routing", "Value": routing},
            ],
        }
        if spec.get("datapoints_to_alarm"):
            params["DatapointsToAlarm"] = spec["datapoints_to_alarm"]
        if spec.get("unit"):
            params["Unit"] = spec["unit"]
        if actions:
            params["AlarmActions"] = actions
            params["OKActions"] = actions

        try:
            cloudwatch.put_metric_alarm(**params)
            created.append(aid)
        except Exception as exc:
            # Carry on through the rest of the set. Aborting would leave the
            # resource partially instrumented AND stop alarms that would have
            # worked, for a failure usually specific to one metric.
            exceptions.append({"alarm": aid, "reason": "put-failed", "error": str(exc)})

    if any(e["reason"] == "put-failed" for e in exceptions):
        record_exception("alarm-creation-failed", {
            "resource_type": resource_type, "resource_id": resource_id,
            "created": created,
            "failed": [e for e in exceptions if e["reason"] == "put-failed"]})

    # Four outcomes, not three. The first version collapsed "nothing was
    # created" into "exception", so an S3 bucket in dev — where every alarm in
    # its set is either suppressed by tier or handled by an account-level event
    # rule — reported status "exception" with an empty exceptions list.
    #
    # That is the same mistake this handler exists to avoid, made one level up:
    # a resource with no alarms BY DESIGN read identically to one that failed
    # to be instrumented.
    if exceptions and created:
        status = "partial"
    elif exceptions:
        status = "exception"
    elif created:
        status = "instrumented"
    else:
        status = "no-alarms-applicable"

    result = {
        "status": status,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "environment_tier": ENVIRONMENT_TIER,
        "created": created,
        # Three distinct reasons an alarm does not exist, kept apart on purpose.
        "suppressed_by_tier": suppressed,
        "not_applicable": not_applicable,
        "exceptions": exceptions,
        "pending_destination": pending_destination,
        "account_level_event_rules": account_level,
    }
    print(json.dumps(result))
    return result


def _dimension_for(resource_type):
    """Fallback dimension name when the spec's type block omits one.

    The spec carries `dimension` per resource type for most entries; this
    covers the rest rather than failing, and an unknown type here is
    impossible because IN_SCOPE was already checked.
    """
    return {
        "AWS::EC2::Instance": "InstanceId",
        "AWS::RDS::DBInstance": "DBInstanceIdentifier",
        "AWS::RDS::DBCluster": "DBClusterIdentifier",
        "AWS::Lambda::Function": "FunctionName",
        "AWS::DynamoDB::Table": "TableName",
        "AWS::SQS::Queue": "QueueName",
        "AWS::S3::Bucket": "BucketName",
        "AWS::EFS::FileSystem": "FileSystemId",
        "AWS::AutoScaling::AutoScalingGroup": "AutoScalingGroupName",
        "AWS::ElasticLoadBalancingV2::LoadBalancer": "LoadBalancer",
        "AWS::ElasticLoadBalancingV2::TargetGroup": "TargetGroup",
        "AWS::ECS::Service": "ServiceName",
        "AWS::ElastiCache::ReplicationGroup": "ReplicationGroupId",
        "AWS::ApiGateway::RestApi": "ApiName",
        "AWS::ApiGatewayV2::Api": "ApiId",
    }.get(resource_type, "ResourceId")
