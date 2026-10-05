"""Daily: which HIPAA-scoped accounts used, or hold, a non-eligible service?

docs/hipaa-eligibility.md §4.2 and §4.4. B-161.

Two sweeps, each with a different blind spot, which is why there are two:

  usage      IAM service-last-accessed for every role in each account, read
             through PlatformHipaaEligibilityReader. It reports IAM service
             namespaces, the same vocabulary the SCP is written in, so there is
             no mapping table between "what was used" and "what is allowed" to
             drift. Sees reads as well as writes. Cannot see resources that
             already exist and are not being called.

  inventory  The organization Config aggregator in this account. Sees resources
             that exist, including ones created before the sweep did, and their
             DataClassification tag. Cannot see services Config does not record.

Scope: the accounts in /platform/eligibility/hipaa-accounts, published from the
registry by scripts/sync-hipaa-accounts.sh. Nothing else is swept. Tenants
without HIPAA exposure are never read and never reported on.

Severities (the router decides destinations — §4.4; this emitter never names
one except to downgrade):

  not-allowlisted prefix used, account not enforced (dev/test)      MEDIUM
  not-allowlisted prefix used, account enforced (SCP attached)      LOW, digest
      With PlatformHipaaEligibleOnly attached and no principal exempt from
      its Statement A, such a call cannot have succeeded. It is a denied
      attempt, and the attachment check, not this sweep, owns the question of
      whether the SCP is really there.
  pending prefix used                                               LOW
  resource of a non-allowlisted or pending type                     MEDIUM; HIGH if enforced
  DataClassification=regulated on such a resource                   CRITICAL
  resource type or prefix the catalog does not mention              LOW, one roll-up

Repetition: each (account, subject) is reported when first seen, then at most
once per REMIND_DAYS while it persists (state.py).
Nothing is remediated. The sweep reads and reports.
"""

import datetime
import hashlib
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

import catalog
import state

CATALOG_PATH = os.environ.get("CATALOG_PATH", "hipaa-eligible-services.tsv")
ACCOUNTS_PARAM = os.environ.get("ACCOUNTS_PARAM", "/platform/eligibility/hipaa-accounts")
READ_ROLE = os.environ.get("READ_ROLE", "PlatformHipaaEligibilityReader")
AGGREGATOR = os.environ.get("AGGREGATOR", "platform-organization")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
STATE_TABLE = os.environ.get("STATE_TABLE", "")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
REMIND_DAYS = int(os.environ.get("REMIND_DAYS", "7"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Platform/HipaaEligibility")
SELF_ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "")
# Named platform principals allowed the platform-only prefixes (Statement B).
# Empty today: no prefix is platform-only until E1 is decided.
PLATFORM_PRINCIPALS = {p for p in os.environ.get("PLATFORM_PRINCIPALS", "").split(",") if p}
# Roles whose service-last-accessed record is enumeration, not use.
#
# The Config recorder (baseline/10-config-recorder.yaml) calls a describe on
# every resource type Config supports, so its last-accessed list names App
# Runner, Lightsail, CodeArtifact and dozens more in an account where nobody has
# touched them. Proved on the first live sweep, 2026-10-04: every non-eligible
# usage hit in Arc8 dev came from this one role. What Config enumerates is
# exactly what the inventory sweep reads, so excluding it here loses nothing.
# Named exactly, never by pattern.
USAGE_EXCLUDED_ROLES = {p for p in os.environ.get(
    "USAGE_EXCLUDED_ROLES", "PlatformConfigRecorderRole").split(",") if p}

ssm = boto3.client("ssm")
sts = boto3.client("sts")
config = boto3.client("config")
events = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")

DENIED_TYPES = {"not-allowlisted", "pending"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def hipaa_accounts():
    raw = ssm.get_parameter(Name=ACCOUNTS_PARAM)["Parameter"]["Value"]
    return json.loads(raw)


def member_iam(account_id):
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{READ_ROLE}",
        RoleSessionName="hipaa-eligibility-sweep")["Credentials"]
    return boto3.client(
        "iam", aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


def roles(iam):
    out = []
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            # Service-linked roles act for AWS, and SCPs do not apply to them.
            # Reporting their use would be reporting AWS operating its own
            # service, which nobody can act on.
            if r.get("Path", "/").startswith("/aws-service-role/"):
                continue
            if r["RoleName"] in USAGE_EXCLUDED_ROLES:
                continue
            out.append(r)
    return out


def last_accessed(iam, role_arns, poll_seconds=2, max_wait=120):
    """{role_arn: [ServiceLastAccessed]} — jobs started together, then polled."""
    jobs = {}
    for arn in role_arns:
        jobs[arn] = iam.generate_service_last_accessed_details(
            Arn=arn, Granularity="SERVICE_LEVEL")["JobId"]
    results, deadline = {}, time.monotonic() + max_wait
    while jobs and time.monotonic() < deadline:
        for arn, job in list(jobs.items()):
            resp = iam.get_service_last_accessed_details(JobId=job)
            if resp["JobStatus"] == "IN_PROGRESS":
                continue
            if resp["JobStatus"] == "FAILED":
                results[arn] = None
            else:
                svc, marker = list(resp.get("ServicesLastAccessed", [])), resp.get("Marker")
                while resp.get("IsTruncated"):
                    resp = iam.get_service_last_accessed_details(JobId=job, Marker=marker)
                    svc += resp.get("ServicesLastAccessed", [])
                    marker = resp.get("Marker")
                results[arn] = svc
            del jobs[arn]
        if jobs:
            time.sleep(poll_seconds)
    for arn in jobs:
        # Unfinished is not "used nothing". Reported as unreadable below.
        results[arn] = None
    return results


def aggregator_resources(account_id):
    expr = ("SELECT accountId, awsRegion, resourceType, resourceId, arn, tags "
            f"WHERE accountId = '{account_id}'")
    token = None
    while True:
        kw = {"ConfigurationAggregatorName": AGGREGATOR, "Expression": expr, "Limit": 100}
        if token:
            kw["NextToken"] = token
        resp = config.select_aggregate_resource_config(**kw)
        for item in resp.get("Results", []):
            yield json.loads(item)
        token = resp.get("NextToken")
        if not token:
            return


# ---------------------------------------------------------------------------
# Deciding — pure
# ---------------------------------------------------------------------------

def classify_usage(rows, account, role_name, namespace):
    """(severity, status, routing) for one role's use of one namespace, or None."""
    status, row = catalog.classify_prefix(rows, namespace)
    if status in catalog.ALLOWLISTED:
        return None
    if status in catalog.PLATFORM_ONLY:
        return None if role_name in PLATFORM_PRINCIPALS else ("MEDIUM", status, None)
    if status == "unlisted":
        return ("LOW", "unlisted", "digest")
    if status == "pending":
        return ("LOW", status, "digest")
    # not-allowlisted
    if account.get("enforced"):
        return ("LOW", status, "digest")
    return ("MEDIUM", status, None)


def classify_resource(rows, account, resource):
    status, row = catalog.classify_config_type(rows, resource["resourceType"])
    if status in catalog.ALLOWLISTED or status in catalog.PLATFORM_ONLY:
        return None
    if status == "unlisted":
        return ("LOW", "unlisted", "digest")
    tags = {t.get("key"): t.get("value") for t in (resource.get("tags") or [])}
    if (tags.get("DataClassification") or "").lower() == "regulated":
        return ("CRITICAL", status, None)
    return ("HIGH" if account.get("enforced") else "MEDIUM", status, None)


def message(row, status, subject, account, verb):
    env = account.get("environment", "?")
    alias = account.get("alias") or "the account"
    if status == "pending":
        return (f"{subject} {verb} in {alias} ({env}). Its HIPAA eligibility is unconfirmed, so "
                "it is treated as not eligible until reviewed: denied in uat and prod.")
    if status == "unlisted":
        return (f"{subject} {verb} in {alias} ({env}) is not in the HIPAA eligibility catalog at "
                "all. The catalog is incomplete; AltDigital will classify it.")
    alt = (row or {}).get("alternative") or ""
    future = ("is denied here by the HIPAA eligibility policy" if account.get("enforced")
              else "will be denied in uat and prod")
    return (f"{subject} {verb} in {alias} ({env}). Not HIPAA-eligible — this {future}."
            + (f" Use instead: {alt}." if alt else "")
            + " Needed for something that never touches PHI? Request an exception "
              "(developer guide §4.16).")


def emit(detail):
    events.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME, "Source": "platform.eligibility",
        "DetailType": "hipaa-eligibility", "Detail": json.dumps(detail, default=str)}])


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def sweep_account(rows, account_id, account, now):
    found, unreadable = [], []
    cutoff = now - datetime.timedelta(days=LOOKBACK_DAYS)

    # usage
    try:
        iam = member_iam(account_id)
        rs = roles(iam)
        results = last_accessed(iam, [r["Arn"] for r in rs])
    except ClientError as exc:
        unreadable.append(f"usage: {exc.response['Error']['Code']}")
        results, rs = {}, []
    names = {r["Arn"]: r["RoleName"] for r in rs}
    used = {}
    for arn, services in results.items():
        if services is None:
            unreadable.append(f"usage: last-accessed job did not complete for {names.get(arn)}")
            continue
        for s in services:
            when = s.get("LastAuthenticated")
            if not when or when < cutoff:
                continue
            ns = s["ServiceNamespace"]
            verdict = classify_usage(rows, account, names[arn], ns)
            if verdict:
                u = used.setdefault(ns, {"verdict": verdict, "roles": set(), "last": when})
                u["roles"].add(names[arn])
                u["last"] = max(u["last"], when)
    for ns, u in used.items():
        sev, status, routing = u["verdict"]
        _, row = catalog.classify_prefix(rows, ns)
        found.append({
            "check": "usage", "subject": ns, "severity": sev, "status": status,
            "routing": routing,
            "summary": message(row, status, f"`{ns}`", account,
                               "attempted" if account.get("enforced") else "used"),
            "alternative": (row or {}).get("alternative") or None,
            # Role names, never session names: an Identity Center session name
            # is a username. docs/hipaa-eligibility.md §6.
            "facts": {"roles": sorted(u["roles"]), "last_used": u["last"]}})

    # inventory
    try:
        for res in aggregator_resources(account_id):
            verdict = classify_resource(rows, account, res)
            if not verdict:
                continue
            sev, status, routing = verdict
            _, row = catalog.classify_config_type(rows, res["resourceType"])
            subject = f"{res['resourceType']}/{res['resourceId']}"
            found.append({
                "check": "inventory", "subject": subject, "severity": sev, "status": status,
                "routing": routing,
                "summary": message(row, status, f"{res['resourceType']} `{res['resourceId']}`",
                                   account, "exists"),
                "alternative": (row or {}).get("alternative") or None,
                "facts": {"region": res.get("awsRegion"),
                          # The digest, for evidence (§6). The id itself travels
                          # to the ticket because a developer cannot act on a hash.
                          "arn_sha256": hashlib.sha256((res.get("arn") or subject).encode()).hexdigest(),
                          "regulated": sev == "CRITICAL"}})
    except ClientError as exc:
        unreadable.append(f"inventory: {exc.response['Error']['Code']}")
    return found, unreadable


def handler(event, context):
    now = _now()
    version, rows = catalog.load(CATALOG_PATH)
    accounts = hipaa_accounts()
    emitted, suppressed, blind = 0, 0, {}
    unlisted = {}
    for account_id, account in sorted(accounts.items()):
        found, unreadable = sweep_account(rows, account_id, account, now)
        if unreadable:
            blind[account_id] = unreadable
        for f in found:
            if f["status"] == "unlisted":
                # One roll-up below, addressed to the catalog's owner rather than
                # to the tenant: an unlisted namespace is a gap in OUR file.
                unlisted.setdefault(f["subject"].split("/")[0], set()).add(account_id)
                continue
            if not state.should_emit(STATE_TABLE, account_id, f["subject"], now, REMIND_DAYS):
                suppressed += 1
                continue
            emit(dict(f, framework="hipaa", account_id=account_id,
                      environment=account.get("environment"),
                      enforced=bool(account.get("enforced")),
                      list_version=version, detected_at=now.isoformat()))
            emitted += 1

    if unlisted and state.should_emit(STATE_TABLE, SELF_ACCOUNT or "audit", "catalog:unlisted:"
                                      + ",".join(sorted(unlisted)), now, REMIND_DAYS):
        emit({"framework": "hipaa", "check": "catalog", "account_id": SELF_ACCOUNT or None,
              "subject": "unlisted services", "severity": "LOW", "routing": "digest_and_jira",
              "summary": ("HIPAA-scoped accounts used or hold services the eligibility catalog "
                          "does not mention: " + ", ".join(sorted(unlisted))
                          + ". Add a row for each to eligibility/hipaa-eligible-services.tsv."),
              "facts": {k: sorted(v) for k, v in unlisted.items()},
              "list_version": version, "detected_at": now.isoformat()})
        emitted += 1

    if blind:
        # The else. A sweep that could not read an account has not found it
        # clean, and must not look as though it has.
        emit({"framework": "hipaa", "check": "sweep", "account_id": SELF_ACCOUNT or None,
              "subject": "sweep incomplete", "severity": "HIGH", "routing": "digest_and_jira",
              "summary": "The HIPAA eligibility sweep could not read every account: "
                         + "; ".join(f"{a}: {', '.join(v)}" for a, v in sorted(blind.items())),
              "facts": blind, "list_version": version, "detected_at": now.isoformat()})
        emitted += 1
    else:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[{
            "MetricName": "SweepCompleted", "Value": 1, "Unit": "Count"}])

    result = {"list_version": version, "accounts": len(accounts), "emitted": emitted,
              "suppressed_repeats": suppressed, "blind": sorted(blind)}
    print(json.dumps(result))
    return result
