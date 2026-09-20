"""Generates Cost Category definitions from the platform registry.

Prompt 9.2, design/10-commercial-model.md "Cost visibility and allocation":

    Cost Categories map account IDs to tenant, application, environment,
    service model and compliance scope -- WITHOUT requiring a single resource
    tag.

Why generated and not written in a template
--------------------------------------------
The rules are a function of which accounts exist and what they are, and that
changes every time an account is vested. A static list in CloudFormation would
be a second copy of the registry, maintained by hand, drifting quietly -- and
the failure would be silent: cost landing in the wrong tenant's bucket looks
exactly like cost landing in the right one.

B-060, B-064 and B-080 are all the same rule. Derive from the source of truth;
do not keep a second copy of it.

Accounts with no registry record
---------------------------------
Reported, never quietly dropped into a default bucket. An account in the
Organization that the registry has never heard of is one of two things: newly
vested and not yet recorded, or something nobody meant to create. Both want a
human. Silently categorising it as "unallocated" would make the second
indistinguishable from the first, and would put real spend somewhere nobody
looks.

design 10 on why the account boundary carries this at all:

    The account boundary gives per-application spend for free.
"""
import json
import sys

# The five dimensions prompt 9.2 names, mapped to the registry attribute that
# answers each. Data rather than five near-identical functions, so adding a
# dimension is a row here and a redeploy.
DIMENSIONS = {
    "Tenant": "client",
    "Application": "app",
    "Environment": "environment",
    "ServiceModel": "service_model",
    "ComplianceScope": "compliance_scope",
    # Not in prompt 9.2's list, and needed by the thing 9.2 exists to feed.
    #
    # design 10 positions shared-platform cost as absorbed into the percentage
    # uplift rather than allocated. That is only checkable if shared cost can
    # be separated from tenant cost in the first place, which needs a
    # dimension. Without it the absorbed number cannot be measured and the
    # position cannot be tested. B-090.
    "AccountKind": "kind",
    # Billing rolls up twice -- by client for the partner's invoicing, and by
    # partner for AltDigital's. design 10, "Cost visibility and allocation".
    "Partner": "partner",
}

# Where cost goes when the registry cannot say. A NAMED bucket rather than
# leaving the account out of the category: an account absent from every rule is
# invisible in a grouped report, whereas one in "unregistered" is a number
# somebody asks about.
UNREGISTERED = "unregistered"


def _value(record, attribute):
    raw = (record.get(attribute) or {})
    value = raw.get("S") if isinstance(raw, dict) else raw
    value = (value or "").strip()
    return value or None


def build(records, org_accounts):
    """Cost Category definitions, plus the accounts nobody could categorise.

    `records`   registry ACCOUNT items, as DynamoDB returns them
    `org_accounts`  every account id the Organization actually contains

    Returns (definitions, unregistered_account_ids).
    """
    by_account = {}
    for item in records:
        if _value(item, "record_type") != "ACCOUNT":
            continue
        account_id = _value(item, "account_id")
        if account_id:
            by_account[account_id] = item

    unregistered = sorted(set(org_accounts) - set(by_account))

    definitions = []
    for name, attribute in DIMENSIONS.items():
        # value -> [account ids]. Grouped so that one rule covers every account
        # sharing a value, which is both smaller and what the console shows.
        buckets = {}
        for account_id, record in by_account.items():
            value = _value(record, attribute)
            if not value:
                # An account whose record exists but does not answer THIS
                # dimension. Distinct from unregistered, and it still belongs
                # in a named bucket rather than nowhere.
                value = UNREGISTERED
            buckets.setdefault(value, []).append(account_id)

        if unregistered:
            buckets.setdefault(UNREGISTERED, []).extend(unregistered)

        rules = []
        for value, accounts in sorted(buckets.items()):
            rules.append({
                "Value": value,
                "Rule": {
                    "Dimensions": {
                        "Key": "LINKED_ACCOUNT",
                        "Values": sorted(set(accounts)),
                        "MatchOptions": ["EQUALS"],
                    }
                },
                "Type": "REGULAR",
            })

        definitions.append({
            "Name": name,
            "RuleVersion": "CostCategoryExpression.v1",
            "Rules": rules,
            # No DefaultValue.
            #
            # A default would sweep every unmatched account into one label and
            # make the sweep invisible -- which is the whole failure this file
            # is written against. Everything that can be categorised is named
            # by a rule; anything that cannot appears as `unregistered` because
            # it was put there explicitly, and is reported separately.
        })

    return definitions, unregistered


def main():
    payload = json.load(sys.stdin)
    definitions, unregistered = build(
        payload.get("registry") or [], payload.get("org_accounts") or [])
    json.dump({"definitions": definitions, "unregistered": unregistered},
              sys.stdout)


if __name__ == "__main__":
    main()
