"""Turn a finished Fargate test run into CloudWatch Synthetics metrics.

Prompt 4.3: the Fargate runner must publish "on the same metric names, so the
downstream alarm path is identical either way". This is the piece that makes
that true.

Why this exists at all
----------------------
CloudWatch Synthetics has no Playwright runtime, so a tenant with an existing
Playwright suite cannot be monitored natively. The suite runs on Fargate
instead — unmodified, with nothing injected into their image — and this
function reads the outcome from the task's exit code.

The contract is the exit code and nothing else. A reporting shim mounted into
their container would make the platform a participant in their test run: a shim
that breaks takes their suite down, and a shim that needs their Node version
makes their image upgrade our problem. Exit code zero is the one contract every
test runner already honours.

Namespace
---------
Publishing into `CloudWatchSynthetics` is possible because it is not an `AWS/`
reserved namespace — CloudWatch only refuses PutMetricData for namespaces
beginning `AWS/`. That single fact is what lets one alarm serve both runners
instead of two alarms that must be kept in step by hand.
"""
import datetime
import json
import os

import boto3

cloudwatch = boto3.client("cloudwatch")

CANARY_NAME = os.environ.get("CANARY_NAME", "")
NAMESPACE = "CloudWatchSynthetics"


def _parse(ts):
    """ECS timestamps are ISO 8601 with a Z suffix Python 3.11+ accepts."""
    if not ts:
        return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def handler(event, context):
    detail = (event or {}).get("detail") or {}

    containers = detail.get("containers") or []
    # `suite` is the essential container in the task definition. Reading it by
    # name rather than taking containers[0] because a task with a sidecar would
    # otherwise report the sidecar's exit code, and the failure mode of that is
    # a canary that passes while the suite fails.
    suite = next((c for c in containers if c.get("name") == "suite"), None)

    if suite is None:
        # No verdict is available. Publishing a 0 here would report the service
        # as broken when what actually happened is that the platform could not
        # find the result, and publishing a 100 would report health nobody
        # measured. Neither is honest, so this raises: the function's Errors
        # alarm fires, and CanaryFailedAlarm breaches on missing data anyway,
        # which is the correct outcome for "the check did not produce a result".
        raise RuntimeError(json.dumps({
            "reason": "no container named 'suite' in the task state change",
            "task": detail.get("taskArn"),
            "containers": [c.get("name") for c in containers],
            "note": "not publishing a metric; a guessed verdict is worse than "
                    "a missing one, and CanaryFailedAlarm treats missing data "
                    "as breaching.",
        }))

    exit_code = suite.get("exitCode")
    stop_code = detail.get("stopCode")

    if exit_code is None:
        # The container never reported an exit code — killed by the agent,
        # out of memory, image pull failure, or capacity. That is a failed
        # check: the suite did not run, so the service is unverified, and an
        # unverified service is reported as failing rather than silently
        # skipped.
        success = 0.0
        reason = f"no exit code (stopCode={stop_code}, " \
                 f"reason={suite.get('reason') or detail.get('stoppedReason')})"
    else:
        success = 100.0 if exit_code == 0 else 0.0
        reason = f"exit code {exit_code}"

    started, stopped = _parse(detail.get("startedAt")), _parse(detail.get("stoppedAt"))
    # startedAt, not createdAt: the gap between them is Fargate provisioning
    # the task, which is platform latency and not the tenant's response time.
    # Including it would make the SLA alarm fire on our own scheduling.
    duration_ms = int((stopped - started).total_seconds() * 1000) \
        if started and stopped else None

    metrics = [{
        "MetricName": "SuccessPercent",
        "Dimensions": [{"Name": "CanaryName", "Value": CANARY_NAME}],
        "Value": success,
        "Unit": "Percent",
    }]
    if duration_ms is not None:
        metrics.append({
            "MetricName": "Duration",
            "Dimensions": [{"Name": "CanaryName", "Value": CANARY_NAME}],
            "Value": duration_ms,
            "Unit": "Milliseconds",
        })
    if stopped:
        # Timestamped to when the run finished rather than to now. A results
        # event delivered late would otherwise land in the wrong period and
        # could clear an alarm with a stale verdict.
        for m in metrics:
            m["Timestamp"] = stopped

    cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=metrics)

    out = {"canary": CANARY_NAME, "success_percent": success,
           "duration_ms": duration_ms, "reason": reason,
           "task": (detail.get("taskArn") or "").rsplit("/", 1)[-1]}
    print(json.dumps(out))
    return out
