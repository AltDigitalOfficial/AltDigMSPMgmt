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
"""
import datetime
import json
import os

import boto3

ce = boto3.client("ce")
ssm = boto3.client("ssm")

RATES_PARAM = os.environ.get("COST_RATES_PARAM", "/platform/cost/rates")

# The Cost Categories sync creates these. Named here because the report is
# useless without them, and a missing category should say which one rather
# than returning an empty grouping.
TENANT_CATEGORY = os.environ.get("COST_TENANT_CATEGORY", "Tenant")
KIND_CATEGORY = os.environ.get("COST_KIND_CATEGORY", "AccountKind")
MODEL_CATEGORY = os.environ.get("COST_MODEL_CATEGORY", "ServiceModel")

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


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _month_bounds(month):
    """(start, end) for a YYYY-MM, as Cost Explorer wants them.

    End is EXCLUSIVE and is the first of the next month. Cost Explorer treats
    it that way, and an inclusive reading here would silently drop the last day
    of every month -- roughly 3% of every invoice, consistently, in AltDigital's
    favour. The kind of error that is embarrassing rather than expensive.
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


def _ce_month(start, end, group_by, metrics):
    return ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=metrics,
        GroupBy=group_by,
    )


def _amount(group, metric):
    return float(group["Metrics"][metric]["Amount"])


def _period_total(start, end):
    """Ungrouped spend for the period, to tell 'nothing' from 'not grouped'."""
    try:
        result = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY", Metrics=["UnblendedCost"])
        return sum(float(r["Total"]["UnblendedCost"]["Amount"])
                   for r in result.get("ResultsByTime", []))
    except Exception:                              # noqa: BLE001
        # Unknown, and must not be reported as zero -- zero is one of the two
        # answers this call exists to distinguish.
        return None


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

    # Both metrics, every run. See the module docstring: the day these diverge
    # is the day the uplift starts being billed on the wrong basis, and nothing
    # else in the platform would notice.
    grouped = _ce_month(
        start, end,
        [{"Type": "COST_CATEGORY", "Key": TENANT_CATEGORY},
         {"Type": "COST_CATEGORY", "Key": KIND_CATEGORY}],
        ["UnblendedCost", "NetUnblendedCost"])

    tenants = {}
    shared_total = 0.0
    discount_delta = 0.0
    # How many groups Cost Explorer returned AT ALL.
    #
    # "No tenants" has two causes and they look identical in the output:
    #
    #   every account is kind=shared        correct, and true today
    #   the Cost Categories have not        Cost Explorer takes up to 24 hours
    #   backfilled yet                      to populate a newly created one
    #
    # A billing report that cannot tell those apart would show a customer
    # nothing and look healthy doing it. Counted rather than inferred.
    groups_seen = 0
    for result in grouped.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            groups_seen += 1
            tenant, kind = (group["Keys"] + ["", ""])[:2]
            tenant = tenant.split("$", 1)[-1] or "unregistered"
            kind = kind.split("$", 1)[-1] or "unknown"
            gross = _amount(group, "UnblendedCost")
            net = _amount(group, "NetUnblendedCost")
            discount_delta += gross - net

            if kind == "shared":
                # design 10: absorbed into the percentage uplift, never
                # allocated. Reported as its own figure because absorbing a
                # cost is not a reason to be unable to see it -- and because
                # B-090's report needs this number to exist.
                shared_total += gross
                continue

            entry = tenants.setdefault(tenant, {"raw_aws_cost": 0.0,
                                                "accounts": set()})
            entry["raw_aws_cost"] += gross

    # Account counts, for the per-account flat fee.
    accounts = _ce_month(
        start, end,
        [{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
         {"Type": "COST_CATEGORY", "Key": TENANT_CATEGORY}],
        ["UnblendedCost"])
    for result in accounts.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            account_id, tenant = (group["Keys"] + ["", ""])[:2]
            tenant = tenant.split("$", 1)[-1] or "unregistered"
            if tenant in tenants:
                tenants[tenant]["accounts"].add(account_id)

    # Each tenant's service model, from the ServiceModel Cost Category, which
    # scripts/sync-cost-categories.sh builds from the registry's account
    # records. Until 2026-09-30 the model came from the invocation EVENT, once
    # for the whole run, so every tenant in a report was billed at the same
    # model whatever its contract said.
    by_model = _ce_month(
        start, end,
        [{"Type": "COST_CATEGORY", "Key": TENANT_CATEGORY},
         {"Type": "COST_CATEGORY", "Key": MODEL_CATEGORY}],
        ["UnblendedCost"])
    for result in by_model.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            tenant, model = (group["Keys"] + ["", ""])[:2]
            tenant = tenant.split("$", 1)[-1] or "unregistered"
            model = model.split("$", 1)[-1]
            if tenant in tenants:
                tenants[tenant].setdefault("models", set()).add(model)

    lines = []
    for tenant, entry in sorted(tenants.items()):
        recorded = {m for m in entry.get("models", set()) if m not in UNRECORDED_MODELS}
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
        "tenants": lines,
        # design 10 absorbs this rather than allocating it. Shown because the
        # position "the uplift covers it" is otherwise uncheckable. B-090.
        "shared_platform_cost": round(shared_total, 2),
        "rates_are_contractual": contractual,
        # See above. Zero groups means the categorisation is not answering,
        # not that the estate is empty.
        "cost_groups_seen": groups_seen,
    }

    if groups_seen == 0:
        # THREE causes, not two, and only one is a problem. Separated by asking
        # what the period cost WITHOUT any grouping -- which the first version
        # did not do, and so reported "the categories have not backfilled"
        # about a month in which nothing had been spent because the estate did
        # not yet exist.
        #
        #   ungrouped total is zero     nothing was spent. Not a problem.
        #   total > 0, no groups        categorisation is not answering. It is
        #                               either backfilling or misconfigured,
        #                               and both need somebody to look.
        total = _period_total(start, end)
        if total is None:
            out["coverage_warning"] = (
                "Cost Explorer returned no grouped results, and the ungrouped "
                "total could not be read either — so it is not known whether "
                "this period had no spend or whether the categorisation "
                "failed. No conclusion should be drawn from an empty report.")
        elif total == 0.0:
            out["coverage_note"] = (
                "No spend at all in this period, grouped or ungrouped. The "
                "report is empty because nothing was spent, not because the "
                "categorisation failed.")
        else:
            out["coverage_warning"] = (
                f"This period cost {total:.2f} ungrouped, and Cost Explorer "
                "returned NO grouped results. Spend exists and the Cost "
                "Categories are not accounting for it. Most likely they were "
                "created within the last 24 hours and are still backfilling; "
                "if not, check scripts/sync-cost-categories.sh has run. Do "
                "not read this report as an empty estate.")
    elif not lines:
        # Groups came back and none of them were billable. That IS a real
        # answer, and a different one.
        out["coverage_note"] = (
            f"{groups_seen} cost group(s) found, none billable — every "
            "account in this period is kind=shared, which is AltDigital's own "
            "estate. Correct until a tenant account is vested.")

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
