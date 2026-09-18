"""Auto-instrumentation: apply the standard alarm set to newly created resources.

Prompt 4.1. EventBridge triggers this on resource creation; it looks the
resource type up in alarm-sets.yaml, creates the alarms for the account's
monitoring tier, tags them platform-managed, and records what it did.

------------------------------------------------------------------------------
The whole design follows from one sentence in the prompt
------------------------------------------------------------------------------
    "Critical: anything it cannot handle produces an exception record. It must
    never fail silently. An unrecognised resource type is an exception, not a
    no-op."

That is a stronger requirement than it first reads. It rules out the natural
shape of this kind of function — look up the type, return early if absent —
because returning early IS failing silently. It means every path out of this
handler either created the alarms it was supposed to, or wrote down why it
did not.

Hence:

  * an unknown resource type writes an exception, rather than returning
  * a type on the ignore list writes nothing, because the decision not to
    instrument it was made deliberately and recorded in the config; that is the
    only silent path, and it is silent by an explicit act
  * an alarm that fails to create writes an exception naming the alarm, and the
    handler carries on to the others rather than aborting the set
  * an exception that cannot be written is re-raised, because at that point
    there is no way to report anything and a Lambda error is the last remaining
    signal

Exceptions go to a dedicated log group with a metric filter and an alarm, so
"instrumentation is not working" is itself an alarm rather than something
discovered when an incident goes unnoticed.
"""

import datetime
import json
import os
import re

import boto3

# JSON, not YAML, and the config file is CONVERTED at package time by
# scripts/publish-instrumentation.sh.
#
# The Lambda Python runtime ships boto3 but not PyYAML. The options were to
# vendor PyYAML into the package for a single safe_load, or to author in YAML
# and ship JSON. Shipping JSON wins twice: no third-party dependency to audit
# or keep patched inside a function that creates alarms in every member
# account, and the explanatory comments in alarm-sets.yaml — which are most of
# its value — stay in the repository instead of being deployed.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "alarm-sets.json")

EXCEPTION_LOG_GROUP = os.environ.get(
    "EXCEPTION_LOG_GROUP", "/aws/platform/instrumentation-exceptions"
)
MONITORING_TIER = os.environ.get("MONITORING_TIER", "reduced")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "platform-auto")

cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CONFIG = json.load(fh)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def record_exception(reason, detail):
    """Write an exception record. Raises if it cannot.

    Re-raising is deliberate. If the exception record cannot be written there
    is no remaining way to report anything, and a Lambda invocation error is
    the only signal left — CloudWatch counts it, and the Lambda Errors alarm
    catches it. Swallowing the failure here would produce the exact silent
    failure this function exists to prevent, one level up.
    """
    record = {
        "record_type": "instrumentation-exception",
        "recorded_at": _now().isoformat(),
        "reason": reason,
        "detail": detail,
    }
    stream = _now().strftime("%Y/%m/%d")
    try:
        try:
            logs.create_log_stream(
                logGroupName=EXCEPTION_LOG_GROUP, logStreamName=stream
            )
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_log_events(
            logGroupName=EXCEPTION_LOG_GROUP,
            logStreamName=stream,
            logEvents=[
                {
                    "timestamp": int(_now().timestamp() * 1000),
                    "message": json.dumps(record),
                }
            ],
        )
    except Exception:
        print(json.dumps({"FATAL_could_not_record_exception": record}))
        raise
    print(json.dumps(record))
    return record


def resource_id_from_arn(arn, resource_type):
    """Extract the dimension value CloudWatch expects from an ARN.

    Not uniform across services, which is why this is a function rather than a
    split on the last colon:

      EC2 instance   arn:...:instance/i-abc        -> i-abc
      RDS instance   arn:...:db:my-db              -> my-db
      Lambda         arn:...:function:my-fn        -> my-fn
      ALB            arn:...:loadbalancer/app/x/id -> app/x/id   (not just id)

    The ALB case is the one that silently produces a useless alarm if handled
    generically: CloudWatch's LoadBalancer dimension is the whole
    `app/name/hash` suffix, and passing only the hash creates an alarm that
    matches no metric and sits in INSUFFICIENT_DATA.
    """
    if not arn:
        return None
    tail = arn.split(":")[-1]
    if resource_type == "AWS::ElasticLoadBalancingV2::LoadBalancer":
        m = re.search(r"loadbalancer/(.+)$", arn)
        return m.group(1) if m else None
    if "/" in tail:
        return tail.split("/", 1)[1] if tail.startswith("instance/") else tail.split("/")[-1]
    return tail


def extract(event):
    """Pull (resource_type, resource_id) out of an EventBridge event.

    Two shapes arrive here and they are not interchangeable:

      AWS Config  'configurationItem' carries resourceType and resourceId
                  directly. Reliable, but delayed by Config's own evaluation.
      CloudTrail  'detail.responseElements' — shape differs per API call, so
                  this only handles it as a fallback and raises an exception
                  rather than guessing.
    """
    detail = event.get("detail", {}) or {}

    ci = detail.get("configurationItem") or detail.get("configurationItemSummary")
    if ci:
        return ci.get("resourceType"), ci.get("resourceId"), ci.get("ARN")

    # Config rule / oversized item notifications reference the item instead.
    if detail.get("resourceType"):
        return detail.get("resourceType"), detail.get("resourceId"), detail.get("ARN")

    return None, None, None


def build_alarm(spec, cfg, resource_type, resource_id):
    namespace = spec.get("namespace", cfg["namespace"])
    name = f"{ALARM_PREFIX}-{resource_id}-{spec['name_suffix']}"
    # CloudWatch alarm names cap at 255 characters and allow a wide character
    # set, but a resource id containing a slash (the ALB case above) produces a
    # name that is legal and awkward to work with. Normalised rather than
    # rejected: rejecting would turn a naming nuisance into an uninstrumented
    # load balancer.
    name = name.replace("/", "-")[:255]

    params = {
        "AlarmName": name,
        "AlarmDescription": (
            f"Auto-instrumentation: {spec['metric']} on {resource_type} {resource_id}. "
            f"Created by the platform instrumentation Lambda from alarm-sets.yaml "
            f"at monitoring tier '{MONITORING_TIER}'. Edit the configuration, not "
            f"this alarm — drift detection will revert a hand edit."
        ),
        "Namespace": namespace,
        "MetricName": spec["metric"],
        "Dimensions": [{"Name": cfg["dimension"], "Value": resource_id}],
        "Statistic": spec["statistic"],
        "Period": spec["period"],
        "EvaluationPeriods": spec["evaluation_periods"],
        "Threshold": spec["threshold"],
        "ComparisonOperator": spec["comparison"],
        "TreatMissingData": spec["treat_missing_data"],
        "Tags": [
            {"Key": "platform-managed", "Value": "true"},
            {"Key": "platform-auto-instrumented", "Value": "true"},
            {"Key": "platform-resource-type", "Value": resource_type},
        ],
    }
    if ALERT_TOPIC_ARN:
        params["AlarmActions"] = [ALERT_TOPIC_ARN]
        params["OKActions"] = [ALERT_TOPIC_ARN]
    return params


def handler(event, context):
    resource_type, resource_id, arn = extract(event)

    if not resource_type:
        record_exception(
            "unparseable-event",
            {
                "message": (
                    "Could not determine a resource type from the event. The "
                    "handler understands AWS Config configuration items; this "
                    "was neither. Guessing at CloudTrail responseElements would "
                    "create alarms against the wrong dimension, which is worse "
                    "than not creating them."
                ),
                "event": event,
            },
        )
        return {"status": "exception", "reason": "unparseable-event"}

    if resource_type in (CONFIG.get("ignored_resource_types") or []):
        # The only silent path, and it is silent by explicit decision recorded
        # in the configuration rather than by omission.
        print(json.dumps({"status": "ignored", "resource_type": resource_type}))
        return {"status": "ignored", "resource_type": resource_type}

    cfg = (CONFIG.get("resource_types") or {}).get(resource_type)
    if not cfg:
        record_exception(
            "unrecognised-resource-type",
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "message": (
                    "No alarm set defined and not on the ignore list. This "
                    "resource is running with no platform monitoring. Either add "
                    "it to resource_types in alarm-sets.yaml, or add it to "
                    "ignored_resource_types to record that the omission is "
                    "deliberate."
                ),
            },
        )
        return {"status": "exception", "reason": "unrecognised-resource-type"}

    dimension_value = resource_id or resource_id_from_arn(arn, resource_type)
    if not dimension_value:
        record_exception(
            "no-resource-id",
            {"resource_type": resource_type, "arn": arn,
             "message": "Resource type is known but no id could be determined."},
        )
        return {"status": "exception", "reason": "no-resource-id"}

    applicable = [
        s for s in cfg["alarms"]
        if MONITORING_TIER in s.get("tiers", [])
    ]

    if not applicable:
        record_exception(
            "no-alarms-for-tier",
            {
                "resource_type": resource_type,
                "resource_id": dimension_value,
                "monitoring_tier": MONITORING_TIER,
                "message": (
                    "The type is recognised but no alarm in its set applies at "
                    "this monitoring tier, so the resource is uninstrumented. "
                    "That may be correct; it is recorded because a resource with "
                    "zero alarms is indistinguishable from a resource the "
                    "instrumentation never saw."
                ),
            },
        )
        return {"status": "exception", "reason": "no-alarms-for-tier"}

    created, failed = [], []
    for spec in applicable:
        try:
            cloudwatch.put_metric_alarm(**build_alarm(spec, cfg, resource_type, dimension_value))
            created.append(spec["name_suffix"])
        except Exception as exc:
            # Carry on to the rest of the set. Aborting here would leave a
            # resource partially instrumented AND stop the alarms that would
            # have worked, for a failure that is usually specific to one metric.
            failed.append({"alarm": spec["name_suffix"], "error": str(exc)})

    if failed:
        record_exception(
            "alarm-creation-failed",
            {
                "resource_type": resource_type,
                "resource_id": dimension_value,
                "created": created,
                "failed": failed,
            },
        )

    agent_metrics = set(CONFIG.get("agent_required_metrics") or [])
    needs_agent = any(s["metric"] in agent_metrics for s in applicable)

    result = {
        "status": "instrumented" if not failed else "partial",
        "resource_type": resource_type,
        "resource_id": dimension_value,
        "monitoring_tier": MONITORING_TIER,
        "alarms_created": created,
        "alarms_failed": [f["alarm"] for f in failed],
        # Surfaced rather than acted on. The SSM association installs the agent
        # on new instances; this flag is what makes it visible when an alarm
        # depends on a metric the agent publishes, so an INSUFFICIENT_DATA alarm
        # can be traced to a missing agent rather than a missing threshold.
        "depends_on_cloudwatch_agent": needs_agent,
    }
    print(json.dumps(result))
    return result
