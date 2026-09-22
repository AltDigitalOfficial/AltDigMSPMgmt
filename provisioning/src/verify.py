"""The verification sweep. Prompt 12.5, design 13 "The verification sweep".

design 13: "This is what makes provisioning a control rather than a script."

The difference between a control and a script
-----------------------------------------------
A script asserts that configuration exists. A control exercises the thing and
observes the result. The two are easy to confuse because they pass at the same
time and fail at different times -- the script keeps passing after the thing
has stopped working, which is the only moment either of them mattered.

So every check here is written to exercise, and where it cannot yet exercise,
it says so and BLOCKS. It does not assert configuration as a substitute. An
alarm configuration that exists is not an alarm that reached PagerDuty, and
B-103 in this codebase is exactly that error: an agent installed, never
configured, and a check that would have passed on its presence.

Nine checks, not eight
------------------------
prompt 12.5 lists eight. design 13 lists nine, and its version of the BAA check
is materially stronger than the prompt's summary of it:

    prompt      "BAA notification window is set and non-null"
    design 13   "BAA notification windows set and non-null at BOTH levels --
                 AltDigital->partner and partner->client -- and AltDigital's
                 is tighter"

The design is followed. A single non-null window would pass the prompt's
version and still leave the chain incoherent: if AltDigital's deadline to tell
the partner is not shorter than the partner's deadline to tell the client, the
partner is obliged to notify before it has been notified. The ninth check, the
HOOP turndown/turnup cycle, is absent from the prompt entirely.

Fail closed, and "not built" is a failure
-------------------------------------------
design 13: "Any failure blocks go-live rather than raising a ticket for later."

Three verdicts -- `pass`, `fail`, `undeterminable` -- and only `pass` clears.
`undeterminable` covers the check that could not run at all, including because
the system it exercises does not exist yet. That is deliberately NOT a skip: a
sweep that skipped its unbuilt checks and reported success would certify a
tenant on the strength of the checks that happened to be implemented, which is
how a control becomes a formality.

Most checks are undeterminable today. That is the honest state of the platform
and it is visible in the output rather than hidden by it.
"""
import datetime
import json
import os

import boto3

import saga

dynamodb = boto3.client("dynamodb")

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")

PASS = "pass"
FAIL = "fail"
UNKNOWN = "undeterminable"


class VerificationFailed(Exception):
    """The sweep ran and did not clear. Carries the summary, not just a code."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _s(item, key):
    """Read a field from an UNWRAPPED registry record.

    Everything here works on `saga.record`-shaped dicts -- plain values, not
    DynamoDB attribute maps. Mixing the two shapes is how the first version of
    this module read every field as None while raising nothing, which would
    have made every check fail for a reason that had nothing to do with the
    thing it was checking.
    """
    return (item or {}).get(key)


def _partner_record(slug):
    """The partner record, unwrapped to match `saga.record`'s shape."""
    if not slug:
        return None
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"PARTNER#{slug}"}, "SK": {"S": "META"}})
    item = got.get("Item")
    if not item:
        return None
    return {k: (v.get("S") or v.get("N") or v.get("BOOL"))
            for k, v in item.items()}


def _result(verdict, examined, why):
    """Every check reports what it looked at, not only what it decided.

    A verdict with no `examined` cannot be told apart from a verdict reached
    without looking, which is the failure this codebase keeps rediscovering
    (B-086, B-092, B-100, B-103).
    """
    return {"verdict": verdict, "examined": examined, "why": why}


def _not_built(what, tracked_as, examined=None):
    return _result(
        UNKNOWN, examined or {},
        f"{what} is not built ({tracked_as}). This blocks go-live rather than "
        "being skipped: a sweep that passed on its implemented checks alone "
        "would certify a tenant on the strength of what happens to exist.")


# ---------------------------------------------------------------------------
# 1. Truveon received evidence from all three accounts, sequence clean
# ---------------------------------------------------------------------------


def check_evidence(ctx):
    rec = ctx["record"]
    if not _s(rec, "truveon_tenant_id"):
        return _not_built(
            "The Truveon tenant API", "B-088",
            {"truveon_tenant_id": None, "saga_step": 2})
    return _not_built(
        "The Truveon evidence-receipt confirmation endpoint", "B-088",
        {"truveon_tenant_id": _s(rec, "truveon_tenant_id")})


# ---------------------------------------------------------------------------
# 2. A synthetic test alarm reached PagerDuty and paged the correct rotation
# ---------------------------------------------------------------------------


def check_paging(ctx):
    rec = ctx["record"]
    # The check design 13 asks for is to SEND one and verify receipt. Asserting
    # that a service exists would be the script version of this check, and
    # would have passed throughout B-096 -- a containment role that could not
    # invoke the function it needed, where every alarm configuration was
    # present and correct.
    return _not_built(
        "Per-tenant PagerDuty provisioning, and with it the ability to send a "
        "synthetic alarm to a tenant's own routing key and confirm which "
        "rotation it woke", "prompt 12.2",
        {"pagerduty_service_id": _s(rec, "pagerduty_service_id"),
         "saga_step": 3})


# ---------------------------------------------------------------------------
# Reading into accounts the sweep does not run in
# ---------------------------------------------------------------------------
# B-112. Three of design 13's checks need state the sweep cannot see from
# tooling, and two of them are genuine reads served by narrow roles:
#
#   PlatformVerificationReader      in each member account
#                                   (baseline/90-verification-reader.yaml)
#   PlatformVerificationOrgReader   in the management account
#                                   (org/30-verification-org-reader.yaml)
#
# Deliberately NOT OrganizationAccountAccessRole. A sweep that runs on a
# schedule should not hold administrator in every tenant account in the estate
# to find out whether a canary passed.
#
# Every failure to assume returns `undeterminable` carrying the reason. It must
# never return `fail`: "I could not look" and "I looked and it is broken" are
# different findings, and reporting the second when the first is true sends
# somebody to fix a healthy tenant.

MEMBER_READER_ROLE = os.environ.get(
    "MEMBER_READER_ROLE", "PlatformVerificationReader")
ORG_READER_ROLE_ARN = os.environ.get("ORG_READER_ROLE_ARN", "")

sts = boto3.client("sts")


def _assume(role_arn, session_name):
    """A boto3 Session for `role_arn`, or (None, why)."""
    try:
        creds = sts.assume_role(
            RoleArn=role_arn, RoleSessionName=session_name[:64])["Credentials"]
    except Exception as exc:                          # noqa: BLE001
        return None, f"could not assume {role_arn}: {type(exc).__name__}: {exc}"
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"]), None


def _member_session(account_id):
    return _assume(
        f"arn:aws:iam::{account_id}:role/{MEMBER_READER_ROLE}",
        f"verify-{account_id}")


# ---------------------------------------------------------------------------
# 3. The canary is running and passing
# ---------------------------------------------------------------------------


def check_canary(ctx):
    rec = ctx["record"]
    account_ids = _s(rec, "account_ids")
    if not account_ids:
        return _result(
            UNKNOWN, {"account_ids": None, "saga_step": 5},
            "the tenant has no accounts, so there is nowhere for a canary to "
            "run. Blocked by the vesting pipeline invocation (phase 3).")
    if not _s(rec, "instrumented_at"):
        return _result(
            UNKNOWN, {"account_ids": account_ids,
                      "instrumented_at": None, "saga_step": 6},
            "instrumentation has not been rolled out to this tenant, so no "
            "canary exists to be running or passing.")

    if isinstance(account_ids, str):
        account_ids = [a.strip() for a in account_ids.split(",") if a.strip()]

    examined = {"accounts": {}, "canaries_seen": 0, "accounts_read": 0}
    failing, unreadable = [], []

    for account_id in account_ids:
        session, why = _member_session(account_id)
        if session is None:
            unreadable.append(account_id)
            examined["accounts"][account_id] = {"read": False, "why": why}
            continue
        try:
            runs = session.client("synthetics").describe_canaries_last_run()
        except Exception as exc:                      # noqa: BLE001
            unreadable.append(account_id)
            examined["accounts"][account_id] = {
                "read": False,
                "why": f"{type(exc).__name__}: {exc}"}
            continue

        examined["accounts_read"] += 1
        states = {}
        for entry in runs.get("CanariesLastRun") or []:
            name = entry.get("CanaryName")
            state = ((entry.get("LastRun") or {}).get("Status") or {}).get(
                "State")
            states[name] = state
            examined["canaries_seen"] += 1
            if state != "PASSED":
                failing.append(f"{account_id}/{name}={state}")
        examined["accounts"][account_id] = {"read": True, "canaries": states}

    if unreadable:
        return _result(
            UNKNOWN, examined,
            "could not read canary state in "
            + ", ".join(unreadable)
            + ". The sweep assumes "
            + MEMBER_READER_ROLE
            + " in each member account (B-112); a tenant whose baseline has "
              "not yet created that role is undeterminable, not failing.")

    if examined["canaries_seen"] == 0:
        # The population question. Zero canaries produce zero failures, and a
        # check that read that as health would clear a tenant with no
        # monitoring at all -- which is the state design 13's check exists to
        # catch, not to pass over.
        return _result(
            FAIL, examined,
            "no canary exists in any of this tenant's accounts. design 13 asks "
            "for a canary running and passing; zero canaries produce zero "
            "failures, which is not the same as passing.")

    if failing:
        return _result(
            FAIL, examined,
            "canary not passing: " + ", ".join(failing) + ".")

    return _result(
        PASS, examined,
        f"{examined['canaries_seen']} canary(ies) across "
        f"{examined['accounts_read']} account(s), all last runs PASSED.")


# ---------------------------------------------------------------------------
# 4. A test change reconciled correctly against Jira
# ---------------------------------------------------------------------------


def check_change_reconciliation(ctx):
    """Partially exercisable: the reconciliation engine's INPUT can be proven
    reachable even though the full round trip cannot.

    Reported as undeterminable regardless, because proving Jira is reachable
    is not proving a change reconciled -- and reporting the weaker thing as
    though it were the stronger one is the exact substitution this module
    exists to refuse. What was verified is recorded so the residue is visible.
    """
    verified = {}
    try:
        import jira
        health = jira.health()
        verified["jira_reachable"] = bool(health.get("ok"))
        verified["change_project_resolves"] = health.get(
            "default_change_project")
    except Exception as exc:                          # noqa: BLE001
        return _result(
            UNKNOWN, {"jira_reachable": False, "error": type(exc).__name__},
            f"Jira could not be reached ({exc}), so nothing can be concluded "
            "about change reconciliation for this tenant.")

    return _result(
        UNKNOWN, verified,
        "Jira is reachable and the change project resolves, but design 13 "
        "asks for a test change that RECONCILED -- which needs a deployment "
        "event to reconcile against, and the vesting pipeline that would "
        "produce one is not built (phase 3). Reachability is not "
        "reconciliation.")


# ---------------------------------------------------------------------------
# 5. Backup policy attached; a first recovery point exists
# ---------------------------------------------------------------------------


def check_backup(ctx):
    """Both halves, because either alone is a false positive.

    A plan with no recovery point is a policy that has never run, and it looks
    identical to a healthy one in every view that shows configuration rather
    than outcome. A recovery point with no plan is a manual backup somebody
    took once.

    This check previously reported that design 05 was absent and that there was
    "no backup policy to attach and no recovery point to find". That stopped
    being true when design 05 arrived and `recovery/30-backup-policy.yaml` and
    `40-backup-vault.yaml` were built and deployed -- a stale refusal, which is
    the same failure as a stale pass: the check was not reporting the estate,
    it was reporting what was true when it was written.
    """
    rec = ctx["record"]
    account_ids = _s(rec, "account_ids")
    if not account_ids:
        return _result(
            UNKNOWN, {"account_ids": None, "saga_step": 5},
            "the tenant has no accounts, so there is no vault to hold a "
            "recovery point. Blocked by the vesting pipeline invocation "
            "(phase 3).")

    if isinstance(account_ids, str):
        account_ids = [a.strip() for a in account_ids.split(",") if a.strip()]

    examined = {"accounts": {}, "plans_seen": 0, "recovery_points_seen": 0}
    unreadable, without_plan, without_point = [], [], []

    for account_id in account_ids:
        session, why = _member_session(account_id)
        if session is None:
            unreadable.append(account_id)
            examined["accounts"][account_id] = {"read": False, "why": why}
            continue
        backup = session.client("backup")
        try:
            plans = [p for p in (backup.list_backup_plans().get(
                         "BackupPlansList") or [])
                     if (p.get("BackupPlanName") or "").startswith("platform-")]
            vaults = [v for v in (backup.list_backup_vaults().get(
                          "BackupVaultList") or [])
                      if (v.get("BackupVaultName") or "").startswith(
                          "platform-backup-")]
            points = 0
            for vault in vaults:
                points += len(backup.list_recovery_points_by_backup_vault(
                    BackupVaultName=vault["BackupVaultName"],
                    MaxResults=1).get("RecoveryPoints") or [])
        except Exception as exc:                      # noqa: BLE001
            unreadable.append(account_id)
            examined["accounts"][account_id] = {
                "read": False, "why": f"{type(exc).__name__}: {exc}"}
            continue

        examined["plans_seen"] += len(plans)
        examined["recovery_points_seen"] += points
        examined["accounts"][account_id] = {
            "read": True,
            "plans": [p.get("BackupPlanName") for p in plans],
            "vaults": [v.get("BackupVaultName") for v in vaults],
            "recovery_points": points}
        if not plans:
            without_plan.append(account_id)
        elif points == 0:
            without_point.append(account_id)

    if unreadable:
        return _result(
            UNKNOWN, examined,
            "could not read backup state in " + ", ".join(unreadable)
            + ". The sweep assumes " + MEMBER_READER_ROLE
            + " in each member account (B-112).")

    if without_plan:
        return _result(
            FAIL, examined,
            "no platform backup plan in " + ", ".join(without_plan)
            + ". recovery/30-backup-policy.yaml has not reached these "
              "accounts.")

    if without_point:
        return _result(
            FAIL, examined,
            "a backup plan exists in " + ", ".join(without_point)
            + " and has produced no recovery point. A plan that has never run "
              "is indistinguishable from a healthy one in every view that "
              "shows configuration rather than outcome, which is why design 13 "
              "asks for the first recovery point and not the plan.")

    return _result(
        PASS, examined,
        f"{examined['plans_seen']} platform backup plan(s) and "
        f"{examined['recovery_points_seen']} recovery point(s) across "
        f"{len(account_ids)} account(s).")


# ---------------------------------------------------------------------------
# 6. The account inherits the guardrails we believe it does
# ---------------------------------------------------------------------------


def check_guardrails(ctx):
    """Inheritance, not the harness. Decided with Jamie 2026-09-22.

    design 13 asks that "the guardrail test harness passes against these
    specific accounts". `scripts/test-guardrails.sh` cannot serve that: it
    proves SCPs work by attempting denied actions with an administrator,
    creating IAM roles, KMS keys, backup vaults and EBS volumes in the account
    under test. Running it per tenant would mean a scheduled job holding admin
    in every account in the estate, and creating test IAM roles inside a
    client's account as a condition of their go-live.

    It is also the wrong question. SCPs attach to OUs. Running the harness for
    forty accounts in one OU tests the same policy forty times and learns
    nothing after the first, so the harness stays the per-OU exercise it is.

    What is true per account, and is a read: is this account under the OU we
    believe it is, and are policies actually attached to that OU? Expected
    comes from SSM, actual comes from Organizations -- two sources, so the
    comparison is not the registry agreeing with itself.
    """
    rec = ctx["record"]
    account_ids = _s(rec, "account_ids")
    if not account_ids:
        return _result(
            UNKNOWN, {"account_ids": None, "saga_step": 5},
            "the tenant has no accounts, so there is nothing to check "
            "placement for.")

    if not ORG_READER_ROLE_ARN:
        return _result(
            UNKNOWN, {"account_ids": account_ids, "ORG_READER_ROLE_ARN": None},
            "ORG_READER_ROLE_ARN is not set, so Organizations cannot be "
            "reached. org/30-verification-org-reader.yaml creates the role in "
            "the management account; until it is deployed and wired, placement "
            "is unverifiable rather than wrong.")

    partner, client = _s(rec, "partner"), _s(rec, "client")
    if not partner or not client:
        return _result(
            UNKNOWN, {"partner": partner, "client": client},
            "the registry record does not name both a partner and a client, so "
            "there is no expected OU to compare against.")

    session, why = _assume(ORG_READER_ROLE_ARN, "verify-org")
    if session is None:
        return _result(UNKNOWN, {"role": ORG_READER_ROLE_ARN}, why)

    param = f"/platform/org/ou/members/{partner}/{client}"
    try:
        expected_ou = session.client("ssm").get_parameter(
            Name=param)["Parameter"]["Value"].strip()
    except Exception as exc:                          # noqa: BLE001
        return _result(
            UNKNOWN, {"parameter": param},
            f"the client OU parameter {param} could not be read "
            f"({type(exc).__name__}). The OU is published when it is created; "
            "its absence means the client OU does not exist, which is a "
            "provisioning gap rather than a guardrail failure.")

    if isinstance(account_ids, str):
        account_ids = [a.strip() for a in account_ids.split(",") if a.strip()]

    orgs = session.client("organizations")
    examined = {"expected_ou": expected_ou, "parameter": param, "accounts": {}}
    misplaced, unpoliced, unreadable = [], [], []

    for account_id in account_ids:
        try:
            parents = orgs.list_parents(ChildId=account_id).get("Parents") or []
            actual = parents[0]["Id"] if parents else None
            attached = orgs.list_policies_for_target(
                TargetId=actual, Filter="SERVICE_CONTROL_POLICY").get(
                    "Policies") or [] if actual else []
        except Exception as exc:                      # noqa: BLE001
            unreadable.append(account_id)
            examined["accounts"][account_id] = {
                "read": False, "why": f"{type(exc).__name__}: {exc}"}
            continue

        names = sorted(p.get("Name") for p in attached)
        examined["accounts"][account_id] = {
            "read": True, "actual_ou": actual, "scps_attached": names}
        if actual != expected_ou:
            misplaced.append(f"{account_id} is in {actual}, not {expected_ou}")
        # FullAWSAccess alone is AWS's default and is not a guardrail. An OU
        # carrying only it has had the platform's SCPs detached, which reads as
        # "policies attached" to anything that merely counts them.
        elif not [n for n in names if n != "FullAWSAccess"]:
            unpoliced.append(account_id)

    if unreadable:
        return _result(
            UNKNOWN, examined,
            "could not read placement for " + ", ".join(unreadable) + ".")

    if misplaced:
        return _result(
            FAIL, examined,
            "account placement does not match the published OU: "
            + "; ".join(misplaced)
            + ". An account outside its client OU inherits whatever its actual "
              "parent carries, which is the one failure the OU tree exists to "
              "prevent.")

    if unpoliced:
        return _result(
            FAIL, examined,
            "the client OU carrying " + ", ".join(unpoliced)
            + " has no service control policy attached beyond AWS's default "
              "FullAWSAccess. The guardrails are not applying to this tenant.")

    return _result(
        PASS, examined,
        f"all {len(account_ids)} account(s) sit in {expected_ou} as published, "
        "with platform SCPs attached. Note this asserts INHERITANCE, not that "
        "the policies deny what they claim -- scripts/test-guardrails.sh "
        "proves that per OU, and is the exercise this deliberately does not "
        "duplicate per account.")
# ---------------------------------------------------------------------------
# 7. Cost Category mapping resolves; the account appears by-customer
# ---------------------------------------------------------------------------


def check_cost_mapping(ctx):
    rec = ctx["record"]
    accounts = _s(rec, "account_ids")
    if not accounts:
        return _result(
            UNKNOWN, {"account_ids": None, "saga_step": 5},
            "the tenant has no accounts, so no Cost Category membership can "
            "resolve and nothing can appear in the by-customer view.")
    if not _s(rec, "cost_category_synced_at"):
        return _result(
            FAIL, {"account_ids": accounts, "cost_category_synced_at": None,
                   "saga_step": 7},
            "the tenant has accounts but has never been synced into a Cost "
            "Category. Its spend is therefore unattributed, which is a "
            "billing defect and not merely an incomplete step.")
    return _not_built(
        "Reading the commercial by-customer view back to confirm the account "
        "appears in it. The Cost Category sync is recorded but the read-back "
        "that would prove it resolved is not built", "B-090",
        {"account_ids": accounts,
         "cost_category_synced_at": _s(rec, "cost_category_synced_at")})


# ---------------------------------------------------------------------------
# 8. BAA notification windows, at BOTH levels, and AltDigital's tighter
# ---------------------------------------------------------------------------


def check_notification_windows(ctx):
    """design 13's own footnote: "matters more than it looks".

    The reason is the failure mode. An unset window does not break anything
    until an incident, at which point the clock cannot run and the omission is
    discovered while it is doing damage. So this is checked before go-live,
    when it costs nothing to fix.

    Fully implemented -- this is the one check on the list that exercises the
    real thing today, because the real thing is two numbers in the registry
    and their relationship to each other.
    """
    rec = ctx["record"]
    partner_slug = _s(rec, "partner")
    partner = _partner_record(partner_slug)

    if not partner:
        return _result(
            FAIL, {"partner": partner_slug, "partner_record": None},
            f"the tenant names partner '{partner_slug}' and no such partner "
            "record exists, so neither notification window can be read.")

    altdig = _s(partner, "altdig_hours")
    to_client = _s(partner, "notify_hours")
    examined = {"partner": partner_slug,
                "altdig_to_partner_hours": altdig,
                "partner_to_client_hours": to_client}

    missing = [n for n, v in (("AltDigital->partner", altdig),
                              ("partner->client", to_client)) if not v]
    if missing:
        return _result(
            FAIL, examined,
            f"notification window(s) unset: {', '.join(missing)}. design 13 "
            "requires both levels non-null. An unset window means the "
            "incident notification clock cannot run, and that would otherwise "
            "be discovered during an incident.")

    try:
        a, c = int(altdig), int(to_client)
    except (TypeError, ValueError):
        return _result(
            FAIL, examined,
            "a notification window is set but is not a number of hours, so "
            "the clock cannot compute a deadline from it.")

    if a >= c:
        # Strictly tighter, not merely equal. Equal leaves the partner no time
        # at all: it would be obliged to notify its client at the same instant
        # it is itself notified.
        return _result(
            FAIL, examined,
            f"AltDigital->partner is {a}h and partner->client is {c}h. design "
            "13 requires AltDigital's to be TIGHTER. As set, the partner must "
            "notify its client no later than it is itself notified, which is "
            "a chain that cannot be satisfied.")

    return _result(
        PASS, examined,
        f"both windows set; AltDigital->partner {a}h is tighter than "
        f"partner->client {c}h, leaving the partner {c - a}h to act.")


# ---------------------------------------------------------------------------
# 9. Where HOOPs are configured: a full turndown/turnup cycle completed
# ---------------------------------------------------------------------------


def check_hoop_cycle(ctx):
    """Conditional, and the condition has to be DETERMINED rather than assumed.

    "Where HOOPs are configured" makes not-applicable a legitimate outcome --
    but only if it can be established that none are. Assuming none because
    none were found is how a check becomes inert.
    """
    rec = ctx["record"]
    if not _s(rec, "instrumented_at"):
        return _result(
            UNKNOWN, {"instrumented_at": None, "saga_step": 6},
            "instrumentation and HOOP schedules are rolled out together in "
            "step 6, which has not run. Whether this tenant has HOOPs "
            "configured is therefore unknown -- and unknown is not 'none'.")
    return _not_built(
        "HOOP scheduling and the turndown/turnup exercise", "phase 13",
        {"instrumented_at": _s(rec, "instrumented_at")})


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

CHECKS = [
    ("evidence-received", check_evidence),
    ("synthetic-alarm-paged", check_paging),
    ("canary-passing", check_canary),
    ("test-change-reconciled", check_change_reconciliation),
    ("backup-and-recovery-point", check_backup),
    ("guardrail-harness", check_guardrails),
    ("cost-category-resolves", check_cost_mapping),
    ("notification-windows", check_notification_windows),
    ("hoop-turndown-turnup", check_hoop_cycle),
]


def sweep(tenant_id):
    """Run every check. Never stops early.

    A sweep that returned on the first failure would report one problem when
    there are six, and the person reading it would fix that one and re-run --
    which is a slow way to find out the other five. Everything runs; the
    verdict is the aggregate.
    """
    rec = saga.record(tenant_id)
    if not rec:
        raise VerificationFailed(
            f"no registry record for {tenant_id}. Nothing can be verified "
            "about a tenant the registry does not describe.")

    ctx = {"tenant_id": tenant_id, "record": rec}
    results = {}
    for name, fn in CHECKS:
        try:
            results[name] = fn(ctx)
        except Exception as exc:                      # noqa: BLE001
            # A check that raised did not pass, and did not evaluate either.
            results[name] = _result(
                UNKNOWN, {"error": type(exc).__name__},
                f"the check itself failed: {exc}. Nothing can be concluded "
                "about what it was meant to verify.")

    passed = [n for n, r in results.items() if r["verdict"] == PASS]
    failed = [n for n, r in results.items() if r["verdict"] == FAIL]
    unknown = [n for n, r in results.items() if r["verdict"] == UNKNOWN]

    out = {
        "record_type": "verification-sweep",
        "tenant_id": tenant_id,
        "swept_at": _iso(_now()),
        "checks_total": len(CHECKS),
        "passed": passed,
        "failed": failed,
        "undeterminable": unknown,
        # The only condition that clears. Not "no failures" -- an
        # undeterminable check has not passed, and design 13's fail-closed
        # rule makes not-passed and failed the same thing for go-live.
        "go_live_eligible": len(passed) == len(CHECKS),
        "results": results,
    }
    print(json.dumps(out, default=str))
    return out


def run(event, context=None):
    tenant_id = (event or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("verify needs tenant_id")
    out = sweep(tenant_id)
    if out["go_live_eligible"]:
        saga.write_back(tenant_id, verified_at=out["swept_at"])
        out["verified_at_written"] = True
    return out


def handler(event, context=None):
    action = (event or {}).get("action") or "run"
    if action == "run":
        return run(event, context)
    if action == "sweep":
        return sweep((event or {}).get("tenant_id"))
    raise ValueError(f"unknown action '{action}'")
