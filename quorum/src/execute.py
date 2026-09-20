"""Performs a quorum-approved action. Prompt 8.3.

This is the only code in the platform that destroys anything, and every design
decision in it is about making sure it can only destroy the one thing two
people agreed to.

It never chooses a target
-------------------------
The action and the resource come from the verification record, which got them
by agreement between what the CALLER intended and what the change record
DECLARED. This module re-checks that agreement and then does what it is told.
There is no path here that reads a ticket and decides what to delete.

Three narrowings, and only the first is code
--------------------------------------------
1. It re-verifies immediately before acting. The verification that gated the
   state machine may be minutes old, and in those minutes the record could have
   been edited or reopened. Time-of-check to time-of-use is the classic hole in
   an approval gate, and the window here is cheap to close.

2. It assumes the executor role with a SESSION POLICY naming the single
   approved resource. Effective permission is the intersection of the role's
   policy and the session policy, so the credentials this module holds cannot
   express a request against any other resource. If the code below had a bug
   that passed the wrong key ARN, the call returns AccessDenied instead of
   deleting the wrong key. That is the containment role's argument -- make the
   wrong action inexpressible rather than merely un-attempted -- rebuilt at
   session scope, because a role that can delete keys cannot be scoped to a key
   that will not exist for months.

3. The session NAME carries the Jira key, so CloudTrail records which change
   record authorised each call. AWS produces that evidence, not this module,
   which is what makes it worth anything. reverse.py reads it back.

Evidence is written BEFORE the action, not after
-------------------------------------------------
An action that succeeds and then fails to record itself is indistinguishable,
afterwards, from an action nobody authorised -- and it is the reverse
reconciliation that would catch it and raise it as a finding against us. So the
attempt is recorded first, with everything needed to reconstruct the decision,
and the outcome is appended second. A crash between them leaves an attempt with
no outcome, which reads as "we tried this and do not know what happened" -- an
alarming record, correctly, and a true one.
"""
import datetime
import json
import os

import boto3

import verify

dynamodb = boto3.client("dynamodb")
sts = boto3.client("sts")

EVIDENCE_TABLE = os.environ.get("QUORUM_EVIDENCE_TABLE", "")
EXECUTOR_ROLE_NAME = os.environ.get("QUORUM_EXECUTOR_ROLE",
                                    "PlatformQuorumExecutor")

# KMS's deletion window. AWS allows 7 to 30 days; this asks for the longest.
#
# The window is the only part of a key deletion that is reversible, so there is
# no reason to shorten it. Somebody wanting the key gone sooner is asking to
# reduce the time available to notice a mistake, which is the opposite of what
# the quorum is for.
KEY_DELETION_WINDOW_DAYS = int(
    os.environ.get("QUORUM_KEY_DELETION_WINDOW_DAYS", "30"))


class ExecutionRefused(Exception):
    """The action was not performed, and the reason is not an AWS error."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# What each action needs, as data
# ---------------------------------------------------------------------------
#
# `session_actions` is what goes in the session policy. It is wider than the
# one approved action only where the API requires a read to perform the write
# -- describing a key before scheduling its deletion, for instance -- and never
# wider than that.
#
# `resource_scoped` records whether the API supports resource-level
# permissions. Where it does not, the session policy CANNOT narrow the call and
# says so out loud rather than silently granting more than it appears to.

ACTION_SPEC = {
    "kms:ScheduleKeyDeletion": {
        "session_actions": ["kms:ScheduleKeyDeletion", "kms:DescribeKey"],
        "resource_scoped": True,
        "service": "kms",
    },
    "backup:DeleteBackupVault": {
        "session_actions": ["backup:DeleteBackupVault",
                            "backup:DescribeBackupVault"],
        "resource_scoped": True,
        "service": "backup",
    },
    "backup:DeleteBackupVaultLockConfiguration": {
        "session_actions": ["backup:DeleteBackupVaultLockConfiguration",
                            "backup:DescribeBackupVault"],
        "resource_scoped": True,
        "service": "backup",
    },
    "cloudtrail:StopLogging": {
        "session_actions": ["cloudtrail:StopLogging",
                            "cloudtrail:DescribeTrails"],
        "resource_scoped": True,
        "service": "cloudtrail",
    },
    "cloudtrail:DeleteTrail": {
        "session_actions": ["cloudtrail:DeleteTrail",
                            "cloudtrail:DescribeTrails"],
        "resource_scoped": True,
        "service": "cloudtrail",
    },
    "config:StopConfigurationRecorder": {
        # Config recorders are not ARN-addressable and the API takes a NAME.
        # A session policy cannot narrow this one, so the only thing standing
        # between the verification and the wrong recorder is the recorder name
        # passed below -- which comes from the approved record, not from a
        # lookup. Said plainly because a reader should not have to infer it
        # from the absence of a Resource clause.
        "session_actions": ["config:StopConfigurationRecorder",
                            "config:DescribeConfigurationRecorders"],
        "resource_scoped": False,
        "service": "config",
    },
    "organizations:RemoveAccountFromOrganization": {
        # Same: Organizations does not support resource-level permissions for
        # this call. The account id is taken from the approved record.
        "session_actions": ["organizations:RemoveAccountFromOrganization",
                            "organizations:DescribeAccount"],
        "resource_scoped": False,
        "service": "organizations",
    },
}


def _session_policy(action, resource):
    """The narrowest policy that still permits the approved call."""
    spec = ACTION_SPEC[action]
    statement = {
        "Effect": "Allow",
        "Action": spec["session_actions"],
        "Resource": [resource] if spec["resource_scoped"] else "*",
    }
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]})


def _assume(account_id, region, issue, action, resource):
    """Credentials that can do the approved thing and nothing else."""
    spec = ACTION_SPEC[action]
    # The session name is the audit trail. It must match the trust policy's
    # `quorum-*` condition, and it lands in CloudTrail on every call made with
    # these credentials.
    session_name = f"quorum-{issue}"[:64]
    try:
        creds = sts.assume_role(
            RoleArn=f"arn:aws:iam::{account_id}:role/{EXECUTOR_ROLE_NAME}",
            RoleSessionName=session_name,
            Policy=_session_policy(action, resource),
            DurationSeconds=900,
        )["Credentials"]
    except Exception as exc:                       # noqa: BLE001
        raise ExecutionRefused(
            f"could not assume {EXECUTOR_ROLE_NAME} in {account_id}: {exc}. "
            "Nothing was attempted.") from exc
    return boto3.client(
        spec["service"], region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    ), session_name


def _record(issue, record_type, item):
    """Append one evidence row. Never updates, never deletes.

    Immutability is enforced by the absence of UpdateItem and DeleteItem from
    this function's IAM policy, not by anything written here. A bug in this
    code cannot rewrite history because the credentials it runs under cannot
    express the request.

    PK groups every row for one change record; SK orders them by when they
    happened. Reconstructing a single decision -- the attempt, the verification
    it rested on, what came of it -- is one query.
    """
    at = _now().isoformat()
    row = {
        "PK": f"quorum#{issue}",
        "SK": f"{at}#{record_type}",
        "record_type": record_type,
        **item,
    }
    if not EVIDENCE_TABLE:
        print(json.dumps({"evidence_not_written": row}, default=str))
        return row
    dynamodb.put_item(
        TableName=EVIDENCE_TABLE,
        Item={k: {"S": json.dumps(v, default=str)
                  if isinstance(v, (dict, list)) else str(v)}
              for k, v in row.items() if v is not None},
        # Append-only at the API level too. Refusing to overwrite an existing
        # row is belt to the IAM policy's braces: a collision is a bug worth
        # hearing about rather than a silent overwrite of evidence.
        ConditionExpression="attribute_not_exists(PK)",
    )
    return row


def _record_lock(issue, action, resource, at):
    """Claim this approval. Raises ConditionalCheckFailedException if taken.

    A deterministic SK, unlike every other row in this table -- the whole point
    is that a second write collides. `attribute_not_exists(PK)` evaluates
    against the item this write would replace, so it succeeds only when no such
    item exists.
    """
    if not EVIDENCE_TABLE:
        return
    dynamodb.put_item(
        TableName=EVIDENCE_TABLE,
        Item={
            "PK": {"S": f"quorum#{issue}"},
            "SK": {"S": f"LOCK#{action}#{resource}"},
            "record_type": {"S": "quorum-execution-lock"},
            "claimed_at": {"S": at.isoformat()},
        },
        ConditionExpression="attribute_not_exists(PK)",
    )


def execute(event, context=None):
    """Perform the approved action.

    event: {"issue": ..., "action": ..., "resource": ...,
            "account_id": ..., "region": ...,
            "verification": {...}}          # from verify.verify

    The verification is passed in AND redone. Passing it in keeps the state
    machine's decision in the evidence; redoing it closes the window between
    the two.
    """
    issue = event.get("issue")
    action = event.get("action")
    resource = event.get("resource")
    account_id = event.get("account_id")
    region = event.get("region") or os.environ.get("AWS_REGION")
    started = _now()

    if action not in ACTION_SPEC:
        raise ExecutionRefused(
            f"'{action}' is not an executable quorum action. Known: "
            f"{', '.join(sorted(ACTION_SPEC))}")
    if not (issue and resource and account_id):
        raise ExecutionRefused(
            "execute needs issue, action, resource and account_id")

    # ---- Re-verify. The state machine already did; minutes have passed.
    fresh = verify.verify({"issue": issue, "action": action,
                           "resource": resource})

    attempt = {
        "issue": issue,
        "action": action,
        "resource": resource,
        "account_id": account_id,
        "region": region,
        "attempted_at": started.isoformat(),
        "verification_at_gate": event.get("verification"),
        "verification_at_execution": fresh,
    }
    _record(issue, "quorum-execution-attempt", attempt)

    if fresh.get("verdict") != verify.VERDICT_APPROVED:
        # Recorded as a refusal, which is itself evidence worth keeping: a
        # request that reached the execution step and was turned away at the
        # last moment is more interesting than one that never got there.
        outcome = {
            "issue": issue, "action": action, "resource": resource,
            "account_id": account_id,
            "refused_at": _now().isoformat(),
            "verdict": fresh.get("verdict"),
            "failures": fresh.get("failures"),
            "reason": fresh.get("reason"),
        }
        _record(issue, "quorum-execution-refused", outcome)
        print(json.dumps(outcome, default=str))
        raise ExecutionRefused(
            f"{issue} did not verify at execution time: "
            f"{fresh.get('verdict')}. "
            f"{fresh.get('reason') or fresh.get('failures')}")

    # ---- One approval, one execution.
    #
    # A deterministic row per (issue, action, resource) written with a
    # conditional put. A second attempt to act on the same approval fails the
    # condition and is refused.
    #
    # This is what a reader expects "two people approved this" to mean: they
    # approved the thing happening, once. Without it, an approved record can be
    # replayed -- by a retry, by somebody re-running the state machine, or by
    # anybody who can start an execution -- and every replay verifies perfectly
    # because the approval is genuinely still there.
    #
    # DynamoDB's conditional write is the whole mechanism. It is atomic, so two
    # simultaneous executions cannot both win it, which also covers the
    # parallel case that reserved concurrency would have handled.
    try:
        _record_lock(issue, action, resource, started)
    except dynamodb.exceptions.ConditionalCheckFailedException:
        outcome = {
            "issue": issue, "action": action, "resource": resource,
            "account_id": account_id,
            "refused_at": _now().isoformat(),
            "verdict": "already-executed",
        }
        _record(issue, "quorum-execution-refused", outcome)
        raise ExecutionRefused(
            f"{issue} has already been executed against {resource}. "
            "An approval authorises an action once. If it genuinely needs to "
            "happen again, that is a new change record and a new approval.")

    client, session_name = _assume(account_id, region, issue, action, resource)

    try:
        result = _perform(client, action, resource)
        status = "performed"
        error = None
    except Exception as exc:                       # noqa: BLE001
        # An AWS-side failure is recorded and re-raised. It is NOT swallowed
        # into a "refused" outcome: refused means the platform declined, failed
        # means AWS did, and an operator needs to know which.
        result = None
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    outcome = {
        "issue": issue, "action": action, "resource": resource,
        "account_id": account_id, "region": region,
        "session_name": session_name,
        "completed_at": _now().isoformat(),
        "result": result,
        "error": error,
        # The approvers, by accountId, copied into the outcome so the evidence
        # row stands alone. An auditor reading this should not have to join it
        # against Jira -- which by then may have been edited, and which design
        # 08 warns is exactly what can happen.
        "approver_account_ids": fresh.get("approver_account_ids"),
        "requester_account_id": fresh.get("requester_account_id"),
    }
    _record(issue, f"quorum-execution-{status}", outcome)
    print(json.dumps(outcome, default=str))

    if error:
        raise RuntimeError(f"{action} on {resource} failed: {error}")
    return outcome


def _perform(client, action, resource):
    """The actual call. One branch per action, explicit rather than dispatched.

    A dispatch table mapping action -> lambda would be shorter and would make
    this the hardest function in the codebase to audit. Somebody checking that
    `kms:ScheduleKeyDeletion` really does schedule a deletion of the approved
    key, with the window we claim, should be able to read it in one place.
    """
    if action == "kms:ScheduleKeyDeletion":
        return client.schedule_key_deletion(
            KeyId=resource, PendingWindowInDays=KEY_DELETION_WINDOW_DAYS)

    if action == "backup:DeleteBackupVault":
        return client.delete_backup_vault(BackupVaultName=_tail(resource))

    if action == "backup:DeleteBackupVaultLockConfiguration":
        return client.delete_backup_vault_lock_configuration(
            BackupVaultName=_tail(resource))

    if action == "cloudtrail:StopLogging":
        return client.stop_logging(Name=resource)

    if action == "cloudtrail:DeleteTrail":
        return client.delete_trail(Name=resource)

    if action == "config:StopConfigurationRecorder":
        return client.stop_configuration_recorder(
            ConfigurationRecorderName=_tail(resource))

    if action == "organizations:RemoveAccountFromOrganization":
        return client.remove_account_from_organization(AccountId=_tail(resource))

    # Unreachable: ACTION_SPEC is checked in execute(). Here so that adding a
    # spec entry without a branch fails loudly rather than silently doing
    # nothing and reporting success.
    raise ExecutionRefused(
        f"'{action}' has a spec but no implementation in _perform")


def _tail(arn_or_name):
    """The name at the end of an ARN, or the value if it is already a name.

    Backup vaults, Config recorders and account ids are addressed by name in
    their APIs while the change record declares a full ARN, because an ARN is
    unambiguous and a bare name is not -- two accounts can both have a vault
    called `default`.
    """
    text = str(arn_or_name)
    if not text.startswith("arn:"):
        return text
    tail = text.split(":")[-1]
    return tail.split("/")[-1] if "/" in tail else tail


HANDLERS = {"execute": execute}


def handler(event, context):
    fn = HANDLERS.get(event.get("action_type") or "execute")
    if fn is None:
        raise ValueError(f"unknown action_type: {event.get('action_type')}")
    return fn(event, context)
