"""Declaration-to-containment. Prompt 7.1.

design/07-response-automation.md, and design principle P4 again:

    Any recovery, restore or containment procedure is a single artifact with a
    mode parameter. Test mode and production mode differ in target and
    teardown, never in sequence or logic.

    Applies to: backup restore, security containment, failover, account vesting.

Design 07 says the same thing about this specific procedure, and says why it
matters more here than anywhere else:

    Containment has the worst testing problem in the design. The restore path is
    exercised monthly by design. Containment automation may sit unused for a
    year and then run once, badly, on the night it matters.

So this is the same shape as recovery/src/worker.py deliberately: ONE code
path, a mode parameter, and exactly two places the mode is read — select_target()
here, and the teardown Choice in the state machine. Prompt 7.4's scheduled
exercise fires THIS module against a planted target, not a copy of it.

The sequence, and why it is not negotiable
------------------------------------------
    1. Preserve   snapshot volumes, capture configuration, copy to Forensics
    2. Isolate    apply the pre-staged isolation group; instance STAYS RUNNING
    3. Revoke     attach deny-all to the principal; do NOT delete it
    4. Blast      revoke sessions by token issue time; look for the same
                  indicators in other accounts

Preserve runs first and completes before anything else starts, because every
later step reduces what can still be learned. Isolation cuts the network a
memory capture would have travelled over; revocation changes the principal whose
state you wanted to record.

Gather ALWAYS runs. design 07: "Stage 1 — Gather (always runs, always safe)."
A medium-confidence finding in production does not get contained, but it is
still preserved, because the evidence is perishable and the decision to contain
may be made by a human twenty minutes later.

What this never does
--------------------
Terminate. Delete. Deregister. Roll back. Purge. Not as a matter of code
discipline — the role it assumes (containment/00-responder-role.yaml) holds no
such permission, so a bug here cannot express the request.
"""
import datetime
import hashlib
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
events = boto3.client("events")
sts = boto3.client("sts")

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE", "")
RESPONDER_ROLE_NAME = os.environ.get("RESPONDER_ROLE_NAME",
                                     "PlatformSecurityResponder")
EXTERNAL_ID = os.environ.get("RESPONDER_EXTERNAL_ID", "")
ISOLATION_SG_NAME = os.environ.get("ISOLATION_SG_NAME", "platform-isolation")
ACCOUNT_TIER_PARAM = os.environ.get("ACCOUNT_TIER_PARAM",
                                    "/platform/routing/account-tiers")

# The bus the router listens on. Named explicitly rather than defaulted,
# because this runs IN the Audit account: a member-account component puts to
# its DEFAULT bus and baseline/70-event-forwarding.yaml carries it across, but
# there is nothing to carry here. Putting to the default bus would succeed, be
# charged for, and be read by nobody.
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")

# The exercise target. design 07: "a purpose-built target in an isolated
# account". Empty until prompt 7.4 builds it; select_target() refuses rather
# than falling back to a real account, because the failure mode of getting that
# wrong is containing production during a drill.
EXERCISE_ACCOUNT_ID = os.environ.get("EXERCISE_ACCOUNT_ID", "")

# design 07, "Confidence thresholds by environment":
#
#   | Environment | High-confidence | Medium-confidence |
#   | Production  | Auto-contain    | Gather and page   |
#   | Test / Dev  | Auto-contain    | Auto-contain      |
#
# "Isolating a healthy production service on a false positive is its own
# incident" — which is why production is the tier that gets the benefit of the
# doubt, and non-production is the tier where containment is close to free.
#
# UAT IS NOT IN THE TABLE, and it has to be somewhere. It is treated as
# production here. design/14-environment-tiers.md splits UAT along two axes:
# production-equivalent DATA handling, non-production RESILIENCE. Containment is
# a data-protection control, not a resilience one, and a UAT account is
# explicitly expected to hold regulated data — so a false-positive isolation
# there costs a customer's test team their afternoon, which is the cost
# production's row exists to avoid. Recorded as a deviation.
CONTAINMENT_DECISION = {
    ("prod", "high"): "contain",
    ("prod", "medium"): "gather_and_page",
    ("uat", "high"): "contain",
    ("uat", "medium"): "gather_and_page",
    ("test", "high"): "contain",
    ("test", "medium"): "contain",
    ("dev", "high"): "contain",
    ("dev", "medium"): "contain",
}


class ContainmentError(Exception):
    """Raised for conditions the incident record must carry rather than retry."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Targets — the first of P4's two permitted differences
# ---------------------------------------------------------------------------


def select_target(mode, incident):
    """The ONE place the mode is read.

    In `incident` mode the target is whatever the finding named. In `exercise`
    mode it is the purpose-built target in the isolated exercise account, and
    the finding's own account is ignored — a drill must never be able to reach
    a real account because a planted finding carried the wrong id.
    """
    if mode == "incident":
        account_id = incident.get("account_id")
        if not account_id:
            raise ContainmentError(
                "the finding names no account, so there is nothing to contain "
                "and nowhere to assume into")
        return {"mode": mode, "account_id": account_id,
                "resource_id": incident.get("resource_id"),
                "principal_arn": incident.get("principal_arn"),
                "is_exercise": False}

    if mode == "exercise":
        if not EXERCISE_ACCOUNT_ID:
            raise ContainmentError(
                "exercise mode requires EXERCISE_ACCOUNT_ID, and it is unset. "
                "Refusing to fall back to the account named in the finding: a "
                "drill that reaches a real account is worse than a drill that "
                "does not run. Prompt 7.4 builds the exercise target.")
        if incident.get("account_id") and \
                incident["account_id"] != EXERCISE_ACCOUNT_ID:
            # Loud, not silent. A planted finding carrying a real account id
            # means the exercise harness is misconfigured, and the next thing
            # it would have done is contain something real.
            raise ContainmentError(
                f"exercise mode was given a finding for account "
                f"{incident['account_id']}, which is not the exercise account "
                f"{EXERCISE_ACCOUNT_ID}. Refusing.")
        return {"mode": mode, "account_id": EXERCISE_ACCOUNT_ID,
                "resource_id": incident.get("resource_id"),
                "principal_arn": incident.get("principal_arn"),
                "is_exercise": True}

    raise ContainmentError(
        f"mode '{mode}' is neither 'incident' nor 'exercise'. The automation "
        "has exactly two targets and refuses to guess which was meant.")


def responder(account_id, purpose):
    """Assume the member-side responder role.

    A fresh client per stage rather than one for the whole execution: each
    Lambda invocation is a separate state, credentials last an hour, and a
    sequence that can run for longer than that during a real incident must not
    fail on stage 4 because the session it took at stage 1 expired.
    """
    role_arn = f"arn:aws:iam::{account_id}:role/{RESPONDER_ROLE_NAME}"
    kwargs = {"RoleArn": role_arn, "RoleSessionName": f"contain-{purpose}"[:64]}
    if EXTERNAL_ID:
        kwargs["ExternalId"] = EXTERNAL_ID
    try:
        creds = sts.assume_role(**kwargs)["Credentials"]
    except Exception as exc:                       # noqa: BLE001
        # This is the failure design 07 calls load-bearing: if the member
        # account could block the containment role, this is where it shows.
        # It must never be swallowed into "nothing to do".
        raise ContainmentError(
            f"could not assume {role_arn}: {type(exc).__name__}: {exc}. "
            "Either the baseline has not reached this account, or something "
            "in the account is blocking the containment role — which is the "
            "assumption open-items.md V3 exists to test.")
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


# ---------------------------------------------------------------------------
# The incident record — append only, so discovery_time cannot be rewritten
# ---------------------------------------------------------------------------
#
# design 07: "discovery_time is legally significant. It is set by the system at
# detection and is never editable."
#
# "Never editable" is not a promise code can make about itself. It is made here
# by SHAPE: the header item carries discovery_time and is written with a
# condition that fails if it already exists, and everything that happens
# afterwards is a separate append-only event item. Nothing in this module has
# an update path to the header, and the IAM policy behind it grants no
# UpdateItem or DeleteItem on the table at all.
#
# So rewriting discovery_time is not a matter of nobody choosing to — there is
# no API call available that would do it.


def record_event(incident_id, stage, outcome, detail):
    """Append one step of what happened. Never overwrites anything."""
    if not INCIDENT_TABLE:
        print(json.dumps({"UNRECORDED_CONTAINMENT_EVENT": {
            "incident_id": incident_id, "stage": stage, "outcome": outcome}}))
        return None
    now = _now()
    # Sequence inside the same millisecond: two stages can complete fast enough
    # to collide on timestamp alone, and a collision would silently overwrite
    # one of them -- which in an append-only evidence table is the one bug that
    # must not be possible.
    seq = hashlib.sha256(
        f"{stage}{outcome}{now.timestamp()}".encode()).hexdigest()[:8]
    item = {
        "PK": {"S": f"incident#{incident_id}"},
        "SK": {"S": f"event#{_iso(now)}#{seq}"},
        "record_type": {"S": "containment-event"},
        "incident_id": {"S": incident_id},
        "stage": {"S": stage},
        "outcome": {"S": outcome},
        "recorded_at": {"S": _iso(now)},
        "detail": {"S": json.dumps(detail, default=str)[:38000]},
    }
    dynamodb.put_item(TableName=INCIDENT_TABLE, Item=item)
    return item["SK"]["S"]


# ---------------------------------------------------------------------------
# Stage 0 — Triage
# ---------------------------------------------------------------------------


def triage(event):
    """Decide whether this finding is contained or gathered and paged.

    Runs before anything touches the account, and its output is recorded, so
    "why was production not isolated" has an answer that predates the argument.
    """
    incident = event["incident"]
    mode = event.get("mode") or "incident"
    target = select_target(mode, incident)

    tier = (incident.get("environment_tier") or "").lower()
    confidence = (incident.get("confidence") or "").lower()

    if tier not in ("dev", "test", "uat", "prod"):
        # Unknown tier is treated as production: the benefit of the doubt goes
        # to NOT isolating something that might be serving customers. An
        # unknown tier is also a finding in its own right — it means
        # scripts/sync-account-tiers.sh has not seen this account.
        note = (f"environment tier '{tier or 'unknown'}' is not one of "
                "dev/test/uat/prod, so production rules were applied. The "
                "account is missing from /platform/routing/account-tiers.")
        tier = "prod"
    else:
        note = None

    if confidence not in ("high", "medium"):
        # design 07 tabulates high and medium only. Anything else is treated as
        # medium — the more cautious of the two — rather than being refused,
        # because refusing would mean a finding nobody looks at.
        note = ((note + " ") if note else "") + (
            f"confidence '{confidence or 'unrated'}' is not high or medium; "
            "treated as medium.")
        confidence = "medium"

    decision = CONTAINMENT_DECISION.get((tier, confidence), "gather_and_page")

    result = {
        "decision": decision,
        "mode": mode,
        "target": target,
        "environment_tier": tier,
        "confidence": confidence,
        "note": note,
        "basis": (f"design 07 confidence thresholds: {tier} + {confidence} "
                  f"-> {decision}"),
    }
    record_event(incident["incident_id"], "triage", decision, result)
    return result


# ---------------------------------------------------------------------------
# Stage 1 — Preserve. Always runs. Nothing destructive happens before it ends.
# ---------------------------------------------------------------------------


def preserve(event):
    """Snapshot volumes, capture configuration, share evidence to Forensics.

    Runs for EVERY declared incident, including the ones that will not be
    contained. design 07: "Stage 1 — Gather (always runs, always safe)." The
    evidence is perishable and the decision to contain may be a human's, taken
    later; preserving first is what keeps that decision available.
    """
    incident = event["incident"]
    incident_id = incident["incident_id"]
    target = event["triage"]["target"]
    session = responder(target["account_id"], incident_id)
    ec2 = session.client("ec2")

    instance_id = _instance_id(target.get("resource_id"))
    preserved = {"snapshots": [], "instance": None, "shared_with": None,
                 "memory_capture": None}

    if not instance_id:
        preserved["note"] = (
            f"the finding names no EC2 instance (resource_id="
            f"{target.get('resource_id')!r}), so there are no volumes to "
            "snapshot. Principal-only findings are the common case for "
            "credential exposure, and they are contained at stages 3 and 4.")
        record_event(incident_id, "preserve", "nothing-to-snapshot", preserved)
        return preserved

    described = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = described.get("Reservations") or []
    if not reservations or not reservations[0].get("Instances"):
        preserved["note"] = f"instance {instance_id} does not exist in this account"
        record_event(incident_id, "preserve", "instance-not-found", preserved)
        return preserved

    instance = reservations[0]["Instances"][0]
    # The full running configuration, captured BEFORE isolation changes it.
    # After stage 2 the security group list is the isolation group, and the
    # original grouping — which is evidence about what the instance could reach
    # — is gone unless it was written down here.
    preserved["instance"] = {
        "instance_id": instance_id,
        "instance_type": instance.get("InstanceType"),
        "image_id": instance.get("ImageId"),
        "launch_time": _iso(instance["LaunchTime"]) if instance.get("LaunchTime") else None,
        "private_ip": instance.get("PrivateIpAddress"),
        "public_ip": instance.get("PublicIpAddress"),
        "subnet_id": instance.get("SubnetId"),
        "vpc_id": instance.get("VpcId"),
        "iam_instance_profile": (instance.get("IamInstanceProfile") or {}).get("Arn"),
        "state": (instance.get("State") or {}).get("Name"),
        "security_groups_before_isolation": [
            {"GroupId": g["GroupId"], "GroupName": g.get("GroupName")}
            for g in instance.get("SecurityGroups", [])],
        "network_interfaces": [
            {"NetworkInterfaceId": n["NetworkInterfaceId"],
             "security_groups": [g["GroupId"] for g in n.get("Groups", [])]}
            for n in instance.get("NetworkInterfaces", [])],
        "tags": {t["Key"]: t["Value"] for t in instance.get("Tags", [])},
    }

    # One crash-consistent set across every attached volume. Taking them one at
    # a time produces snapshots from different moments, which for anything with
    # a write-ahead log is a state the machine was never actually in.
    tags = [
        {"Key": "platform-incident-id", "Value": incident_id},
        {"Key": "platform-managed", "Value": "true"},
        {"Key": "platform-purpose", "Value": "incident-evidence"},
        {"Key": "platform-mode", "Value": target["mode"]},
        {"Key": "Name", "Value": f"evidence-{incident_id}-{instance_id}"},
    ]
    created = ec2.create_snapshots(
        InstanceSpecification={
            "InstanceId": instance_id,
            # The root volume is usually where the interesting things are.
            "ExcludeBootVolume": False,
        },
        Description=f"Incident {incident_id} evidence, preserved by platform containment",
        CopyTagsFromSource="volume",
        TagSpecifications=[{"ResourceType": "snapshot", "Tags": tags}],
    )
    preserved["snapshots"] = [
        {"snapshot_id": s["SnapshotId"], "volume_id": s.get("VolumeId"),
         "volume_size": s.get("VolumeSize")}
        for s in created.get("Snapshots", [])]

    # Move the evidence out of the account that may be compromised. Sharing
    # rather than copying, because a copy has to be initiated from the far side
    # and this role cannot act there — the Forensics account pulls, which is
    # also the right direction of trust.
    forensics, forensics_problem = _forensics_account(session)
    if forensics:
        # Per snapshot, and never fatal. A share that fails is recorded and
        # containment carries on to isolate and revoke (Jamie, 2026-10-13).
        # Raising here once skipped both: the 2026-10-13 exercise snapshotted
        # a volume encrypted with the AWS-managed aws/ebs key, which AWS will
        # not share across accounts ("Encrypted snapshots with EBS default key
        # cannot be shared"), and the instance was left unisolated and its
        # principal unrevoked. Unshared evidence is bad. An attacker who keeps
        # their access is worse.
        unshared = []
        for snap in preserved["snapshots"]:
            try:
                ec2.modify_snapshot_attribute(
                    SnapshotId=snap["snapshot_id"],
                    Attribute="createVolumePermission",
                    OperationType="add",
                    UserIds=[forensics])
                snap["shared"] = True
            except Exception as exc:                   # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                snap["shared"] = False
                unshared.append({"snapshot_id": snap["snapshot_id"],
                                 "error": f"{code or type(exc).__name__}: {exc}"})
        # shared_with names Forensics only if EVERY snapshot reached it. A
        # partial share reported as a share is the failure this replaces.
        preserved["shared_with"] = None if unshared else forensics
        if unshared:
            preserved["unshared"] = unshared
            preserved["note"] = (
                f"EVIDENCE WAS NOT MOVED OUT OF THE ACCOUNT for {len(unshared)} of "
                f"{len(preserved['snapshots'])} snapshot(s). They exist only in "
                "the account being contained, where a principal with local admin "
                "can delete them. Containment continued regardless.")
    else:
        # Reported as part of the incident, not passed over. Evidence sitting in
        # the account an attacker holds admin in is evidence they can delete.
        preserved["shared_with"] = None
        preserved["note"] = (
            "EVIDENCE WAS NOT MOVED OUT OF THE ACCOUNT. Snapshots exist only "
            "in the account being contained, where a principal with local "
            f"admin can delete them. Reason: {forensics_problem}")

    # design 07 asks for memory "where feasible". It is not feasible from here:
    # capturing memory needs an agent running inside the guest, and this role
    # deliberately cannot run commands on the instance. Stated rather than
    # silently skipped, so the readout does not imply memory was considered
    # and found unnecessary. B-053.
    preserved["memory_capture"] = {
        "attempted": False,
        "reason": "requires in-guest execution (SSM Run Command with an agent "
                  "and a capture tool). The responder role holds no ssm:Send"
                  "Command, deliberately. design 07 asks for memory 'where "
                  "feasible'; it is not feasible from the control plane.",
    }

    record_event(incident_id, "preserve",
                 "complete" if preserved.get("shared_with") else "not-moved",
                 preserved)
    return preserved


def _forensics_account(session):
    """Where evidence goes, and WHY if it cannot be determined.

    Returns (account_id, problem). The second value is the whole point.

    The first version swallowed every exception into None, so a missing
    permission and a genuinely unconfigured platform produced the same answer
    — "no Forensics account" — and the incident record said the platform was
    unconfigured when in fact it could not read a parameter that existed with
    the correct value in it. B-042 is the same bug in the instrumentation
    Lambda, fixed a day earlier, and it was written again here.

    A denial is a PLATFORM FAULT and has to say so, because the remedy is a
    policy change and the symptom is evidence quietly left where an attacker
    can delete it.
    """
    ssm = session.client("ssm")
    try:
        got = ssm.get_parameter(Name="/platform/security/forensics-account")
        return got["Parameter"]["Value"], None
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            return None, ("no Forensics account is configured for this account "
                          "(/platform/security/forensics-account is unset)")
        return None, (f"could not READ /platform/security/forensics-account: "
                      f"{code or type(exc).__name__}: {exc}. This is a platform "
                      "fault, not a missing configuration — evidence cannot be "
                      "moved out of the account until it is fixed.")


def _instance_id(resource_id):
    if not resource_id:
        return None
    text = str(resource_id)
    if text.startswith("i-"):
        return text
    if ":instance/" in text:
        return text.rsplit("/", 1)[-1]
    return None


# ---------------------------------------------------------------------------
# Stage 2 — Isolate. The instance stays running.
# ---------------------------------------------------------------------------


def isolate(event):
    """Swap every network interface onto the pre-staged isolation group.

    design 07: "Apply the isolation security group: deny all inbound and
    outbound except the Forensics account collection path. The instance stays
    running. Only its reachability changes."

    The group is created by baseline/40-network.yaml on day one and attached to
    nothing in normal operation, because "retrofitting this during an incident
    is not possible". This step does not create or edit a security group; it
    only changes which one is in use, and that is deliberately the limit of
    what the responder role can do to a security group.
    """
    incident = event["incident"]
    incident_id = incident["incident_id"]
    target = event["triage"]["target"]
    session = responder(target["account_id"], incident_id)
    ec2 = session.client("ec2")

    instance_id = _instance_id(target.get("resource_id"))
    if not instance_id:
        result = {"isolated": False,
                  "reason": "no instance named in the finding; nothing to isolate"}
        record_event(incident_id, "isolate", "not-applicable", result)
        return result

    described = ec2.describe_instances(InstanceIds=[instance_id])
    instance = described["Reservations"][0]["Instances"][0]
    vpc_id = instance.get("VpcId")

    groups = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [ISOLATION_SG_NAME]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ]).get("SecurityGroups", [])
    if not groups:
        # Stops rather than improvising. Creating a group here would mean
        # inventing an isolation posture mid-incident, and the whole reason the
        # group is pre-staged is that this is not the moment to design one.
        result = {"isolated": False,
                  "reason": (f"no security group named '{ISOLATION_SG_NAME}' in "
                             f"{vpc_id}. baseline/40-network.yaml has not "
                             "reached this VPC. Isolation NOT performed; the "
                             "instance is untouched and a human is needed.")}
        record_event(incident_id, "isolate", "blocked", result)
        raise ContainmentError(result["reason"])

    isolation_sg = groups[0]["GroupId"]

    # Every interface, not just the primary. An instance with a second ENI
    # still reachable is a half-isolated instance, which looks contained and is
    # not — the same failure mode design doc 02 names for partial turnup.
    changed = []
    for nic in instance.get("NetworkInterfaces", []):
        ec2.modify_network_interface_attribute(
            NetworkInterfaceId=nic["NetworkInterfaceId"],
            Groups=[isolation_sg])
        changed.append({"network_interface_id": nic["NetworkInterfaceId"],
                        "groups_before": [g["GroupId"] for g in nic.get("Groups", [])],
                        "groups_after": [isolation_sg]})

    after = ec2.describe_instances(InstanceIds=[instance_id])
    state = (after["Reservations"][0]["Instances"][0].get("State") or {}).get("Name")

    result = {
        "isolated": True,
        "isolation_security_group": isolation_sg,
        "interfaces_changed": changed,
        # Asserted, not assumed. "The instance stays running" is the property
        # that makes this preserve evidence rather than destroy it, so it is
        # checked and recorded rather than trusted.
        "instance_state_after": state,
        "instance_still_running": state == "running",
    }
    record_event(incident_id, "isolate", "complete", result)
    return result


# ---------------------------------------------------------------------------
# Stage 3 — Revoke. Deny-all, never delete.
# ---------------------------------------------------------------------------


DENY_ALL_POLICY_NAME = "PlatformIncidentDenyAll"
REVOKE_SESSIONS_POLICY_NAME = "PlatformIncidentRevokeOlderSessions"


def revoke(event):
    """Attach a deny-all inline policy to the compromised principal.

    design 07: "Attach a deny-all policy to the compromised principal — do not
    delete it. Deletion loses the audit trail and breaks attribution."

    Inline rather than managed, for one practical reason: an inline policy
    lives on the principal and travels with it, so there is no second object
    somebody can detach without touching the role. The responder role has
    PutRolePolicy and has no DeleteRolePolicy, so this automation can apply the
    deny and cannot lift it — standing an incident down is a human action taken
    with human credentials.
    """
    incident = event["incident"]
    incident_id = incident["incident_id"]
    target = event["triage"]["target"]
    principal_arn = target.get("principal_arn")

    if not principal_arn:
        result = {"revoked": False,
                  "reason": "the finding names no principal; nothing to revoke"}
        record_event(incident_id, "revoke", "not-applicable", result)
        return result

    kind, name = _principal(principal_arn)
    if kind is None:
        result = {"revoked": False,
                  "reason": f"{principal_arn} is neither an IAM role nor an IAM "
                            "user. Federated and service principals are not "
                            "revocable this way and need a human."}
        record_event(incident_id, "revoke", "unsupported-principal", result)
        return result

    session = responder(target["account_id"], incident_id)
    iam = session.client("iam")

    deny_all = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PlatformIncidentContainment",
            "Effect": "Deny",
            "Action": "*",
            "Resource": "*",
        }],
    }
    put = iam.put_role_policy if kind == "role" else iam.put_user_policy
    key = "RoleName" if kind == "role" else "UserName"
    put(**{key: name,
           "PolicyName": DENY_ALL_POLICY_NAME,
           "PolicyDocument": json.dumps(deny_all)})

    result = {
        "revoked": True,
        "principal_arn": principal_arn,
        "principal_type": kind,
        "policy_name": DENY_ALL_POLICY_NAME,
        "deleted": False,
        "why_not_deleted": "deletion loses the audit trail and breaks "
                           "attribution (design 07). The principal still "
                           "exists and can do nothing.",
    }
    record_event(incident_id, "revoke", "complete", result)
    return result


def _principal(arn):
    text = str(arn)
    if ":role/" in text:
        return "role", text.split(":role/", 1)[1].split("/")[-1]
    if ":user/" in text:
        return "user", text.split(":user/", 1)[1].split("/")[-1]
    if ":assumed-role/" in text:
        # An assumed-role ARN names the SESSION. The thing to contain is the
        # role behind it, and getting this wrong means attaching a policy to a
        # name that does not exist and reporting success.
        return "role", text.split(":assumed-role/", 1)[1].split("/")[0]
    return None, None


# ---------------------------------------------------------------------------
# Stage 4 — Blast radius. Sessions first; they are the step most often missed.
# ---------------------------------------------------------------------------


def blast_radius(event):
    """Revoke live sessions, then look for the same indicators elsewhere.

    design 07: "Revoke active sessions via a policy conditioned on token issue
    time. This is the step most often missed — disabling a principal does not
    invalidate sessions already issued."

    Prompt 7.1 is blunter: "Session revocation by token issue time is required,
    not optional."

    The deny-all from stage 3 does cover existing sessions for a role, because a
    role's policies are evaluated at request time. This is belt and braces AND
    the documented AWS mechanism, and it matters on its own for the case where a
    human later lifts the deny-all without realising that sessions issued before
    the incident are still valid until they expire.
    """
    incident = event["incident"]
    incident_id = incident["incident_id"]
    target = event["triage"]["target"]
    result = {"sessions_revoked": False, "cross_account": None}

    principal_arn = target.get("principal_arn")
    if principal_arn:
        kind, name = _principal(principal_arn)
        if kind == "role":
            session = responder(target["account_id"], incident_id)
            iam = session.client("iam")
            # Everything issued BEFORE now is denied; the role itself is
            # already denied everything by stage 3. The cut-off is recorded
            # because "which sessions were killed" is a question asked later.
            cutoff = _now()
            policy = {
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "PlatformIncidentRevokeOlderSessions",
                    "Effect": "Deny",
                    "Action": "*",
                    "Resource": "*",
                    "Condition": {
                        "DateLessThan": {"aws:TokenIssueTime": _iso(cutoff)},
                    },
                }],
            }
            iam.put_role_policy(RoleName=name,
                                PolicyName=REVOKE_SESSIONS_POLICY_NAME,
                                PolicyDocument=json.dumps(policy))
            result["sessions_revoked"] = True
            result["token_issue_time_cutoff"] = _iso(cutoff)
            result["policy_name"] = REVOKE_SESSIONS_POLICY_NAME
        else:
            result["sessions_note"] = (
                f"principal is a {kind or 'unknown type'}; token-issue-time "
                "revocation applies to roles. An IAM user's existing sessions "
                "are bounded by its access keys, which stage 3's deny-all "
                "covers.")

    # "Check for the same indicators in other accounts. The difference between
    # one compromised workload and a platform-level event." Prompt 7.2 says not
    # to skip it for speed. It is BUILT there, not here: it needs to read
    # Security Hub across the organization from the Audit account, which is
    # where the readout runs. Stage 4 records that the question is outstanding
    # so the incident cannot close without an answer.
    result["cross_account"] = {
        "checked": False,
        "reason": "the cross-account indicator sweep is the readout's, prompt "
                  "7.2. Recorded here so the incident carries the open "
                  "question rather than implying it was answered.",
    }
    record_event(incident_id, "blast-radius", "complete", result)
    return result


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def finalise(event):
    """What was done, what was deliberately not done, and how long it took."""
    incident = event["incident"]
    incident_id = incident["incident_id"]
    triage_result = event.get("triage") or {}
    started = _parse(event.get("started_at"))
    now = _now()

    # What HAPPENED, not what was decided. This read the triage decision alone
    # until 2026-10-13, so the exercise that day recorded "contained" for an
    # instance that was never isolated and a principal never revoked: preserve
    # had failed and the state machine jumped straight here. Contained means
    # isolate and revoke both ran to a result and nothing raised. Either may
    # legitimately be "not applicable" (a principal-only finding has no
    # instance), which still counts: the stage ran and decided.
    decided_contain = triage_result.get("decision") == "contain"
    error = event.get("error")
    contained = (decided_contain and not error
                 and event.get("isolate") is not None
                 and event.get("revoke") is not None)
    if decided_contain:
        outcome = "contained" if contained else "containment-incomplete"
    else:
        outcome = "gathered" if not error else "gather-incomplete"

    not_done = []
    if decided_contain and not contained:
        not_done.append(
            "CONTAINMENT DID NOT COMPLETE. "
            f"Isolation {'ran' if event.get('isolate') is not None else 'did NOT run'}; "
            f"revocation {'ran' if event.get('revoke') is not None else 'did NOT run'}. "
            "See error. A human must finish containment by hand.")
    if event.get("preserve_error"):
        not_done.append(
            "Evidence preservation FAILED and containment continued without it. "
            "See preserve_error.")
    if not decided_contain:
        not_done.append(
            "Isolation, revocation and session revocation were NOT performed. "
            f"design 07 routes a {triage_result.get('confidence')}-confidence "
            f"finding in {triage_result.get('environment_tier')} to gather and "
            "page, because isolating a healthy production service on a false "
            "positive is its own incident."
            + ("" if event.get("preserve_error") else " Evidence WAS preserved."))
    not_done.append(
        "Nothing was terminated, deleted, deregistered, rolled back or purged. "
        "The responder role holds no permission to do so.")
    if not (event.get("preserve") or {}).get("shared_with"):
        not_done.append(
            "Evidence was not moved to a Forensics account.")

    summary = {
        "record_type": "containment-summary",
        "incident_id": incident_id,
        "recorded_at": _iso(now),
        "mode": triage_result.get("mode"),
        "decision": triage_result.get("decision"),
        "environment_tier": triage_result.get("environment_tier"),
        "confidence": triage_result.get("confidence"),
        "discovery_time": incident.get("discovery_time"),
        "declared_time": incident.get("declared_time"),
        "elapsed_from_discovery_seconds": (
            (now - _parse(incident.get("discovery_time"))).total_seconds()
            if incident.get("discovery_time") else None),
        "elapsed_seconds": (now - started).total_seconds() if started else None,
        "preserved": event.get("preserve"),
        "isolated": event.get("isolate"),
        "revoked": event.get("revoke"),
        "blast_radius": event.get("blast_radius"),
        "outcome": outcome,
        "deliberately_not_done": not_done,
        "preserve_error": event.get("preserve_error"),
        "error": error,
    }
    record_event(incident_id, "summary", outcome, summary)
    emit(summary, incident)
    print(json.dumps(summary, default=str))
    return {"incident_id": incident_id, "decision": triage_result.get("decision"),
            "contained": contained, "outcome": outcome,
            "elapsed_seconds": summary["elapsed_seconds"]}


def _parse(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def emit(summary, incident):
    """Onto the platform bus, shaped as a dossier.

    Not a second routing path — alerting/src/router.py is the single routing
    layer, the same one the restore workflow and the response engine use. A
    security incident declares NO routing class of its own, so the router
    applies the tier default from design/06a: this is the one thing on the
    platform that should page wherever paging exists.
    """
    dossier = {
        "record_type": summary["record_type"],
        "recorded_at": summary["recorded_at"],
        "current_state": "ALARM",
        # Deliberately absent: `routing`. The router honours a declared class
        # only as a DOWNGRADE, and nothing about a security incident should be
        # downgraded by the thing reporting it.
        "alarm": {
            "name": f"platform-security-incident-{summary['decision']}",
            "resource_id": incident.get("resource_id") or incident.get("principal_arn")
                           or "unknown",
            "environment_tier": summary.get("environment_tier"),
            "account_id": incident.get("account_id"),
        },
        "assessment": {
            "probable_cause": incident.get("title") or "security incident declared",
        },
        "incident": summary,
    }
    events.put_events(Entries=[{
        "Source": "platform.security",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "EventBusName": ALERT_BUS_NAME,
    }])


STEPS = {"triage": triage, "preserve": preserve, "isolate": isolate,
         "revoke": revoke, "blast_radius": blast_radius, "finalise": finalise}


def handler(event, context):
    step = event.get("step")
    fn = STEPS.get(step)
    if fn is None:
        raise ContainmentError(
            f"unknown step '{step}'. The state machine and this function have "
            f"diverged. Known steps: {', '.join(sorted(STEPS))}")
    return fn(event)
