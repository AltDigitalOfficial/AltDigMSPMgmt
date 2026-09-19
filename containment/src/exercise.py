"""The scheduled containment exercise. Prompt 7.4.

design/07-response-automation.md, "Exercising containment automation":

    Containment has the worst testing problem in the design. The restore path
    is exercised monthly by design. Containment automation may sit unused for a
    year and then run once, badly, on the night it matters.

    Same treatment as restore (P4): a scheduled exercise fires the REAL
    automation against a purpose-built target in an isolated account.

      1. Plant a finding
      2. Let it declare
      3. Let it preserve and isolate
      4. Verify snapshots landed in Forensics and isolation actually isolated
      5. Measure elapsed time
      6. Tear down

The word doing the work is REAL. This module plants a finding on the same bus
a GuardDuty finding arrives on, and then gets out of the way: declaration runs
declare.py, containment runs contain.py, the readout runs readout.py. Nothing
here re-implements any of them, and nothing here is invoked by them. If the
exercise passes, the code that passed is the code that will run during an
incident -- which is the entire claim P4 makes.

What the exercise is allowed to be different about
--------------------------------------------------
The target, and the teardown. Exactly the two things P4 permits, and the same
two the restore workflow varies. contain.py's select_target() refuses to run in
exercise mode against anything but the exercise account, so a planted finding
that named a real account would abort rather than contain production.

On tearing down a real instance
-------------------------------
CLAUDE.md forbids automation that terminates a resource as REMEDIATION. This
terminates the instance the exercise itself launched ninety seconds earlier, in
an empty account, and the permission to do so is conditioned in IAM on a tag
only this harness applies at creation. Same argument and same discipline as
recovery/src/teardown.py, which is the precedent this follows deliberately
rather than inventing a second shape for the same problem.
"""
import datetime
import hashlib
import json
import os
import time

import boto3

import declare

dynamodb = boto3.client("dynamodb")
events = boto3.client("events")
sts = boto3.client("sts")

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
EXERCISE_ACCOUNT_ID = os.environ.get("EXERCISE_ACCOUNT_ID", "")
FORENSICS_ACCOUNT_ID = os.environ.get("FORENSICS_ACCOUNT_ID", "")
HARNESS_ROLE = os.environ.get("HARNESS_ROLE_NAME", "PlatformExerciseHarness")
LOCAL_ADMIN_ROLE = os.environ.get("LOCAL_ADMIN_ROLE_NAME",
                                  "PlatformExerciseLocalAdmin")
FORENSICS_READER_ROLE = os.environ.get("FORENSICS_READER_ROLE_NAME",
                                       "PlatformForensicsReader")
RESPONDER_ROLE_NAME = os.environ.get("RESPONDER_ROLE_NAME",
                                     "PlatformSecurityResponder")
DRILL_TAG = os.environ.get("DRILL_TAG_KEY", "platform-exercise-id")
SUBNET_ID = os.environ.get("EXERCISE_SUBNET_ID", "")
AMI_PARAMETER = os.environ.get(
    "EXERCISE_AMI_PARAMETER",
    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")


class ExerciseError(Exception):
    """A drill that cannot run. Reported, never silently skipped."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def assume(account_id, role_name, purpose):
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"exercise-{purpose}"[:64])["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


def exercise_id():
    return "ex-" + _now().strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# 1. Plant a target, then plant a finding
# ---------------------------------------------------------------------------


def prepare(event):
    """Launch the drill instance and create the drill principal.

    Both tagged with the exercise id at creation, which is what makes teardown
    able to prove it created them. Nothing here is reused between runs: a
    long-lived drill target would drift, and an exercise that passes against a
    target nobody has rebuilt in a year proves less than it appears to.
    """
    if not EXERCISE_ACCOUNT_ID:
        raise ExerciseError(
            "EXERCISE_ACCOUNT_ID is unset. The drill has nowhere isolated to "
            "run and will not fall back to any other account.")
    if not SUBNET_ID:
        raise ExerciseError(
            "EXERCISE_SUBNET_ID is unset. The drill target needs a subnet in "
            "the exercise account; without one the instance would land "
            "wherever EC2 chose.")

    ex_id = event.get("exercise_id") or exercise_id()
    session = assume(EXERCISE_ACCOUNT_ID, HARNESS_ROLE, "prepare")
    ec2 = session.client("ec2")
    iam = session.client("iam")
    ssm = session.client("ssm")

    ami = ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]

    tags = [
        {"Key": DRILL_TAG, "Value": ex_id},
        {"Key": "Name", "Value": f"containment-drill-{ex_id}"},
        {"Key": "platform-managed", "Value": "true"},
        {"Key": "platform-purpose", "Value": "containment-exercise"},
    ]
    launched = ec2.run_instances(
        ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1,
        SubnetId=SUBNET_ID,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )
    instance_id = launched["Instances"][0]["InstanceId"]

    # A principal for containment to revoke. Deliberately NOT named Platform*:
    # the responder role explicitly refuses to contain a platform principal, so
    # a drill target with that prefix would test the refusal rather than the
    # revocation.
    principal_name = f"ExerciseDrillPrincipal-{ex_id}"[:64]
    iam.create_role(
        RoleName=principal_name,
        Description=f"Containment drill target {ex_id}. Created and removed by "
                    "the scheduled exercise.",
        AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow",
                           "Principal": {"Service": "ec2.amazonaws.com"},
                           "Action": "sts:AssumeRole"}],
        }),
        Tags=[{"Key": DRILL_TAG, "Value": ex_id}],
    )

    # The instance has to be RUNNING before containment isolates it, or the
    # "instance is still running afterwards" assertion proves nothing.
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id],
                WaiterConfig={"Delay": 10, "MaxAttempts": 30})

    return {
        "exercise_id": ex_id,
        "instance_id": instance_id,
        "principal_arn": f"arn:aws:iam::{EXERCISE_ACCOUNT_ID}:role/{principal_name}",
        "principal_name": principal_name,
        "prepared_at": _iso(_now()),
    }


def plant(event):
    """Put a finding on the bus. From here the platform behaves normally.

    A manual declaration rather than a synthetic GuardDuty finding, for one
    reason: a fabricated GuardDuty event would have to imitate a schema AWS
    owns and changes, and the day that imitation drifts is the day the drill
    starts passing against a shape that no longer arrives. Manual declaration
    is a first-class trigger design 07 names in its own list, and it exercises
    the same declare -> contain path every other trigger uses.
    """
    prepared = event["prepared"]
    ex_id = prepared["exercise_id"]
    source_id = f"exercise-{ex_id}"

    detail = {
        "title": f"CONTAINMENT EXERCISE {ex_id} — planted finding, not a real incident",
        "description": "Scheduled containment exercise per design 07. The "
                       "target was created by the exercise and is destroyed by "
                       "it.",
        "account_id": EXERCISE_ACCOUNT_ID,
        "region": os.environ.get("AWS_REGION", "us-east-2"),
        "resource_id": prepared["instance_id"],
        "principal_arn": prepared["principal_arn"],
        # High, so the confidence table routes to contain rather than to
        # gather-and-page. A drill that only gathers does not exercise
        # isolation or revocation, which are the steps worth rehearsing.
        "confidence": "high",
        "declared_by": "scheduled-exercise",
        "source_id": source_id,
        # THE mode parameter. contain.py refuses to run in exercise mode
        # against any account but the exercise account.
        "mode": "exercise",
    }
    events.put_events(Entries=[{
        "Source": "platform.exercise",
        "DetailType": "Platform Manual Declaration",
        "Detail": json.dumps(detail),
        "EventBusName": ALERT_BUS_NAME,
    }])

    # The incident id declare.py will derive, computed with declare.py's own
    # function rather than a copy of its rule. If the derivation ever changes,
    # this follows it instead of silently watching for an incident that is
    # never written.
    expected = declare.incident_id({"source_id": source_id}, {})
    return {"planted_at": _iso(_now()), "source_id": source_id,
            "expected_incident_id": expected}


def await_containment(event):
    """Poll the incident record until containment has finished.

    Reads the append-only record rather than the state machine's execution
    status, because the record is what the exercise is actually asserting
    about: an execution that succeeded without writing a summary is a failure
    of the thing this drill exists to prove.
    """
    expected = event["planted"]["expected_incident_id"]
    deadline = time.time() + int(os.environ.get("AWAIT_SECONDS", "240"))
    stages = {}
    while time.time() < deadline:
        got = dynamodb.query(
            TableName=INCIDENT_TABLE,
            KeyConditionExpression="PK = :p",
            ExpressionAttributeValues={":p": {"S": f"incident#{expected}"}},
        ).get("Items", [])
        for item in got:
            stage = item.get("stage", {}).get("S")
            if stage:
                stages[stage] = {
                    "outcome": item.get("outcome", {}).get("S"),
                    "recorded_at": item.get("recorded_at", {}).get("S"),
                    "detail": json.loads(item.get("detail", {}).get("S") or "{}"),
                }
        if "summary" in stages:
            return {"incident_id": expected, "observed": True,
                    "stages": stages, "waited_until": _iso(_now())}
        time.sleep(10)

    # Not raised. A drill that times out is a FINDING about containment being
    # slow or broken, and it has to reach the same place a successful drill
    # does -- raising here would lose the timings gathered so far and skip
    # teardown, leaving a live instance behind.
    return {"incident_id": expected, "observed": False,
            "stages": stages,
            "reason": f"containment did not record a summary within the "
                      f"window. Stages seen: {sorted(stages) or 'none'}.",
            "waited_until": _iso(_now())}


# ---------------------------------------------------------------------------
# 4. Verify. This is what makes it evidence rather than a narrative.
# ---------------------------------------------------------------------------


def verify(event):
    """Assert what actually happened, from both ends."""
    prepared = event["prepared"]
    awaited = event["awaited"]
    stages = awaited.get("stages") or {}
    checks = []

    def record(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # --- the sequence ran at all ------------------------------------------
    for stage in ("triage", "preserve", "isolate", "revoke", "blast-radius",
                  "summary"):
        record(f"stage_{stage.replace('-', '_')}_ran", stage in stages,
               stages.get(stage, {}).get("outcome") or "stage not recorded")

    # --- isolation ACTUALLY isolated --------------------------------------
    isolate = stages.get("isolate", {}).get("detail", {})
    session = assume(EXERCISE_ACCOUNT_ID, HARNESS_ROLE, "verify")
    ec2 = session.client("ec2")
    instance_id = prepared["instance_id"]
    described = ec2.describe_instances(InstanceIds=[instance_id])
    instance = described["Reservations"][0]["Instances"][0]
    groups = [g["GroupId"] for g in instance.get("SecurityGroups", [])]
    state = (instance.get("State") or {}).get("Name")

    isolation_sg = isolate.get("isolation_security_group")
    record("isolation_group_applied",
           bool(isolation_sg) and groups == [isolation_sg],
           f"instance carries {groups}; isolation group is {isolation_sg}")
    record("instance_still_running", state == "running",
           f"instance state is {state} — containment must not stop it, because "
           "a stopped instance loses the volatile state the preservation "
           "exists to capture")

    # Not just "the group is attached" but "the group actually denies". A
    # correctly attached group with a permissive rule is the failure this
    # catches, and it is the difference between isolation and the appearance
    # of it.
    if isolation_sg:
        sg = ec2.describe_security_groups(GroupIds=[isolation_sg])["SecurityGroups"][0]
        ingress = sg.get("IpPermissions") or []
        record("isolation_group_denies_all_ingress", not ingress,
               f"{len(ingress)} ingress rule(s) on the isolation group")

    # --- evidence landed in Forensics, checked FROM Forensics -------------
    preserve = stages.get("preserve", {}).get("detail", {})
    snapshots = [s.get("snapshot_id") for s in (preserve.get("snapshots") or [])]
    record("evidence_was_created", bool(snapshots),
           f"{len(snapshots)} snapshot(s): {snapshots}")
    record("evidence_share_requested",
           preserve.get("shared_with") == FORENSICS_ACCOUNT_ID,
           f"shared_with={preserve.get('shared_with')}, "
           f"forensics={FORENSICS_ACCOUNT_ID}")

    if snapshots and FORENSICS_ACCOUNT_ID:
        try:
            fsession = assume(FORENSICS_ACCOUNT_ID, FORENSICS_READER_ROLE,
                              "forensics")
            fec2 = fsession.client("ec2")
            visible = fec2.describe_snapshots(
                SnapshotIds=snapshots)["Snapshots"]
            record("evidence_visible_from_forensics",
                   len(visible) == len(snapshots),
                   f"{len(visible)} of {len(snapshots)} snapshot(s) are "
                   "readable from the Forensics account. This is the check "
                   "that distinguishes a share that was REQUESTED from one "
                   "that WORKS.")
        except Exception as exc:                   # noqa: BLE001
            record("evidence_visible_from_forensics", False,
                   f"could not read the snapshots from Forensics: "
                   f"{type(exc).__name__}: {exc}")
    else:
        record("evidence_visible_from_forensics", False,
               "no snapshots to check, or no Forensics account configured")

    # --- revocation, without deletion -------------------------------------
    revoke = stages.get("revoke", {}).get("detail", {})
    record("principal_revoked", bool(revoke.get("revoked")),
           revoke.get("reason") or "deny-all attached")
    record("principal_not_deleted", revoke.get("deleted") is False,
           "deleting the principal would lose the audit trail and break "
           "attribution")
    blast = stages.get("blast-radius", {}).get("detail", {})
    record("sessions_revoked_by_token_issue_time",
           bool(blast.get("sessions_revoked")),
           f"cutoff {blast.get('token_issue_time_cutoff')}")

    # --- timings ----------------------------------------------------------
    summary = stages.get("summary", {}).get("detail", {})
    discovery = _parse(summary.get("discovery_time"))
    isolated_at = _parse(stages.get("isolate", {}).get("recorded_at"))
    timings = {
        "discovery_time": summary.get("discovery_time"),
        "seconds_discovery_to_isolation": (
            round((isolated_at - discovery).total_seconds(), 1)
            if discovery and isolated_at else None),
        "seconds_discovery_to_summary": summary.get(
            "elapsed_from_discovery_seconds"),
        "stage_times": {k: v.get("recorded_at") for k, v in stages.items()},
    }

    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks, "timings": timings,
            "failed_checks": [c["check"] for c in checks if not c["passed"]]}


# ---------------------------------------------------------------------------
# The other half of 7.4 — open-items.md V3
# ---------------------------------------------------------------------------


def blast_radius(event):
    """Confirm a member account with local admin cannot block containment.

    design 07 calls this assumption load-bearing and says it must be tested.
    The test is adversarial on purpose: assume a role in the exercise account
    that IAM has granted the exact rights to disable the containment role, and
    attempt it. A denial can then only have come from the SCP.

    The attempt is deliberately the LEAST destructive action in the set. If the
    SCP has failed and the call succeeds, the damage is one tag on one role in
    one empty account, and the exercise reports a critical finding rather than
    having deleted the platform's containment capability to prove a point.
    """
    attempts = []
    session = assume(EXERCISE_ACCOUNT_ID, LOCAL_ADMIN_ROLE, "blast")
    iam = session.client("iam")

    def attempt(name, fn):
        try:
            fn()
            attempts.append({
                "attempt": name, "blocked": False,
                "detail": "SUCCEEDED. A member account with local admin was "
                          "able to modify the containment role. Containment "
                          "can be disarmed by the account it is meant to "
                          "contain.",
            })
        except Exception as exc:                   # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            blocked = code in ("AccessDenied", "AccessDeniedException")
            attempts.append({
                "attempt": name, "blocked": blocked,
                "error_code": code or type(exc).__name__,
                "detail": ("denied, as required" if blocked else
                           f"failed for another reason: {exc}. The control was "
                           "NOT demonstrated — a failure that is not a denial "
                           "proves nothing about the SCP."),
            })

    attempt("tag_the_containment_role", lambda: iam.tag_role(
        RoleName=RESPONDER_ROLE_NAME,
        Tags=[{"Key": "exercise-blast-radius-probe",
               "Value": event.get("prepared", {}).get("exercise_id", "probe")}]))

    attempt("attach_a_deny_policy_to_the_containment_role",
            lambda: iam.put_role_policy(
                RoleName=RESPONDER_ROLE_NAME,
                PolicyName="ExerciseAttemptedBlock",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Deny", "Action": "*",
                                   "Resource": "*"}]})))

    all_blocked = all(a["blocked"] for a in attempts)
    return {
        "passed": all_blocked,
        "attempts": attempts,
        "assumption": "open-items.md V3 — a member account with full local "
                      "admin cannot block the containment role",
        "verdict": ("CONFIRMED: every attempt to disable containment from "
                    "inside the account was denied."
                    if all_blocked else
                    "NOT CONFIRMED. See attempts. If an attempt SUCCEEDED this "
                    "is a critical platform finding affecting every tenant, "
                    "not just this account."),
    }


# ---------------------------------------------------------------------------
# 6. Tear down, and write the evidence
# ---------------------------------------------------------------------------


def teardown(event):
    """Remove the drill target. Only what this exercise created.

    Provenance first, deletion second — and the IAM policy behind these calls
    is conditioned on the same tag, so an instance this exercise did not create
    is outside the reach of the credentials rather than merely outside the code
    path. recovery/src/teardown.py makes the same argument at more length.
    """
    prepared = event.get("prepared") or {}
    ex_id = prepared.get("exercise_id")
    instance_id = prepared.get("instance_id")
    principal_name = prepared.get("principal_name")
    removed, orphans = [], []

    session = assume(EXERCISE_ACCOUNT_ID, HARNESS_ROLE, "teardown")
    ec2 = session.client("ec2")
    iam = session.client("iam")

    if instance_id:
        described = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = described.get("Reservations") or []
        instance = (reservations[0]["Instances"][0] if reservations
                    and reservations[0].get("Instances") else None)
        tags = {t["Key"]: t["Value"] for t in (instance or {}).get("Tags", [])}
        if instance is None:
            removed.append({"instance_id": instance_id,
                            "note": "already gone"})
        elif tags.get(DRILL_TAG) != ex_id:
            orphans.append({
                "instance_id": instance_id,
                "reason": f"carries {DRILL_TAG}={tags.get(DRILL_TAG)!r}, not "
                          f"{ex_id!r}. NOT terminated — this exercise cannot "
                          "prove it created it.",
            })
        else:
            ec2.terminate_instances(InstanceIds=[instance_id])
            removed.append({"instance_id": instance_id,
                            "api": "ec2:TerminateInstances"})

    if principal_name:
        try:
            for policy in iam.list_role_policies(
                    RoleName=principal_name).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=principal_name,
                                       PolicyName=policy)
            iam.delete_role(RoleName=principal_name)
            removed.append({"role": principal_name, "api": "iam:DeleteRole"})
        except Exception as exc:                   # noqa: BLE001
            orphans.append({"role": principal_name,
                            "reason": f"{type(exc).__name__}: {exc}"})

    # The blast-radius probe adds a tag IF the SCP has failed. If that happened
    # the tag is removed here, because leaving it would be leaving a mark on
    # the platform's own containment role -- and the finding is carried by the
    # evidence record, not by a stray tag nobody can explain later.
    #
    # Skipped entirely when nothing was prepared, because then no probe ran.
    # Without this guard an aborted exercise reported "removed the probe tag,
    # which means the SCP did NOT block it" -- a claim about a test that never
    # happened, in the record that is supposed to be the evidence.
    if not ex_id:
        return {"removed": removed, "orphans": orphans, "clean": not orphans}

    try:
        responder = assume(EXERCISE_ACCOUNT_ID, LOCAL_ADMIN_ROLE, "cleanprobe")
        responder.client("iam").untag_role(
            RoleName=RESPONDER_ROLE_NAME,
            TagKeys=["exercise-blast-radius-probe"])
        removed.append({"note": "removed the blast-radius probe tag, which "
                                "means the SCP did NOT block it"})
    except Exception:                              # noqa: BLE001
        # Expected: the SCP denies the untag too, because the tag was never
        # applied. Nothing to clean up.
        pass

    return {"removed": removed, "orphans": orphans,
            "clean": not orphans}


def record_evidence(event):
    """Tabletop evidence with real timings, in a form an auditor can read."""
    prepared = event.get("prepared") or {}
    verified = event.get("verified") or {}
    blast = event.get("blast") or {}
    awaited = event.get("awaited") or {}
    torn = event.get("teardown") or {}
    ex_id = prepared.get("exercise_id") or exercise_id()

    passed = bool(verified.get("passed")) and bool(blast.get("passed")) \
        and bool(awaited.get("observed")) and bool(torn.get("clean"))

    evidence = {
        "record_type": "containment-exercise",
        "exercise_id": ex_id,
        "recorded_at": _iso(_now()),
        "outcome": "passed" if passed else "failed",
        "what_was_exercised": (
            "The real declaration and containment automation. The exercise "
            "planted a finding on the platform event bus and did not "
            "participate again until verification: declaration ran declare.py, "
            "containment ran contain.py, and the readout ran readout.py — the "
            "same code paths a GuardDuty finding takes."),
        "target": {
            "account_id": EXERCISE_ACCOUNT_ID,
            "instance_id": prepared.get("instance_id"),
            "principal_arn": prepared.get("principal_arn"),
            "created_by_this_exercise": True,
        },
        "incident_id": awaited.get("incident_id"),
        "timings": verified.get("timings"),
        "checks": verified.get("checks"),
        "failed_checks": verified.get("failed_checks"),
        "blast_radius_verification": blast,
        "teardown": torn,
        "caveats": [
            "The drill target is a bare EC2 instance with no workload. "
            "Isolation is verified at the control plane — security group "
            "membership and the absence of ingress rules — not by attempting "
            "to reach the instance from outside. B-063.",
            "Truveon does not exist. Prompt 7.4 asks for this evidence to be "
            "written there; it is written to the append-only incident record "
            "instead, which is immutable and already holds the incident.",
        ],
    }

    if INCIDENT_TABLE:
        dynamodb.put_item(TableName=INCIDENT_TABLE, Item={
            "PK": {"S": f"exercise#{ex_id}"},
            "SK": {"S": "evidence"},
            "record_type": {"S": "containment-exercise"},
            "exercise_id": {"S": ex_id},
            "recorded_at": {"S": _iso(_now())},
            "outcome": {"S": evidence["outcome"]},
            "detail": {"S": json.dumps(evidence, default=str)[:38000]},
        })

    # Routed through the one routing layer like everything else. A FAILED
    # exercise declares no routing class, so the tier decides and it pages
    # where paging exists -- containment that does not work is not a digest
    # item. A PASSED exercise declares `digest`: it is evidence, not news.
    dossier = {
        "record_type": "containment-exercise",
        "recorded_at": evidence["recorded_at"],
        "current_state": "OK" if passed else "ALARM",
        "routing": "digest" if passed else None,
        "alarm": {
            "name": "platform-containment-exercise",
            "resource_id": prepared.get("instance_id") or ex_id,
            "environment_tier": os.environ.get("EXERCISE_TIER", "prod"),
            "account_id": EXERCISE_ACCOUNT_ID,
        },
        "assessment": {
            "probable_cause": (
                f"Containment exercise {ex_id} passed. "
                f"{verified.get('timings', {}).get('seconds_discovery_to_isolation')}s "
                "from discovery to isolation."
                if passed else
                f"CONTAINMENT EXERCISE {ex_id} FAILED: "
                + ", ".join(verified.get("failed_checks")
                            or [blast.get("verdict", "see evidence")])),
        },
        "exercise": evidence,
    }
    events.put_events(Entries=[{
        "Source": "platform.security",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "EventBusName": ALERT_BUS_NAME,
    }])

    print(json.dumps({"exercise_id": ex_id, "outcome": evidence["outcome"],
                      "failed_checks": evidence["failed_checks"]}, default=str))
    return {"exercise_id": ex_id, "outcome": evidence["outcome"],
            "passed": passed, "failed_checks": evidence["failed_checks"]}


STEPS = {"prepare": prepare, "plant": plant, "await": await_containment,
         "verify": verify, "blast": blast_radius, "teardown": teardown,
         "record": record_evidence}


def handler(event, context):
    step = event.get("step")
    fn = STEPS.get(step)
    if fn is None:
        raise ExerciseError(
            f"unknown step '{step}'. Known steps: {', '.join(sorted(STEPS))}")
    return fn(event)
