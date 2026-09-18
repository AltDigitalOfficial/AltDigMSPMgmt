"""Safety rails for automated response.

Prompt 5.1: "Safety rails are not optional and must live in the framework, not
in individual runbooks."

That placement is the whole design. A rail implemented inside a runbook is a
rail the next runbook forgets, and the failure is silent — the new runbook
works, it just works without a brake. Everything here is enforced before any
action runs, for every action, without the catalog being able to opt out.

------------------------------------------------------------------------------
The six rails
------------------------------------------------------------------------------
Five from doc 07 and 06a, one added on 2026-09-18:

  kill switch       per-account and global, usable without a deployment
  rate limit        20 automated actions per account per hour
  circuit breaker   same remediation 3x in an hour -> stop and page
  failed attempts   same action failed twice on the same resource -> stop
  redundancy        stage 3 verifies redundancy BEFORE acting
  one at a time     stage 3 acts on one resource, revalidating between

------------------------------------------------------------------------------
The circuit breaker counts successes too, and that is the point
------------------------------------------------------------------------------
Doc 07: "Repeated automated repair is masking a real fault."

So three log clears that all WORKED still trips it. Something is writing logs
pathologically and the automation is hiding it — which is worse than the
original symptom, because the symptom was at least visible.

The failed-attempt limit is separate and shorter, because the two say different
things. Three successes in an hour is a fault being masked. Two failures is
automation flailing at something it cannot fix, and every attempt is time
during which nobody is looking at a real incident.

------------------------------------------------------------------------------
History lives in the account it describes
------------------------------------------------------------------------------
A DynamoDB table per member account, with TTL. Not the central registry: the
registry is in Platform Tooling, and giving every member account write access to
the table describing every other tenant to record a log rotation would be a
poor trade. The history is also only ever read by the account that wrote it.
"""

import datetime
import json
import os

import boto3

HISTORY_TABLE = os.environ.get("HISTORY_TABLE", "platform-remediation-history")
KILL_SWITCH_PARAM = os.environ.get("KILL_SWITCH_PARAM", "/platform/response/enabled")
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "20"))
CIRCUIT_BREAKER_COUNT = int(os.environ.get("CIRCUIT_BREAKER_COUNT", "3"))
FAILED_ATTEMPT_LIMIT = int(os.environ.get("FAILED_ATTEMPT_LIMIT", "2"))
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "3600"))

dynamodb = boto3.client("dynamodb")
ssm = boto3.client("ssm")
ecs = boto3.client("ecs")
elbv2 = boto3.client("elbv2")
autoscaling = boto3.client("application-autoscaling")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class Blocked(Exception):
    """A rail refused the action. Carries why, for the dossier."""

    def __init__(self, rail, message):
        self.rail = rail
        self.message = message
        super().__init__(f"{rail}: {message}")


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def check_kill_switch():
    """06a: kill_switch { per_account: true, global: true, requires_deployment: false }

    An SSM parameter rather than a stack parameter or an environment variable,
    because "requires_deployment: false" rules both of those out — changing
    either means a deploy, and the moment you want a kill switch is the moment
    you least want to run a deployment.

    FAILS CLOSED. If the parameter cannot be read, automation does not run.
    A kill switch that defaults to "go" when it cannot be consulted is not a
    kill switch; it is a kill switch-shaped object that works only when nothing
    is wrong.
    """
    try:
        value = ssm.get_parameter(Name=KILL_SWITCH_PARAM)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        raise Blocked(
            "kill-switch",
            f"{KILL_SWITCH_PARAM} does not exist. Automated response is disabled "
            "until it is created with the value 'enabled'. Absence is treated as "
            "off deliberately: an account that never opted in should not be acted "
            "upon because a parameter was forgotten.",
        )
    except Exception as exc:
        raise Blocked(
            "kill-switch",
            f"could not read {KILL_SWITCH_PARAM} ({exc}). Failing closed.",
        )

    if value.strip().lower() != "enabled":
        raise Blocked(
            "kill-switch",
            f"automated response is disabled for this account ({KILL_SWITCH_PARAM} "
            f"= '{value}'). Gathering and paging still happen; nothing will be "
            "changed.",
        )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def record_attempt(resource_id, action, outcome, detail=None):
    """Write one attempt. Outcome is 'succeeded' | 'failed' | 'blocked'.

    Written BEFORE the outcome is known and updated after would be more
    accurate and much worse: a Lambda that dies mid-action would leave no
    record, and the rails would then under-count exactly the actions that went
    wrong. So this is called after the attempt with its real outcome, and the
    engine treats an unrecorded attempt as the failure case.
    """
    now = _now()
    dynamodb.put_item(
        TableName=HISTORY_TABLE,
        Item={
            "resource_action": {"S": f"{resource_id}#{action}"},
            "attempted_at": {"S": now.isoformat()},
            "resource_id": {"S": resource_id},
            "action": {"S": action},
            "outcome": {"S": outcome},
            "detail": {"S": json.dumps(detail or {})},
            # TTL well beyond the rail window. The window decides what the
            # rails consider; retention decides what a human can review after
            # an incident, and those are different questions.
            "expires_at": {"N": str(int((now + datetime.timedelta(days=30)).timestamp()))},
        },
    )


def recent_attempts(resource_id, action):
    """Attempts for this (resource, action) inside the window."""
    cutoff = (_now() - datetime.timedelta(seconds=WINDOW_SECONDS)).isoformat()
    resp = dynamodb.query(
        TableName=HISTORY_TABLE,
        KeyConditionExpression="resource_action = :ra AND attempted_at >= :cutoff",
        ExpressionAttributeValues={
            ":ra": {"S": f"{resource_id}#{action}"},
            ":cutoff": {"S": cutoff},
        },
    )
    return [
        {"outcome": i["outcome"]["S"], "attempted_at": i["attempted_at"]["S"]}
        for i in resp.get("Items", [])
    ]


def account_actions_this_hour():
    """Every action in the account inside the window, for the rate limit.

    A scan, deliberately. The table holds at most a few hundred short-lived
    items — the TTL guarantees that — and a GSI purely to count them would be
    more machinery than the thing it counts. If this table ever grows enough
    for a scan to matter, the rate limit has already failed to do its job.
    """
    cutoff = (_now() - datetime.timedelta(seconds=WINDOW_SECONDS)).isoformat()
    resp = dynamodb.scan(
        TableName=HISTORY_TABLE,
        FilterExpression="attempted_at >= :cutoff AND outcome <> :blocked",
        ExpressionAttributeValues={":cutoff": {"S": cutoff}, ":blocked": {"S": "blocked"}},
        Select="COUNT",
    )
    return resp.get("Count", 0)


# ---------------------------------------------------------------------------
# The rails proper
# ---------------------------------------------------------------------------


def check_rate_limit():
    used = account_actions_this_hour()
    if used >= RATE_LIMIT_PER_HOUR:
        raise Blocked(
            "rate-limit",
            f"{used} automated actions already taken in this account in the last "
            f"{WINDOW_SECONDS // 60} minutes, limit {RATE_LIMIT_PER_HOUR}. An account "
            "hitting this is not having twenty separate incidents; it is having one "
            "that the automation cannot see the shape of.",
        )


def check_circuit_breaker(resource_id, action):
    """Doc 07: "same remediation firing 3x in an hour".

    FIRING. A blocked attempt never fired — some other rail refused it before
    the action ran — so it must not count here.

    An earlier version counted every recorded attempt, and the result was a
    self-locking platform: each invocation against a DISABLED kill switch
    recorded a blocked attempt, so three of those tripped the breaker, and
    enabling automation afterwards found it already refusing to act. The
    account had spent its circuit-breaker budget on actions it never took.

    Found in the canary: nine attempts, all blocked, reported as "has run 8
    times ... (0 of them successfully)". That parenthetical is what gave it
    away, which is an argument for putting the counts in the message rather
    than just the verdict.
    """
    attempts = [a for a in recent_attempts(resource_id, action)
                if a["outcome"] in ("succeeded", "failed")]
    if len(attempts) >= CIRCUIT_BREAKER_COUNT:
        succeeded = sum(1 for a in attempts if a["outcome"] == "succeeded")
        raise Blocked(
            "circuit-breaker",
            f"'{action}' has run {len(attempts)} times on {resource_id} within the "
            f"window ({succeeded} of them successfully), limit {CIRCUIT_BREAKER_COUNT}. "
            "Repeated automated repair is masking a real fault — and if those "
            "attempts SUCCEEDED, that is the more serious reading, because the "
            "symptom has been hidden rather than fixed.",
        )


def check_failed_attempts(resource_id, action):
    """Shorter fuse than the breaker, and a different meaning.

    Three successes in an hour is a fault being masked. Two failures is
    automation flailing at something it cannot fix, and every attempt is time
    during which nobody is looking at a real incident.
    """
    failures = [a for a in recent_attempts(resource_id, action) if a["outcome"] == "failed"]
    if len(failures) >= FAILED_ATTEMPT_LIMIT:
        raise Blocked(
            "failed-attempts",
            f"'{action}' has already failed {len(failures)} times on {resource_id} "
            f"within the window, limit {FAILED_ATTEMPT_LIMIT}. It did not work the "
            "first time and it did not work the second; a third attempt buys nothing "
            "and costs the time a human could have spent diagnosing.",
        )


def check_redundancy(resource_type, resource_id):
    """Stage 3 only. Doc 07: "verifies redundancy before acting, not after."

    Returns quietly if redundant, raises if not or if it cannot tell.

    Not being able to tell is treated as not redundant. The whole point of the
    check is that stage 3 is permitted ONLY where the resource is genuinely
    redundant, so an unknown answer must not be read as a yes.
    """
    if resource_type == "AWS::ECS::Service":
        cluster, service = _split_ecs(resource_id)
        if not cluster:
            raise Blocked("redundancy", f"could not determine the cluster for {resource_id}")
        d = ecs.describe_services(cluster=cluster, services=[service])["services"]
        if not d:
            raise Blocked("redundancy", f"ECS service {resource_id} not found")
        svc = d[0]
        running, desired = svc.get("runningCount", 0), svc.get("desiredCount", 0)
        if desired < 2 or running < 2:
            raise Blocked(
                "redundancy",
                f"{service} has {running} running of {desired} desired. Cycling a task "
                "here removes the only one serving traffic, which is an outage rather "
                "than a remediation.",
            )
        return {"running": running, "desired": desired}

    if resource_type == "AWS::EC2::Instance":
        # Redundancy for an instance means something else is serving the same
        # traffic. The only evidence available without knowing the application
        # is target group membership with other healthy targets.
        groups = elbv2.describe_target_groups()["TargetGroups"]
        for tg in groups:
            arn = tg["TargetGroupArn"]
            health = elbv2.describe_target_health(TargetGroupArn=arn)["TargetHealthDescriptions"]
            ids = [h["Target"]["Id"] for h in health]
            if resource_id not in ids:
                continue
            healthy_others = [
                h for h in health
                if h["Target"]["Id"] != resource_id
                and h["TargetHealth"]["State"] == "healthy"
            ]
            if healthy_others:
                return {"target_group": arn, "healthy_peers": len(healthy_others)}
            raise Blocked(
                "redundancy",
                f"{resource_id} is in target group {arn.split('/')[-2]} with no other "
                "healthy target. Restarting its service takes the last one out of "
                "service.",
            )
        raise Blocked(
            "redundancy",
            f"{resource_id} is not behind any load balancer this account can see, so "
            "redundancy cannot be established. Stage 3 is permitted only where "
            "redundancy is verified, and an unknown answer is not a yes.",
        )

    raise Blocked(
        "redundancy",
        f"no redundancy check implemented for {resource_type}. Stage 3 requires one, "
        "so the action is refused rather than attempted blind.",
    )


def check_scale_in_policy(resource_type, resource_id):
    """Precondition for scale_out. Reversible is not the same as self-reversing.

    Without a scale-in policy the automation adds capacity and nothing ever
    removes it — reversible in principle, permanent in practice until somebody
    reads the bill.
    """
    if resource_type != "AWS::ECS::Service":
        raise Blocked("scale-in-policy",
                      f"no scale-in check implemented for {resource_type}")
    cluster, service = _split_ecs(resource_id)
    rid = f"service/{cluster}/{service}"
    policies = autoscaling.describe_scaling_policies(
        ServiceNamespace="ecs", ResourceId=rid)["ScalingPolicies"]
    scale_in = [
        p for p in policies
        if p.get("TargetTrackingScalingPolicyConfiguration", {}).get("ScaleInCooldown") is not None
        or any(adj.get("ScalingAdjustment", 0) < 0
               for adj in p.get("StepScalingPolicyConfiguration", {}).get("StepAdjustments", []))
    ]
    if not scale_in:
        raise Blocked(
            "scale-in-policy",
            f"{service} has no scaling policy that can reduce capacity. Scaling out "
            "would add tasks that nothing ever removes — reversible in principle, "
            "permanent in practice. Gathering and escalating instead.",
        )
    return {"policies": len(scale_in)}


def _split_ecs(resource_id):
    """ECS service ids arrive as 'cluster/service' or as a bare service name."""
    if "/" in resource_id:
        parts = resource_id.split("/")
        return parts[-2], parts[-1]
    return None, resource_id


def preflight(stage, resource_type, resource_id, action, spec):
    """Every rail, in order, before any action. Raises Blocked on refusal.

    Order matters. The kill switch is first because a disabled account should
    not have its history read or its rate limit consulted — those are questions
    about how to act, and the answer here is that we are not acting. The
    resource-specific checks are last because they cost API calls.
    """
    check_kill_switch()
    check_rate_limit()
    check_circuit_breaker(resource_id, action)
    check_failed_attempts(resource_id, action)

    evidence = {}
    if stage == 3:
        evidence["redundancy"] = check_redundancy(resource_type, resource_id)
    if spec.get("requires_scale_in_policy"):
        evidence["scale_in_policy"] = check_scale_in_policy(resource_type, resource_id)
    return evidence
