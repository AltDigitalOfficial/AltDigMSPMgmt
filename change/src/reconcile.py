"""Change reconciliation. Prompt 8.2, design/08-change-and-release.md.

    Intent:   Pipeline registers a change in Jira before deploying
    Reality:  CloudTrail + EventBridge capture what actually happened
    Judgment: Reconciliation compares the two; mismatches are findings

Why this is a DETECTIVE control and not a gate
----------------------------------------------
design 08's constraint, which shapes everything here:

    Development teams are not overseen by AltDigital. SOC 2 requires visibility
    and approval of changes to the environment. We therefore cannot rely on a
    gate we control -- change management is a detective control, reconciled
    continuously.

So nothing in this module blocks a deployment. It observes what happened and
compares it to what was declared, and the gap is the finding. design 08 is
direct about why that is acceptable, and it is worth keeping in view while
reading the outcomes below:

    Auditors accept continuous reconciliation far more readily than a signature
    on a form, because it is evidence rather than a claim.

The fifth outcome
-----------------
design 08's table has four. This implements five, and the extra one is the
important one:

    change_source_unavailable

If Jira cannot be read -- expired token, wrong project, an issue type that does
not exist, an outage -- the engine does NOT conclude "no approved change" and
report every deployment in the estate as unapproved. That is B-054's lesson
(a denied lookup reported as a negative result) with the blast radius of B-043
(a finding on every run, every account, until somebody mutes it).

An unreadable change source is a finding about the PLATFORM, raised once per
run, not a finding about each deployment.
"""
import datetime
import hashlib
import json
import os

import boto3

import jira

cloudwatch = boto3.client("cloudwatch")
dynamodb = boto3.client("dynamodb")
events = boto3.client("events")
ssm = boto3.client("ssm")

CHANGE_TABLE = os.environ.get("CHANGE_TABLE", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
CHANGE_ISSUETYPE = os.environ.get("JIRA_CHANGE_ISSUETYPE", "Change")
DEFAULT_CHANGE_PROJECT = os.environ.get("JIRA_DEFAULT_CHANGE_PROJECT", "")
# AltDigital's OWN operations project. design 08 treats the platform's pipeline
# as a separate thing from a tenant's -- "The platform's own pipeline [...] the
# most privileged thing AltDigital owns" -- with its own approver. A baseline
# rollout is a change, but it is not the TENANT's change, and reconciling it
# against the tenant's records would report the tenant as having failed to file
# something they were never party to.
OPS_CHANGE_PROJECT = os.environ.get("JIRA_OPS_PROJECT", "")
TENANT_FACTS_PARAM = os.environ.get("TENANT_FACTS_PARAM",
                                    "/platform/change/tenant-facts")

# When the Jira token expires, as YYYY-MM-DD. Atlassian caps token lifetime at
# a year, and the expiry notice goes to the Atlassian account's email rather
# than to anything the platform controls.
#
# Published as a metric on every reconciliation run so the platform notices
# before the credential dies. This CAN be a CloudWatch alarm, unlike the
# restore-test freshness signal in B-050: reconciliation runs every six hours,
# so the metric is dense enough for an ordinary threshold alarm.
TOKEN_EXPIRES = os.environ.get("JIRA_TOKEN_EXPIRES", "")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AltDigital/Change")

# How far back a reconciliation run looks. Deliberately wider than the schedule
# interval: a run that looked back exactly one interval would lose any
# deployment that landed while the previous run was executing, and the gap
# would never be revisited because reconciliation is not retrospective unless
# somebody asks it to be.
LOOKBACK_HOURS = int(os.environ.get("RECONCILE_LOOKBACK_HOURS", "26"))

# How close in time a change record has to be to count as covering a
# deployment. design 08 does not give a number; this is the build's.
#
# Generous rather than tight, because the failure modes are asymmetric. Too
# tight and a legitimate deployment that ran long is reported as unapproved,
# which is a false accusation against a tenant. Too loose and an unapproved
# deployment hides behind an unrelated change record, which reconciliation
# would catch on the next record that has no deployment. A false positive
# costs trust; a false negative costs a day.
MATCH_WINDOW_HOURS = float(os.environ.get("CHANGE_MATCH_WINDOW_HOURS", "24"))

_FACTS = {}

# Approval lookups, keyed by issue key, for the life of one invocation. An
# issue is checked once per run however many deployments it is compared
# against, which stops a busy account becoming one API call per deployment per
# change record.
_APPROVAL_CACHE = {}

# Who may approve an INFRASTRUCTURE change, and how many must. Published by
# scripts/sync-change-approvers.sh from a gitignored file of accountIds.
#
# Jira's own rule is "any N members of the Approver group", which does not check
# identities against a list and does not exclude the requester. Decided
# 2026-10-01: two approvals from Wayne, Art and Tina, and the person who built
# the change does not count. Applied only to changes in AltDigital's ops
# project, which is where every PLATFORM deployment is reconciled. A tenant's
# application changes follow the tenant's own process.
INFRA_APPROVERS_PARAM = os.environ.get("INFRA_APPROVERS_PARAM",
                                       "/platform/change/infrastructure-approvers")
_INFRA_APPROVERS = {}


def infrastructure_approvers():
    """(accountIds, minimum), or (None, reason) when the list cannot be read.

    Unreadable is NOT "nobody is authorised". Treating it that way would turn a
    missing parameter into every platform deployment reported as unapproved --
    a configuration fault presenting as a compliance failure, the shape this
    module refuses everywhere else. The caller records that identities were
    not checked instead.
    """
    if "value" in _INFRA_APPROVERS:
        return _INFRA_APPROVERS["value"]
    try:
        raw = ssm.get_parameter(Name=INFRA_APPROVERS_PARAM)["Parameter"]["Value"]
        data = json.loads(raw)
        approvers = {str(a) for a in data["approvers"] if str(a).strip()}
        minimum = int(data.get("minimum", 2))
        value = (approvers, minimum) if approvers else (
            None, f"{INFRA_APPROVERS_PARAM} lists nobody")
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        value = (None, f"could not read {INFRA_APPROVERS_PARAM}: "
                       f"{code or type(exc).__name__}; run "
                       "scripts/sync-change-approvers.sh")
    _INFRA_APPROVERS["value"] = value
    return value


def approving_accounts(approvals):
    """accountIds of everyone who APPROVED, across every approval step.

    The same reading as quorum/src/verify.py's decided_approvals, confirmed
    there against a live record: one approval object per workflow step, each
    holding a list of approvers with their own decision. Two approvals means
    two distinct people, not two steps. Duplicated rather than imported
    because the two Lambdas are packaged separately.
    """
    seen = set()
    for approval in approvals or []:
        for entry in (approval.get("approvers") or []):
            if (entry.get("approverDecision") or "").lower() != "approved":
                continue
            account = (entry.get("approver") or {}).get("accountId")
            if account:
                seen.add(account)
    return seen


APP_APPROVERS_PREFIX = os.environ.get("APP_APPROVERS_PREFIX",
                                      "/platform/change/application-approvers/")
_APP_APPROVERS = {}


def application_approvers(tenant):
    """{"required", "pool", "minimum"} for a tenant, or (None, reason).

    Per tenant, published by scripts/sync-application-approvers.sh. The
    accountIds are those on the tenant's own Jira site where its records live
    there. Absent is common -- most tenants will not have published one yet --
    and, like the infrastructure list, an absent list never accuses: the
    evidence says identities were not checked.
    """
    if tenant in _APP_APPROVERS:
        return _APP_APPROVERS[tenant]
    name = f"{APP_APPROVERS_PREFIX}{tenant}"
    try:
        data = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
        value = ({"required": set(data.get("required") or []),
                  "pool": set(data.get("pool") or []),
                  "minimum": int(data.get("minimum", 2))}, None)
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        value = (None, ("no application approver list published for "
                        f"{tenant}" if code == "ParameterNotFound" else
                        f"could not read {name}: {code or type(exc).__name__}"))
    _APP_APPROVERS[tenant] = value
    return value


def application_verdict(change, approvals):
    """The tenant's rule: every required approver, `minimum` in total, requester excluded."""
    rule, reason = application_approvers(change.get("tenant"))
    if rule is None:
        return True, f"JSM approval record; approver identities NOT checked ({reason})"
    approved = approving_accounts(approvals) - {change.get("reporter_account_id")}
    missing = rule["required"] - approved
    counted = approved & (rule["required"] | rule["pool"])
    if missing:
        return False, (f"JSM approval record, but {len(missing)} required "
                       "approver(s) did not approve (the requester cannot stand in)")
    if len(counted) < rule["minimum"]:
        return False, (f"JSM approval record, but only {len(counted)} listed "
                       f"approver(s) other than the requester; {rule['minimum']} required")
    return True, (f"JSM approval record; required approver(s) and {len(counted)} "
                  "listed approver(s) in total, requester excluded")


def infrastructure_verdict(change, approvals):
    """Apply the infrastructure rule to an otherwise-approved change.

    Returns (approved, evidence).
    """
    authorised, minimum = infrastructure_approvers()
    if authorised is None:
        # `minimum` carries the reason here.
        return True, ("JSM approval record; approver identities NOT checked "
                      f"({minimum})")
    counted = (approving_accounts(approvals) & authorised) \
        - {change.get("reporter_account_id")}
    if len(counted) >= minimum:
        return True, (f"JSM approval record; {len(counted)} authorised "
                      "infrastructure approver(s), requester excluded")
    return False, (f"JSM approval record, but only {len(counted)} authorised "
                   f"infrastructure approver(s) other than the requester; "
                   f"{minimum} required")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        # Jira returns offsets without a colon: 2026-09-19T12:00:00.000+0000.
        # fromisoformat rejects that on Python < 3.11 and accepts it on 3.11+;
        # normalising here means the code does not depend on which runtime it
        # happens to land on.
        if len(text) > 5 and (text[-5] in "+-") and text[-3] != ":":
            text = text[:-2] + ":" + text[-2:]
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Capture. design 08's six deployment event types.
# ---------------------------------------------------------------------------
#
# "What is captured as a deployment event", verbatim from design 08:
#
#   - AppDeployer role assumption
#   - ECS task definition revisions and service updates
#   - Lambda function code and configuration updates
#   - CloudFormation stack create/update
#   - EC2 launch template and AMI changes
#   - RDS parameter group and engine version changes
#
# Each maps to CloudTrail event names. The mapping is data rather than code so
# that adding a type is an edit to a table and not to a dispatch chain.

DEPLOYMENT_EVENTS = {
    # design 08 lists this first, and it is the one that matters most: it is
    # the control point itself. A deployment that did NOT come through
    # AppDeployer is a different and worse finding than one that did without a
    # change record.
    "AssumeRoleWithWebIdentity": "appdeployer-assumption",

    "RegisterTaskDefinition": "ecs-task-definition",
    "UpdateService": "ecs-service-update",
    "CreateService": "ecs-service-update",

    "UpdateFunctionCode": "lambda-code",
    "UpdateFunctionConfiguration": "lambda-configuration",
    "PublishVersion": "lambda-code",

    "CreateStack": "cloudformation-stack",
    "UpdateStack": "cloudformation-stack",
    "ExecuteChangeSet": "cloudformation-stack",

    "CreateLaunchTemplate": "ec2-launch-template",
    "CreateLaunchTemplateVersion": "ec2-launch-template",
    "ModifyLaunchTemplate": "ec2-launch-template",

    "ModifyDBParameterGroup": "rds-parameter-group",
    "ModifyDBInstance": "rds-engine-or-config",
    "ModifyDBCluster": "rds-engine-or-config",
}


# Who deployed, which decides WHOSE change record should cover it.
#
# design 08 draws this line and the first version of this engine did not. The
# very first captured deployment was made by OrganizationAccountAccessRole --
# AltDigital deploying a stack by hand -- and was reported as the TENANT having
# deployed without a change record. B-074.
PLATFORM_PRINCIPALS = ("OrganizationAccountAccessRole", "stacksets-exec-",
                       "AWSCloudFormationStackSetExecutionRole", "Platform")

# The tenant CI role. Renamed from PlatformAppDeployer- to TenantAppDeployer-
# so that no SCP exemption covers it (B-136). The old prefix stays recognised
# until no reconciliation window can still contain its events, so a deploy made
# just before the rename does not reclassify as an unknown credential.
#
# Matched with the leading '/', which is the role-name boundary in both
# role/<name> and assumed-role/<name>/<session>. A bare substring would let a
# role called EvilTenantAppDeployer-x pass as the tenant's deployer.
TENANT_DEPLOYER_MARKERS = ("/TenantAppDeployer-", "/PlatformAppDeployer-")


def is_tenant_deployer(arn):
    s = str(arn or "")
    return any(m in s for m in TENANT_DEPLOYER_MARKERS)


def deployer_class(principal_arn):
    """tenant | platform | unknown.

    `unknown` is the most interesting of the three and deserves its own
    attention: design 08's premise is that AppDeployer is the ONLY path a
    tenant deploys through. A deployment by neither AppDeployer nor a platform
    role came from a credential nobody has accounted for, which is a finding
    about the control point itself rather than about paperwork.
    """
    arn = str(principal_arn or "")
    # Tenant first: the old PlatformAppDeployer- name also contains "Platform".
    if is_tenant_deployer(arn):
        return "tenant"
    for marker in PLATFORM_PRINCIPALS:
        if marker in arn:
            return "platform"
    return "unknown"


def classify_event(event):
    detail = event.get("detail") or {}
    name = detail.get("eventName")
    kind = DEPLOYMENT_EVENTS.get(name)
    if kind is None:
        return None

    identity = detail.get("userIdentity") or {}
    arn = identity.get("arn") or ""
    role_arn = (detail.get("requestParameters") or {}).get("roleArn", "")
    # Only AppDeployer assumptions count as the control point. A
    # AssumeRoleWithWebIdentity for some other role is somebody else's
    # federation and is not a deployment into this estate.
    if kind == "appdeployer-assumption" and not is_tenant_deployer(role_arn):
        return None

    # For the assumption itself, the evidence is the ROLE ASSUMED, not the
    # caller. A WebIdentityUser has no `arn` in CloudTrail (only userName, the
    # OIDC subject), so classifying by the caller always answered "unknown".
    # That turned the one event proving a deploy came THROUGH the control point
    # into a finding that it had bypassed it: "credentials nobody has accounted
    # for". Found on the first real OIDC assumption, 2026-10-01 (B-142).
    if kind == "appdeployer-assumption":
        via_appdeployer, deployer = True, "tenant"
    else:
        via_appdeployer, deployer = is_tenant_deployer(arn), deployer_class(arn)

    return {
        "event_name": name,
        "kind": kind,
        "account_id": detail.get("recipientAccountId") or event.get("account"),
        "region": detail.get("awsRegion") or event.get("region"),
        "occurred_at": detail.get("eventTime") or event.get("time"),
        "principal_arn": arn,
        "principal_type": identity.get("type"),
        # The deployer session name, which for an OIDC assumption is the CI run
        # -- the single most useful field when asking "which pipeline run did
        # this".
        "session": (identity.get("sessionContext") or {}).get(
            "sessionIssuer", {}).get("userName") or identity.get("userName"),
        "resource": _resource_of(detail),
        "error_code": detail.get("errorCode"),
        "event_id": detail.get("eventID"),
        "via_appdeployer": via_appdeployer,
        "deployer": deployer,
    }


def _resource_of(detail):
    rp = detail.get("requestParameters") or {}
    for key in ("stackName", "functionName", "service", "family",
                "dBParameterGroupName", "dBInstanceIdentifier",
                "launchTemplateName", "roleArn"):
        if rp.get(key):
            return str(rp[key])[:256]
    resources = detail.get("resources") or []
    if resources and isinstance(resources[0], dict):
        return str(resources[0].get("ARN") or "")[:256]
    return None


def capture(event, context=None):
    """Record one deployment event. Append-only, like every other record here.

    Stored rather than reconciled immediately, for a reason worth stating: a
    change record is often filed minutes AFTER the pipeline starts, so
    reconciling at capture time would report half of a well-behaved tenant's
    deployments as unapproved. Reconciliation runs on a schedule, over a
    window, once the dust has settled.
    """
    record = classify_event(event)
    if record is None:
        out = {"captured": False,
               "reason": "not a deployment event",
               "event_name": (event.get("detail") or {}).get("eventName")}
        print(json.dumps(out))
        return out

    if CHANGE_TABLE:
        key = hashlib.sha256(
            (record.get("event_id") or json.dumps(record, sort_keys=True)
             ).encode()).hexdigest()[:24]
        dynamodb.put_item(TableName=CHANGE_TABLE, Item={
            "PK": {"S": f"deployment#{record['account_id']}"},
            "SK": {"S": f"{record['occurred_at']}#{key}"},
            "record_type": {"S": "deployment-event"},
            "account_id": {"S": str(record["account_id"])},
            "occurred_at": {"S": str(record["occurred_at"])},
            "kind": {"S": record["kind"]},
            "detail": {"S": json.dumps(record, default=str)[:38000]},
        })

    print(json.dumps({"captured": True, "kind": record["kind"],
                      "account_id": record["account_id"],
                      "via_appdeployer": record["via_appdeployer"]}))
    return {"captured": True, "record": record}


# ---------------------------------------------------------------------------
# Tenant facts
# ---------------------------------------------------------------------------


def tenant_facts(account_id):
    """Which Jira project holds this account's change records.

    Per tenant, not global. design 08 routes an unapproved deployment to "the
    tenant's change approver", and tenants must not see each other's change
    records -- so the project is a property of the account, published into this
    account the same way the tier map and the notification windows are.

    Falls back to the configured default, which exists so the sandbox can be
    exercised before any tenant is vested. The fallback is RECORDED in the
    outcome rather than applied silently: reconciling a tenant's deployments
    against somebody else's change project would produce confident nonsense.
    """
    if not _FACTS:
        try:
            raw = ssm.get_parameter(Name=TENANT_FACTS_PARAM)["Parameter"]["Value"]
            _FACTS.update(json.loads(raw))
        except Exception as exc:                   # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            _FACTS["__error__"] = code or type(exc).__name__

    entry = (_FACTS.get(account_id) or {}) if account_id else {}
    project = entry.get("jira_change_project")
    if project:
        # jira_site / jira_secret: the tenant's own Atlassian site, when its
        # change records are not on AltDigital's (questionnaire
        # change.ticketing). None means AltDigital's site.
        return {"project": project, "source": "tenant record",
                "change_approver": entry.get("change_approver"),
                "jira_site": entry.get("jira_site"),
                "jira_secret": entry.get("jira_secret"),
                # <partner>-<client>: which application approver list applies.
                "tenant": (f"{entry['partner']}-{entry['client']}"
                           if entry.get("partner") and entry.get("client") else None)}
    if DEFAULT_CHANGE_PROJECT:
        return {"project": DEFAULT_CHANGE_PROJECT,
                "source": ("platform default -- no per-tenant change project "
                           "is recorded for this account"),
                "change_approver": entry.get("change_approver")}
    return {"project": None,
            "source": ("no change project is configured for this account and "
                       "no platform default is set"),
            "change_approver": entry.get("change_approver")}


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def approved_changes(project):
    """Change records in the window, with their planned windows.

    Verifies the ISSUE TYPE EXISTS before searching. Without that check, a
    wrong JIRA_CHANGE_ISSUETYPE returns zero issues and the engine would report
    every deployment in the estate as unapproved -- a configuration mistake
    presenting as a compliance catastrophe. open-items.md B9 (the golden Jira
    scheme) is what finally settles the correct value.
    """
    types = jira.project_issue_types(project)
    if CHANGE_ISSUETYPE not in types:
        raise jira.JiraUnavailable(
            f"project {project} has no issue type named "
            f"'{CHANGE_ISSUETYPE}'. It has: {', '.join(types) or 'none'}. "
            "No conclusion can be drawn about whether deployments were "
            "approved -- this is a configuration fault, not a finding about "
            "any deployment. See open-items.md B9.")

    since = _iso(_now() - datetime.timedelta(hours=LOOKBACK_HOURS * 2))
    jql = (f'project = "{project}" AND issuetype = "{CHANGE_ISSUETYPE}" '
           f'AND updated >= "{since[:10]}" ORDER BY created DESC')
    issues = jira.search(jql, fields=["summary", "status", "created", "updated",
                                      "description", "labels", "reporter"])
    changes = []
    for issue in issues:
        f = issue.get("fields") or {}
        status = ((f.get("status") or {}).get("name") or "").lower()
        changes.append({
            "key": issue.get("key"),
            "summary": f.get("summary"),
            "status": (f.get("status") or {}).get("name"),
            # A GUESS from the status name, used only when there is no
            # approval record to read. See is_approved() below.
            #
            # The list is a heuristic over workflow names this code does not
            # control, and it was wrong the first time it met a real workflow:
            # AMI-4 was approved and sat in "Awaiting implementation", which is
            # not in this list, so the deployment it covered was reported as
            # having no change record. Adding that one string would have fixed
            # this workflow and broken on the next tenant's.
            "approved_by_status": status in (
                "approved", "done", "closed", "resolved", "in progress",
                "implementing", "awaiting implementation"),
            "created": f.get("created"),
            "updated": f.get("updated"),
            # The requester, by accountId. Fetched all along and discarded;
            # kept since 2026-10-01 because an infrastructure change's
            # requester must not count towards its own approval.
            "reporter_account_id": (f.get("reporter") or {}).get("accountId"),
            "labels": f.get("labels") or [],
            # Accounts a change claims to touch, by label. A convention rather
            # than a custom field, because a label needs no scheme change and
            # B9 has not settled the scheme. e.g. account-754280127660
            "accounts": [l.split("account-", 1)[1] for l in (f.get("labels") or [])
                         if l.startswith("account-")],
        })
    return changes


def match(deployment, changes):
    """Does an approved change cover this deployment?

    Matching is by ACCOUNT and TIME, not by resource name. Resource-level
    matching sounds stronger and is brittle: a change record says "deploy the
    payments service" and CloudTrail says `UpdateFunctionCode` on
    `payments-api-prod-handler`, and no string comparison reliably bridges
    that. Account plus a time window is coarse, honest, and catches the thing
    the control exists to catch -- a deployment nobody declared.
    """
    when = _parse(deployment.get("occurred_at"))
    if when is None:
        return None, "the deployment event carries no usable timestamp"

    window = datetime.timedelta(hours=MATCH_WINDOW_HOURS)
    candidates = []
    for change in changes:
        if change["accounts"] and deployment.get("account_id") not in change["accounts"]:
            continue
        created = _parse(change.get("created"))
        if created is None:
            continue
        # A change record filed AFTER the deployment it covers is not approval,
        # it is a retrospective note. design 08's third outcome exists for
        # exactly this shape, so it is matched but flagged rather than dropped.
        delta = (when - created).total_seconds()
        if -3600 <= delta <= window.total_seconds():
            candidates.append((abs(delta), change, delta))

    if not candidates:
        return None, "no change record covers this deployment"
    candidates.sort(key=lambda c: c[0])
    _, change, delta = candidates[0]
    approved, evidence = is_approved(change)
    if not approved:
        return change, (f"change {change['key']} covers this deployment but is "
                        f"not approved ({evidence}; status "
                        f"'{change['status']}')")
    if delta < 0:
        return change, (f"change {change['key']} was created AFTER the "
                        "deployment -- a retrospective record, not an approval")
    return change, None


def is_approved(change):
    """Is this change approved? Approval RECORDS first, status name second.

    Returns (approved, evidence) where evidence names which source answered,
    because the two are not equally trustworthy and the finding should say
    which one it rests on.

    Why not the status field, which was what this used to use. The status name
    is a string this code does not control: JSM's change workflow parks an
    approved record in "Awaiting implementation", tenant workflows will use
    other words, and a hardcoded list of approved-sounding names is wrong for
    every project nobody thought of. It was wrong for the very first real one.
    The failure is not subtle either -- an approved change reads as unapproved
    and every deployment it covers becomes a finding against the tenant.

    The approval record is the actual evidence and does not depend on anybody
    naming a status the way this code expects. design 08 says as much for the
    quorum pipeline; the only reason 8.2 did not read approvals is that they
    were unreadable until B-077 made the records JSM requests.

    The status heuristic survives as a FALLBACK, for two cases that are not
    errors:
      - a record created through the issue API, which cannot carry a JSM
        approval at all (AMI-1 and AMI-2 are both this)
      - a tenant project that is plain Jira Software with no approvals

    An unreadable approvals endpoint falls back too rather than concluding
    "not approved". Same rule as everywhere else in this module: a source that
    cannot be read has not returned a negative answer.
    """
    # Keyed by site and layer as well as issue. Site, because two Atlassian
    # sites can both hold an ARC8-1. Layer, because with no ops project
    # configured a platform deployment falls back to the tenant's project, and
    # the same record can then be judged under both rules in one run.
    cache_key = (jira.current_site(), change["key"], change.get("layer"))
    cached = _APPROVAL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = None
    try:
        approvals = jira.approvals(change["key"])
    except jira.JiraUnavailable:
        # 404 here is the normal answer for an issue that is not a JSM
        # request, not a fault. Fall back quietly.
        approvals = None

    if approvals:
        # Any step still awaiting a decision means not yet approved. All steps
        # final and approved means approved.
        decided = [(a.get("finalDecision") or "").lower() for a in approvals]
        if all(d == "approved" for d in decided):
            result = (True, "JSM approval record")
            if change.get("layer") == "infrastructure":
                result = infrastructure_verdict(change, approvals)
            elif change.get("layer") == "application":
                result = application_verdict(change, approvals)
        else:
            result = (False, f"JSM approval record says {sorted(set(decided))}")
    elif approvals is not None:
        # Readable and empty: the record has not reached its approval step.
        # Not a refusal, and not approved either.
        result = (False, "no approval recorded yet")
    else:
        result = (change["approved_by_status"],
                  "no approval record readable, fell back to the status name")
        if change.get("layer") in ("infrastructure", "application") and result[0]:
            # A status name carries no approver identities, so neither layer's
            # rule can be applied. Said, not implied.
            result = (True, result[1] + f"; {change['layer']} approver "
                                        "identities NOT checked")

    # Cached OUTSIDE the change record. It used to be stashed on the dict
    # itself, which put `"_approved": [true, "fell back to ..."]` into every
    # emitted finding -- an internal tuple in a record an auditor reads.
    #
    # The evidence itself is worth keeping, so it goes in under a name that
    # means something. Which source answered is exactly what somebody
    # assessing the finding needs: a status-name guess and a recorded approval
    # are not equally good grounds for accusing a tenant.
    _APPROVAL_CACHE[cache_key] = result
    change["approval_evidence"] = result[1]
    return result


def token_days_remaining():
    """Days until the Jira credential expires, or None if unknown."""
    if not TOKEN_EXPIRES:
        return None
    try:
        expires = datetime.datetime.fromisoformat(
            TOKEN_EXPIRES).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return round((expires - _now()).total_seconds() / 86400.0, 1)


def publish_credential_health():
    """One metric, every run.

    Deliberately a metric rather than a finding-per-run. A finding would fire
    every six hours for however many days the warning window is, which is how a
    real signal becomes something people filter. A metric plus one alarm says
    the same thing once.
    """
    days = token_days_remaining()
    if days is None:
        return None
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[{"MetricName": "JiraTokenDaysRemaining",
                     "Value": days, "Unit": "Count"}])
    return days


def _reconcile_account(account_id, deployer, account_deployments):
    """Reconcile one account's deployments of one deployer class.

    Extracted from reconcile() unchanged on 2026-10-01 so the caller can
    run it inside jira.using() -- a tenant's change records may live on
    its own Atlassian site. Returns the outcome records for the account.
    """
    results = []
    facts = tenant_facts(account_id)
    if deployer == "platform":
        project = OPS_CHANGE_PROJECT or facts["project"]
        facts = dict(facts, project=project, source=(
            "AltDigital's own operations project — this was a PLATFORM "
            "deployment, not a tenant one, and design 08 treats the "
            "platform's pipeline as separate"
            if OPS_CHANGE_PROJECT else
            "no ops project configured; fell back to the tenant's, which "
            "is wrong for a platform deployment"))
    else:
        project = facts["project"]

    if not project:
        results.append(_unavailable(account_id, account_deployments,
                                    facts["source"]))
        return results
    try:
        changes = approved_changes(project)
    except jira.JiraUnavailable as exc:
        results.append(_unavailable(account_id, account_deployments,
                                    str(exc)))
        return results
    # Records in AltDigital's ops project are infrastructure changes, and
    # is_approved() holds them to the infrastructure approver rule. Only
    # when the ops project is genuinely configured: the fallback to the
    # tenant's project is already flagged as wrong for a platform
    # deployment, and judging a tenant's records by AltDigital's approver
    # list would compound it.
    if deployer == "platform" and OPS_CHANGE_PROJECT:
        changes = [dict(c, layer="infrastructure") for c in changes]
    elif deployer == "tenant" and facts.get("tenant"):
        # A tenant's application changes, held to that tenant's own approver
        # list where one is published (scripts/sync-application-approvers.sh).
        changes = [dict(c, layer="application", tenant=facts["tenant"])
                   for c in changes]

    matched_keys = set()
    for deployment in account_deployments:
        change, problem = match(deployment, changes)
        if change is None:
            outcome = "no_change_record"
        elif problem:
            outcome = "outside_approved_window"
            matched_keys.add(change["key"])
        else:
            outcome = "matched"
            matched_keys.add(change["key"])
        results.append(_finding(account_id, outcome, deployment, change,
                                problem, project, facts))

    # design 08's fourth outcome: "Change record with no corresponding
    # deployment -- Review: stale or abandoned."
    for change in changes:
        # Only an APPROVED record whose window has CLOSED is "stale or
        # abandoned". Two ways to get this wrong, and the first version
        # had both:
        #
        #   a record still awaiting approval is in flight, not abandoned
        #   a record approved ten minutes ago has not been abandoned either
        #
        # The second one fired on AMI-4 the moment it was approved, while
        # the work it authorises was actively being done. Left in, this
        # outcome would raise a finding against every change record in the
        # gap between approval and implementation -- which is every change
        # record, for as long as the change takes. A finding that is always
        # true is noise, and noise is what gets a control muted.
        if change["key"] in matched_keys:
            continue
        if not is_approved(change)[0]:
            continue
        created = _parse(change.get("created"))
        if created is None:
            continue
        closes = created + datetime.timedelta(hours=MATCH_WINDOW_HOURS)
        if _now() < closes:
            # Still in its window. Nothing to conclude yet.
            continue
        if change["accounts"] and account_id not in change["accounts"]:
            continue
        results.append(_finding(account_id, "change_without_deployment",
                                None, change, None, project, facts))
    return results


def reconcile(event, context=None):
    """The scheduled run. Produces design 08's outcomes, plus the fifth."""
    # Cleared per run. A warm Lambda container lives for hours, and an approval
    # granted between two runs has to be visible to the second one -- a cache
    # that outlived the invocation would keep reporting a since-approved change
    # as unapproved until the container happened to recycle. Same shape as the
    # cached auth header in jira.py that cost a diagnostic cycle on 2026-09-19.
    _APPROVAL_CACHE.clear()
    # The approver lists likewise: someone added to or removed from a list
    # between runs must count, or stop counting, on the next run.
    _INFRA_APPROVERS.clear()
    _APP_APPROVERS.clear()
    since = _now() - datetime.timedelta(hours=LOOKBACK_HOURS)
    deployments = recent_deployments(since)

    by_account = {}
    for d in deployments:
        by_account.setdefault(d.get("account_id"), []).append(d)

    results = []
    # Split by WHOSE change it is before choosing a change source. A platform
    # deployment and a tenant deployment in the same account are answerable to
    # different records and different approvers.
    regrouped = {}
    for account_id, account_deployments in by_account.items():
        for d in account_deployments:
            regrouped.setdefault(
                (account_id, d.get("deployer") or "unknown"), []).append(d)

    for (account_id, deployer), account_deployments in regrouped.items():
        facts = tenant_facts(account_id)
        # Tenant deployments are reconciled on the tenant's own Jira site
        # when it has one. Platform deployments always use AltDigital's.
        site = facts.get("jira_site") if deployer != "platform" else None
        try:
            with jira.using(site, facts.get("jira_secret")):
                results.extend(_reconcile_account(
                    account_id, deployer, account_deployments))
        except jira.JiraUnavailable as exc:
            results.append(_unavailable(account_id, account_deployments,
                                        str(exc)))

    emitted = [r for r in results if emit_finding(r)]
    out = {
        "record_type": "change-reconciliation",
        "reconciled_at": _iso(_now()),
        "jira_token_days_remaining": publish_credential_health(),
        "window_hours": LOOKBACK_HOURS,
        "deployments": len(deployments),
        "accounts": len(by_account),
        "outcomes": {k: sum(1 for r in results if r["outcome"] == k)
                     for k in sorted({r["outcome"] for r in results})},
        "emitted": len(emitted),
    }
    print(json.dumps(out, default=str))
    return out


def recent_deployments(since):
    """Every captured deployment in the window, across every account.

    Read from the by-record-type index rather than by querying a known list of
    accounts, and the difference matters. Deriving the account list from the
    tenant facts map -- the first version of this -- meant an account absent
    from that map had its deployments captured and then never reconciled: the
    evidence accumulated, nothing compared it, and the account read as clean
    rather than as unexamined.

    Reconciliation now covers everything that was captured. An account with no
    tenant record still gets reconciled, against the platform default, with
    that fact recorded in the finding.
    """
    if not CHANGE_TABLE:
        return []
    found = []
    token = None
    while True:
        kwargs = {
            "TableName": CHANGE_TABLE,
            "IndexName": "by-record-type",
            "KeyConditionExpression": "record_type = :t AND occurred_at >= :s",
            "ExpressionAttributeValues": {
                ":t": {"S": "deployment-event"},
                ":s": {"S": _iso(since)},
            },
        }
        if token:
            kwargs["ExclusiveStartKey"] = token
        page = dynamodb.query(**kwargs)
        for item in page.get("Items", []):
            found.append(json.loads(item["detail"]["S"]))
        token = page.get("LastEvaluatedKey")
        if not token:
            return found


def _finding(account_id, outcome, deployment, change, problem, project, facts):
    return {
        "outcome": outcome,
        "account_id": account_id,
        "deployment": deployment,
        "change": change,
        "problem": problem,
        "jira_project": project,
        "project_source": facts["source"],
        "change_approver": facts.get("change_approver"),
    }


def _unavailable(account_id, deployments, reason):
    return {
        "outcome": "change_source_unavailable",
        "account_id": account_id,
        "deployment": None,
        "deployments_unreconciled": len(deployments),
        "change": None,
        "problem": reason,
        "jira_project": None,
        "project_source": reason,
        "change_approver": None,
    }


# ---------------------------------------------------------------------------
# Routing. design 08's table, through the one routing layer.
# ---------------------------------------------------------------------------
#
#   Deployment with a matching approved change | Evidence record, no action
#   Deployment with no change record           | Low-severity page + Jira ticket
#   Deployment outside an approved window      | Finding, owned and dated
#   Change record with no corresponding deploy | Review -- stale or abandoned
#
# `no_change_record` declares NOTHING, so the router applies the account's tier
# default from design/06a. In prod that pages, which is design 08's
# "low-severity page"; in dev it does not, which is the tier table's business
# and not this module's. Declaring `pagerduty` here would be B-041's shape --
# a component deciding for itself who gets woken.
DECLARED_ROUTING = {
    "matched": "digest",
    "no_change_record": None,
    "outside_approved_window": "digest_and_jira",
    "change_without_deployment": "digest_and_jira",
    # A platform fault, not a tenant one. Dated work for AltDigital.
    "change_source_unavailable": "digest_and_jira",
}


def emit_finding(result):
    """One dossier per finding, except the ones that are only evidence.

    A matched deployment is recorded and not emitted. There will be far more of
    those than anything else, and a digest entry per successful deployment
    would bury the three outcomes that need a human in a list of the one that
    does not.
    """
    outcome = result["outcome"]
    if outcome == "matched":
        return False

    deployment = result.get("deployment") or {}
    change = result.get("change") or {}
    summaries = {
        "no_change_record": (
            {"tenant": "Tenant deployment with NO change record. ",
             "platform": "PLATFORM deployment with no change record in "
                         "AltDigital's own operations project. ",
             "unknown": "Deployment by an UNRECOGNISED principal, and with no "
                        "change record. This came through neither the "
                        "AppDeployer control point nor any platform role -- "
                        "somebody deployed with credentials nobody has "
                        "accounted for, which matters more than the missing "
                        "paperwork. "}.get(deployment.get("deployer"),
                                           "Deployment with NO change record. ")
            + f"{deployment.get('kind')} on {deployment.get('resource')} at "
            f"{deployment.get('occurred_at')}, by "
            f"{deployment.get('principal_arn')}"),
        "outside_approved_window": (
            f"Deployment outside an approved window. {result.get('problem')}"),
        "change_without_deployment": (
            f"Change record {change.get('key')} has no corresponding "
            "deployment -- stale or abandoned."),
        "change_source_unavailable": (
            "CHANGE SOURCE UNAVAILABLE -- no conclusion can be drawn about "
            f"{result.get('deployments_unreconciled', 0)} deployment(s) in "
            f"account {result.get('account_id')}. {result.get('problem')}"),
    }

    dossier = {
        "record_type": "change-reconciliation-finding",
        "recorded_at": _iso(_now()),
        "current_state": "ALARM",
        "routing": DECLARED_ROUTING.get(outcome),
        "alarm": {
            "name": f"platform-change-{outcome.replace('_', '-')}",
            "resource_id": deployment.get("resource") or change.get("key")
                           or result.get("account_id") or "unknown",
            "account_id": result.get("account_id"),
        },
        "assessment": {"probable_cause": summaries.get(outcome, outcome)},
        # The ticket goes to the tenant's change approver. Resolving WHO that
        # is happens at ticket creation, not here: the Jira intents queue has
        # no consumer until prompt 12.3, and resolving an approver now would be
        # building for a consumer that does not exist. The account id travels
        # so the consumer can resolve it. B-039.
        "change_reconciliation": result,
    }
    events.put_events(Entries=[{
        "Source": "platform.change",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "EventBusName": ALERT_BUS_NAME,
    }])
    return True


# ---------------------------------------------------------------------------
# The commercial one. Prompt 8.2 calls it first-class; it is.
# ---------------------------------------------------------------------------


def customer_caused(event, context=None):
    """Deployments in an account shortly before a given moment.

    Prompt 8.2: "Make the customer-caused-outage correlation query a
    first-class function, because it is what we rely on commercially."

    This is the SLA exclusion determination in design 10: if an outage began
    twenty minutes after the tenant deployed, the tenant's change is the first
    thing to look at, and the platform should be able to say so from evidence
    rather than from memory.

    Deliberately NOT a verdict. It returns what was deployed and when, and the
    word "caused" appears nowhere in the output -- correlation is not
    causation, an SLA exclusion is a commercial conversation, and an automated
    system asserting fault would be both wrong sometimes and unhelpful always.
    The function's value is that the conversation starts from a list of facts.

    Needs NO Jira. It reads captured deployment events only, so it works
    whether or not the tenant files change records at all.
    """
    account_id = event.get("account_id")
    at = _parse(event.get("at")) or _now()
    window = float(event.get("window_hours") or 4)
    if not account_id:
        raise ValueError("customer_caused requires an account_id")

    since = at - datetime.timedelta(hours=window)
    got = dynamodb.query(
        TableName=CHANGE_TABLE,
        KeyConditionExpression="PK = :p AND SK BETWEEN :s AND :e",
        ExpressionAttributeValues={
            ":p": {"S": f"deployment#{account_id}"},
            ":s": {"S": _iso(since)},
            ":e": {"S": _iso(at)},
        },
    ).get("Items", [])

    deployments = [json.loads(i["detail"]["S"]) for i in got]
    deployments.sort(key=lambda d: str(d.get("occurred_at")))

    out = {
        "record_type": "deployment-correlation",
        "account_id": account_id,
        "incident_at": _iso(at),
        "window_hours": window,
        "window_start": _iso(since),
        "deployment_count": len(deployments),
        "deployments": deployments,
        "nearest": None,
        "finding": None,
    }
    if deployments:
        last = deployments[-1]
        when = _parse(last.get("occurred_at"))
        out["nearest"] = {
            "kind": last.get("kind"),
            "resource": last.get("resource"),
            "occurred_at": last.get("occurred_at"),
            "via_appdeployer": last.get("via_appdeployer"),
            "minutes_before_incident": (round((at - when).total_seconds() / 60, 1)
                                        if when else None),
        }
        out["finding"] = (
            f"{len(deployments)} tenant deployment(s) in the {window}h before "
            f"this incident; the most recent was "
            f"{out['nearest']['minutes_before_incident']} minutes before. "
            "This is CORRELATION, not a determination of cause.")
    else:
        out["finding"] = (
            f"No tenant deployments in the {window}h before this incident. "
            "An SLA exclusion on change grounds is not supported by the "
            "deployment record.")
    print(json.dumps(out, default=str))
    return out


# ---------------------------------------------------------------------------


def health(event=None, context=None):
    """Can the platform read Jira, as whom, and is the scheme what we expect?

    Invoked by scripts/jira-check.sh. Runs IN the Lambda rather than from a
    laptop deliberately: the credential never leaves AWS, and the path being
    tested is the real one -- execution role to Secrets Manager to Jira -- not
    a similar path from somewhere else.

    Checks the ISSUE TYPE as well as the connection, because a working
    credential pointed at a project with no matching issue type is the failure
    that would otherwise present as an estate-wide compliance finding.
    """
    ok, detail = jira.health()
    out = {"record_type": "jira-health", "checked_at": _iso(_now()),
           "connected": ok, "detail": detail,
           "token_expires": TOKEN_EXPIRES or "unknown",
           "token_days_remaining": token_days_remaining(),
           "expected_issue_type": CHANGE_ISSUETYPE,
           "default_change_project": DEFAULT_CHANGE_PROJECT}
    if ok and DEFAULT_CHANGE_PROJECT:
        try:
            types = jira.project_issue_types(DEFAULT_CHANGE_PROJECT)
            out["project_issue_types"] = types
            out["issue_type_present"] = CHANGE_ISSUETYPE in types
            if not out["issue_type_present"]:
                out["warning"] = (
                    f"project {DEFAULT_CHANGE_PROJECT} has no issue type "
                    f"'{CHANGE_ISSUETYPE}'. Reconciliation will report the "
                    "change source as UNAVAILABLE rather than reporting "
                    "deployments as unapproved -- see open-items.md B9.")
        except jira.JiraUnavailable as exc:
            out["project_error"] = str(exc)

        # JSM request types, resolved to the issue type behind each one.
        #
        # The portal shows a request type -- "Request a change" -- and the
        # issue underneath carries an issue type like "[System] Change".
        # Reconciliation searches by ISSUE TYPE in JQL, so that is what the
        # configuration needs, and finding it in the Jira UI is unreasonably
        # hard. Asking the API is not.
        try:
            id_to_name = jira.project_issue_type_map(DEFAULT_CHANGE_PROJECT)
            desks = jira.service_desks()
            for desk in desks:
                if desk.get("project_key") != DEFAULT_CHANGE_PROJECT:
                    continue
                out["request_types"] = [
                    {"request_type": rt["name"],
                     "backed_by_issue_type": id_to_name.get(rt["issue_type_id"],
                                                            "unknown")}
                    for rt in jira.request_types(desk["id"])
                ]
                # The one the operator is looking for, named outright.
                changeish = [r for r in out["request_types"]
                             if "change" in (r["request_type"] or "").lower()
                             or "change" in (r["backed_by_issue_type"] or "").lower()]
                if changeish:
                    out["change_candidates"] = changeish
                    out["suggested_issue_type"] = changeish[0][
                        "backed_by_issue_type"]
        except jira.JiraUnavailable as exc:
            out["request_type_error"] = str(exc)

    print(json.dumps(out, default=str))
    return out


def change_fields(event=None, context=None):
    """What a change record in this project actually requires.

    Read-only discovery. Answers "what do I need from Jamie" with data rather
    than with a guess about how his scheme is configured.
    """
    project = (event or {}).get("project") or OPS_CHANGE_PROJECT         or DEFAULT_CHANGE_PROJECT
    id_to_name = jira.project_issue_type_map(project)
    wanted = {v: k for k, v in id_to_name.items()}.get(CHANGE_ISSUETYPE)
    if not wanted:
        return {"error": f"project {project} has no issue type "
                         f"'{CHANGE_ISSUETYPE}'",
                "available": sorted(id_to_name.values())}
    fields = jira.field_requirements(project, wanted)
    out = {"project": project, "issue_type": CHANGE_ISSUETYPE,
           "required": [f for f in fields if f["required"]],
           "optional": [f for f in fields if not f["required"]],
           "choices": {f["name"]: f["allowed_values"] for f in fields
                       if f.get("allowed_values")}}
    print(json.dumps(out, default=str))
    return out


def find_field(event=None, context=None):
    """Where does a named field live, and is it on the create screen?

    A field absent from `change_fields` has two very different causes and the
    fix is a different configuration page for each:

        not on the site at all   -> the name is wrong, or it was never created
        on the site, not on the  -> it exists but the change record's CREATE
        create screen               screen does not carry it, so nothing can
                                    set it at creation time

    createmeta cannot tell these apart -- both are simply absent from it.
    """
    project = (event or {}).get("project") or OPS_CHANGE_PROJECT         or DEFAULT_CHANGE_PROJECT
    wanted = (event or {}).get("name") or ""
    on_site = jira.site_fields(wanted)
    creatable = {}
    id_to_name = jira.project_issue_type_map(project)
    type_id = {v: k for k, v in id_to_name.items()}.get(CHANGE_ISSUETYPE)
    if type_id:
        creatable = {f["id"]: f for f in jira.field_requirements(project,
                                                                 type_id)}
    out = {"searched_for": wanted, "project": project,
           "issue_type": CHANGE_ISSUETYPE,
           "matches": [{**f, "on_create_screen": f["id"] in creatable}
                       for f in on_site]}
    print(json.dumps(out, default=str))
    return out


def approval_shape(event=None, context=None):
    """What does an approval on this site actually look like?

    Pure discovery, and it exists because the quorum verifier (8.3) is about to
    depend on the shape of this response. Every field it reads -- the decider's
    accountId, the decision, the timestamp -- has to be confirmed against one
    real approval before any of it is trusted. B-068.
    """
    key = (event or {}).get("issue")
    if not key:
        return {"error": "pass an issue key"}
    try:
        raw = jira.approvals(key)
    except jira.JiraUnavailable as exc:
        return {"issue": key, "readable": False, "error": str(exc)}
    out = {"issue": key, "readable": True, "count": len(raw), "raw": raw}
    print(json.dumps(out, default=str))
    return out


def request_type_map(event=None, context=None):
    """Service desks and their request types, with the issue type behind each.

    Needed because a JSM issue created through the plain REST API is not a
    "request": it has no Request Type (vp-origin), and every servicedeskapi
    endpoint -- including approvals -- returns 404 for it. The request type id
    is what turns a Change issue into a Change REQUEST.
    """
    want = (event or {}).get("project") or OPS_CHANGE_PROJECT         or DEFAULT_CHANGE_PROJECT
    out = {"project": want, "desks": []}
    for desk in jira.service_desks():
        if want and desk.get("project_key") != want:
            continue
        types = []
        for rt in jira.request_types(desk["id"]):
            types.append(rt)
        out["desks"].append({**desk, "request_types": types})
    print(json.dumps(out, default=str))
    return out


def request_form(event=None, context=None):
    """What the change request FORM accepts. Discovery for the filer."""
    project = (event or {}).get("project") or OPS_CHANGE_PROJECT         or DEFAULT_CHANGE_PROJECT
    want_name = (event or {}).get("request_type") or "Request a change"
    for desk in jira.service_desks():
        if desk.get("project_key") != project:
            continue
        for rt in jira.request_types(desk["id"]):
            if (rt.get("name") or "").lower() == want_name.lower():
                out = {"project": project, "service_desk_id": desk["id"],
                       "request_type_id": rt["id"],
                       "request_type_name": rt["name"],
                       "issue_type_id": rt.get("issue_type_id")}
                out.update(jira.request_type_fields(desk["id"], rt["id"]))
                print(json.dumps(out, default=str))
                return out
    return {"error": f"no request type named '{want_name}' in {project}"}


def issue_state(event=None, context=None):
    """Status plus approvals for one issue, which is how to read 'count: 0'."""
    key = (event or {}).get("issue")
    if not key:
        return {"error": "pass an issue key"}
    out = jira.issue_state(key)
    try:
        out["approvals"] = jira.approvals(key)
    except jira.JiraUnavailable as exc:
        out["approvals_error"] = str(exc)
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"capture": capture, "reconcile": reconcile,
            "customer_caused": customer_caused, "health": health,
            "change_fields": change_fields, "find_field": find_field,
            "approval_shape": approval_shape,
            "request_type_map": request_type_map,
            "request_form": request_form,
            "issue_state": issue_state}


def handler(event, context):
    # Two invocation shapes: an EventBridge event (capture) and a scheduled or
    # explicit call carrying `action`. An EventBridge deployment event has no
    # `action`, so the default is capture.
    action = event.get("action")
    if action is None and "detail" in event:
        action = "capture"
    fn = HANDLERS.get(action or "reconcile")
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(sorted(HANDLERS))}")
    return fn(event, context)
