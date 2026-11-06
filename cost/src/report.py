"""The monthly tenant cost report. Prompt 9.2, design 10 "Support pricing".

    Flat fee per account per month + a percentage of AWS spend.
    [...] Monthly tenant service report includes raw AWS cost alongside the
    uplift.

Three numbers, shown separately, never one number
--------------------------------------------------
Prompt 9.2 asks for raw AWS cost, flat fee and percentage uplift "shown
separately", and design 10 says why:

    Showing the raw AWS cost alongside the uplift is good practice -- it makes
    the margin transparent and gives the customer a reason to care about
    right-sizing, which benefits both parties.

A single total would hide which of the three moved. A customer whose bill rose
because they left a test cluster running and one whose bill rose because
AltDigital changed its percentage have very different conversations, and only
the separated form tells them apart.

The rates here are INDICATIVE and are not a price list
-------------------------------------------------------
design 10 gives ~$1,000/account/month and 15-20%, and then says:

    Model A is materially less work than Model B. The flat fee and percentage
    should differ accordingly. Worth pricing before the first contract rather
    than after.

So the numbers are a starting point somebody still has to decide. They live in
configuration, every output says which rate it used, and a report generated
before the rates are set says so rather than presenting a plausible invoice.
An indicative figure that reaches a customer becomes the price.

The percentage basis is LIST price, and that will drift
--------------------------------------------------------
design 10 is explicit that the uplift applies to list-price AWS spend, not net
of Savings Plans or Reserved Instance discounts, because AltDigital carries the
commitment risk and retains the arbitrage.

    Interim position: no negotiated discounts exist yet, so list price is the
    actual price.

Today those are the same number, so unblended cost is a correct basis. The
moment a commitment is bought they diverge, and a report still using unblended
cost would quietly bill the uplift on the DISCOUNTED figure -- handing the
customer the arbitrage design 10 says AltDigital keeps, with nothing anywhere
saying it happened.

So the report compares unblended against net-unblended every run and reports
the basis it used. When they differ it says so loudly instead of continuing to
produce believable-looking numbers.

Where the numbers come from: the CUR 2.0 export, not Cost Explorer
------------------------------------------------------------------
Phase 15.9 step 4. The report reads platform_cost.cur2 in the FinOps account
through Athena workgroup platform-finops (finops/10-cost-exports.yaml), one
query per month. Three things differ from Cost Explorer, each found in the
first delivery (2026-11-06), and each handled here rather than assumed away:

    category names   CUR rewrites them: Tenant -> tenant, AccountKind ->
                     account_kind, ServiceModel -> service_model. The map is
                     read with element_at, which answers NULL for an absent
                     key; a subscript would fail the whole query.
    net columns      line_item_net_unblended_cost was NULL on every line, with
                     no discounts in the estate. NULL is read as "equal to
                     unblended" for the arithmetic, and the output says how
                     many lines carried a net figure, so the basis check never
                     passes in silence.
    no backfill      the export began with 2026-11. An earlier month has no
                     rows at all, which must not read as a month of no spend.

Tax lines are included in raw AWS cost, as they were in Cost Explorer's
UnblendedCost. Whether the uplift applies to tax is a pricing decision, not
this report's.

Spend with no tenant is reported, not billed
--------------------------------------------
An account the Cost Categories do not cover has no tenant. Cost Explorer
answered "Tenant$" for it and the first version billed it as a tenant called
"unregistered", at the default rate. Here it is its own figure, with the
accounts named, and a warning: a platform account created after the last
scripts/sync-cost-categories.sh run lands here (the FinOps account did, on its
first day).
"""
import datetime
import json
import os
import time

import boto3

athena = boto3.client("athena")
ssm = boto3.client("ssm")

RATES_PARAM = os.environ.get("COST_RATES_PARAM", "/platform/cost/rates")
WORKGROUP = os.environ.get("COST_WORKGROUP", "platform-finops")
DATABASE = os.environ.get("COST_DATABASE", "platform_cost")
TABLE = os.environ.get("COST_TABLE", "cur2")
QUERY_TIMEOUT_SECONDS = int(os.environ.get("COST_QUERY_TIMEOUT_SECONDS", "240"))

# The Cost Categories sync creates these, and CUR writes them in snake case.
# Named here because the report is useless without them, and a missing
# category should say which one rather than returning an empty grouping.
TENANT_KEY = os.environ.get("COST_TENANT_KEY", "tenant")
KIND_KEY = os.environ.get("COST_KIND_KEY", "account_kind")
MODEL_KEY = os.environ.get("COST_MODEL_KEY", "service_model")

# Cost Category values that mean "the registry did not say". A tenant whose
# accounts carry one of these is billed at the `default` rate, as designed.
UNRECORDED_MODELS = {"", "unset", "unregistered"}


class RatesNotSet(Exception):
    """Nobody has decided the pricing yet.

    Distinct and fatal. design 10's figures are indicative, and a report that
    quietly used them would put a number in front of a customer that no human
    chose. The failure mode of guessing here is a contract.
    """


class RatesUnreadable(Exception):
    """The rates could not be READ. Not the same as not being set.

    Both stop the report, so the safe outcome is identical -- which is exactly
    why this was wrong for an hour without anyone noticing. The DIFFERENCE is
    where it sends whoever reads it:

        not set      somebody has to decide the pricing
        unreadable   somebody has to fix a credential or a permission

    The first version caught every exception and reported all of them as "run
    scripts/set-cost-rates.sh". An expired SSO token therefore produced
    confident, specific, wrong advice -- the same shape as B-086, B-089, B-091
    and B-092, committed in the same session that documented them.
    """


class BillingDataUnreadable(Exception):
    """The export could not be queried. Not the same as a month of no spend."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _month_bounds(month):
    """(start, end) for a YYYY-MM, end EXCLUSIVE: the first of the next month.

    Reported in the output so a reader can see the period. The query itself
    selects by billing_period, which is the whole month by construction, so
    the inclusive-end error this once guarded against cannot occur there.
    """
    year, mon = (int(p) for p in month.split("-"))
    start = datetime.date(year, mon, 1)
    end = datetime.date(year + (mon == 12), (mon % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def rates():
    """Flat fee and percentage, per service model. From SSM, never defaults."""
    try:
        raw = ssm.get_parameter(Name=RATES_PARAM)["Parameter"]["Value"]
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            raise RatesNotSet(
                f"{RATES_PARAM} does not exist. design 10's $1,000 and 15-20% "
                "are INDICATIVE and were never a price list -- no report is "
                "produced until somebody sets the real numbers. Run "
                "scripts/set-cost-rates.sh.") from exc
        raise RatesUnreadable(
            f"could not read {RATES_PARAM}: {code or type(exc).__name__}. "
            "This is NOT 'the rates are unset' -- the parameter may be "
            "perfectly fine and unreachable. Check credentials and the "
            "ssm:GetParameter permission before touching the pricing.") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RatesUnreadable(
            f"{RATES_PARAM} is not valid JSON. It was written by something "
            "other than scripts/set-cost-rates.sh.") from exc
    models = data.get("service_models") or {}
    if not models:
        raise RatesNotSet(f"{RATES_PARAM} defines no service models.")
    return models, bool(data.get("rates_are_contractual"))


def month_sql(month):
    """One row per (tenant, kind, model, account) for the month.

    `month` is validated as YYYY-MM before it reaches here; nothing else from
    the event enters the SQL.
    """
    return (
        f"SELECT element_at(cost_category, '{TENANT_KEY}') AS tenant, "
        f"element_at(cost_category, '{KIND_KEY}') AS kind, "
        f"element_at(cost_category, '{MODEL_KEY}') AS model, "
        "line_item_usage_account_id AS account, "
        "sum(line_item_unblended_cost) AS unblended, "
        "sum(coalesce(line_item_net_unblended_cost, line_item_unblended_cost)) AS net, "
        "count(line_item_net_unblended_cost) AS net_lines, "
        "count(*) AS lines "
        f"FROM {TABLE} WHERE billing_period = '{month}' "
        "GROUP BY 1, 2, 3, 4")


def _query(sql):
    """Rows as dicts. Raises BillingDataUnreadable on any failure."""
    try:
        qid = athena.start_query_execution(
            QueryString=sql, WorkGroup=WORKGROUP,
            QueryExecutionContext={"Database": DATABASE})["QueryExecutionId"]
        deadline = time.time() + QUERY_TIMEOUT_SECONDS
        while True:
            st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if st["State"] == "SUCCEEDED":
                break
            if st["State"] in ("FAILED", "CANCELLED"):
                raise BillingDataUnreadable(
                    f"query {qid} {st['State']}: {st.get('StateChangeReason')}")
            if time.time() > deadline:
                raise BillingDataUnreadable(f"query {qid} still {st['State']} at the deadline")
            time.sleep(2)
        rows, header = [], None
        for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
            for r in page["ResultSet"]["Rows"]:
                vals = [c.get("VarCharValue") for c in r["Data"]]
                if header is None:
                    header = vals
                    continue
                rows.append(dict(zip(header, vals)))
        return rows
    except BillingDataUnreadable:
        raise
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        raise BillingDataUnreadable(
            f"could not query {DATABASE}.{TABLE} in workgroup {WORKGROUP}: "
            f"{code or type(exc).__name__}. Run in the FinOps account, as "
            "CommercialReadOnly or an operator.") from exc


def _f(value):
    return float(value) if value not in (None, "") else 0.0


def report(event=None, context=None):
    """One month, one line per tenant, three numbers each."""
    month = (event or {}).get("month")
    if not month:
        # Last complete month. The current one is always wrong and always
        # looks low, which is the worst shape for a billing artefact.
        today = _now().date()
        first = today.replace(day=1)
        last_month = first - datetime.timedelta(days=1)
        month = f"{last_month.year:04d}-{last_month.month:02d}"
    try:
        datetime.datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    start, end = _month_bounds(month)

    try:
        models, contractual = rates()
    except (RatesNotSet, RatesUnreadable) as exc:
        out = {"record_type": "tenant-cost-report", "month": month,
               "produced": False,
               # WHICH of the two, not just that it failed. They send whoever
               # reads this to different people.
               "blocked_by": type(exc).__name__,
               "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    try:
        rows = _query(month_sql(month))
    except BillingDataUnreadable as exc:
        out = {"record_type": "tenant-cost-report", "month": month,
               "produced": False, "blocked_by": type(exc).__name__,
               "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    tenants = {}
    shared_total = 0.0
    discount_delta = 0.0
    uncategorized_total, uncategorized_accounts = 0.0, set()
    total, net_lines, lines_seen = 0.0, 0, 0
    # How many categorised groups the export returned.
    #
    # "No tenants" has two causes and they look identical in the output:
    #
    #   every account is kind=shared        correct while only AltDigital's
    #                                       own estate exists
    #   the Cost Categories are not         newly created, or the sync has
    #   answering                           not run
    #
    # A billing report that cannot tell those apart would show a customer
    # nothing and look healthy doing it. Counted rather than inferred.
    groups_seen = 0
    for row in rows:
        gross, net = _f(row.get("unblended")), _f(row.get("net"))
        total += gross
        discount_delta += gross - net
        net_lines += int(_f(row.get("net_lines")))
        lines_seen += int(_f(row.get("lines")))
        tenant, kind = row.get("tenant"), row.get("kind") or "unknown"
        if not tenant:
            uncategorized_total += gross
            uncategorized_accounts.add(row.get("account"))
            continue
        groups_seen += 1

        if kind == "shared":
            # design 10: absorbed into the percentage uplift, never
            # allocated. Reported as its own figure because absorbing a
            # cost is not a reason to be unable to see it -- and because
            # B-090's report needs this number to exist.
            shared_total += gross
            continue

        entry = tenants.setdefault(tenant, {"raw_aws_cost": 0.0, "accounts": set(),
                                            "models": set()})
        entry["raw_aws_cost"] += gross
        entry["accounts"].add(row.get("account"))
        # Each tenant's service model, from the ServiceModel Cost Category,
        # which scripts/sync-cost-categories.sh builds from the registry's
        # account records. Until 2026-09-30 the model came from the invocation
        # EVENT, once for the whole run, so every tenant in a report was billed
        # at the same model whatever its contract said.
        entry["models"].add(row.get("model") or "")

    lines = []
    for tenant, entry in sorted(tenants.items()):
        recorded = {m for m in entry["models"] if m not in UNRECORDED_MODELS}
        if len(recorded) > 1:
            # One tenant, accounts recorded under different models. Either a
            # registry error or a contract nobody has modelled; guessing which
            # model applies would put a number on an invoice nobody chose.
            lines.append({"tenant": tenant, "billable": False,
                          "problem": "accounts are recorded under different service "
                                     f"models: {', '.join(sorted(recorded))}"})
            continue

        # `default` covers a tenant whose service model is not recorded. It
        # does NOT cover a tenant whose model IS recorded and has no rate.
        #
        # Falling back in that second case would bill a Model A tenant at the
        # Model B rate, or the reverse, and produce an invoice that looks
        # entirely correct. design 10: "Model A is materially less work than
        # Model B. The flat fee and percentage should differ accordingly."
        # A silent fallback is precisely the error that costs money and trust.
        #
        # Lowercased because scripts/set-cost-rates.sh stores RATE_A_* as `a`.
        model = recorded.pop().lower() if recorded else "default"
        rate = models.get(model)
        if not rate:
            # Named, not skipped. A tenant silently missing from a billing
            # report is a tenant nobody invoices.
            lines.append({"tenant": tenant, "billable": False,
                          "problem": f"no rate for service model '{model}'"})
            continue

        flat_rate = float(rate["flat_fee_per_account_month"])
        pct = float(rate["uplift_percent"])
        account_count = len(entry["accounts"])
        raw = round(entry["raw_aws_cost"], 2)
        flat = round(flat_rate * account_count, 2)
        uplift = round(raw * pct / 100.0, 2)

        lines.append({
            "tenant": tenant,
            "billable": True,
            "accounts": account_count,
            # THE THREE, separately. Never summed into one field here: a
            # consumer that wants a total can add them, and one that prints
            # whatever it is given cannot accidentally hide the split.
            "raw_aws_cost": raw,
            "flat_fee": flat,
            "flat_fee_basis": f"{account_count} account(s) x {flat_rate}",
            "uplift": uplift,
            "uplift_basis": f"{pct}% of list-price AWS spend",
            "service_model": model,
        })

    out = {
        "record_type": "tenant-cost-report",
        "produced": True,
        "month": month,
        "period": {"start": start, "end_exclusive": end},
        "source": f"{DATABASE}.{TABLE} (CUR 2.0) via {WORKGROUP}",
        "tenants": lines,
        # design 10 absorbs this rather than allocating it. Shown because the
        # position "the uplift covers it" is otherwise uncheckable. B-090.
        "shared_platform_cost": round(shared_total, 2),
        "rates_are_contractual": contractual,
        # See above. Zero groups means the categorisation is not answering,
        # not that the estate is empty.
        "cost_groups_seen": groups_seen,
        # How many line items carried AWS's own net figure. Zero while no
        # discounts exist; the basis check below is only as good as this.
        "net_basis": {"lines": lines_seen, "lines_with_net": net_lines},
    }

    if not rows:
        # Not "nothing was spent". The export holds whole billing periods
        # from its first delivery onward, so an absent month is one it never
        # delivered: before 2026-11, or a delivery that has not happened.
        out["coverage_warning"] = (
            f"The export holds no line items at all for {month}. That is not "
            "a month of no spend: the export began with 2026-11 and was not "
            "backfilled, or this month has not been delivered. Do not read "
            "this report as an empty estate.")
    elif groups_seen == 0:
        out["coverage_warning"] = (
            f"This period cost {total:.2f}, and no line item carries a "
            f"'{TENANT_KEY}' cost category. Spend exists and the Cost "
            "Categories are not accounting for it. Check "
            "scripts/sync-cost-categories.sh has run since the accounts were "
            "created. Do not read this report as an empty estate.")
    elif not lines:
        # Groups came back and none of them were billable. That IS a real
        # answer, and a different one.
        out["coverage_note"] = (
            f"{groups_seen} cost group(s) found, none billable — every "
            "categorised account in this period is kind=shared, which is "
            "AltDigital's own estate.")

    if uncategorized_accounts:
        out["uncategorized"] = {"cost": round(uncategorized_total, 2),
                                "accounts": sorted(a for a in uncategorized_accounts if a)}
        out["uncategorized_warning"] = (
            f"{len(uncategorized_accounts)} account(s) carry no '{TENANT_KEY}' cost "
            "category, so their spend is in no tenant's line and was not billed. "
            "Run scripts/sync-cost-categories.sh, which builds the categories "
            "from the registry, and regenerate the report.")

    if not contractual:
        out["warning"] = (
            "The rates used are NOT marked contractual. design 10's figures "
            "are indicative and it says the flat fee and percentage should "
            "differ by service model, priced before the first contract. This "
            "output is an estimate and must not be sent to a customer.")

    if round(discount_delta, 2) > 0:
        # The uplift is being computed on unblended cost. design 10 says it
        # applies to LIST price, and these have now diverged -- so the basis is
        # understated by exactly this amount and somebody has to decide.
        out["basis_warning"] = (
            f"Unblended and net-unblended cost differ by {discount_delta:.2f} "
            "this month, so negotiated discounts now exist. design 10 applies "
            "the uplift to LIST price because AltDigital carries the "
            "commitment risk and retains the arbitrage. This report used "
            "unblended cost, which no longer equals list price -- the uplift "
            "above is understated. See design 10, 'Percentage basis: gross'.")

    print(json.dumps(out, default=str))
    return out


HANDLERS = {"report": report}


def handler(event, context):
    return report(event, context)
