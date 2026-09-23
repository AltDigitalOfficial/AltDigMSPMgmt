"""Builds the evidence tenant map from the platform accounts and the registry.

A file rather than a heredoc inside sync-evidence-tenants.sh, because that
arrangement had the script on stdin AND the data on stdin, and the data lost.
The result was a valid, empty map -- every evidence record filing as
unattributed, silently.

    evidence_tenants.py <platform-accounts-file> [<registry-accounts-file>]

platform-accounts-file   `aws ssm get-parameters-by-path /platform/org/account/`
                         text output: name and 12-digit id per line.
registry-accounts-file   `registry.sh list-accounts` output: account_id,
                         partner, client, environment, state, baseline per line.

One evidence tenant per CLIENT
------------------------------
Named `<partner>-<client>`, and each maps to exactly one Truveon tenant through
the ingest key stored under that name (docs/truveon-ingestion-contract.md,
"Credentials"). Per client rather than per application because the Truveon
tenant is per client -- B-023, decided 2026-09-23 -- and a platform tenant that
split a client across two names would need two keys for one Truveon tenant.

Client slugs are unique within a partner, not globally, so the partner is part
of the name. `direct` clients have no partner above them and file with a null
partner, which is what design 15's carve-out chain expects for them.

AltDigital's own accounts
-------------------------
Every platform account, and any registry account filed under the `altdigital`
partner, files under one internal tenant with no partner. They produce evidence
AltDigital's own SOC 2 needs, and there is nobody on the far end of the
carve-out chain for them.
"""
import json
import sys

INTERNAL_TENANT = "altdigital-platform"
INTERNAL_PARTNER = "altdigital"


def _platform_accounts(path):
    accounts = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            value = parts[-1].strip()
            if value.isdigit() and len(value) == 12:
                accounts.append(value)
    return accounts


def _registry_accounts(path):
    """(account_id, partner, client) per registry row, skipping devested."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            account_id, partner, client = parts[0], parts[1], parts[2]
            state = parts[4] if len(parts) > 4 else ""
            if not (account_id.isdigit() and len(account_id) == 12):
                continue
            # A devested account emits nothing the platform should file; it
            # keeps whatever it already delivered.
            if state == "devested":
                continue
            rows.append((account_id, partner, client))
    return rows


def build(platform_accounts, registry_rows):
    tenants = {INTERNAL_TENANT: {"partner": None, "accounts": set()}}
    owner = {}

    for account_id in platform_accounts:
        tenants[INTERNAL_TENANT]["accounts"].add(account_id)
        owner[account_id] = INTERNAL_TENANT

    for account_id, partner, client in registry_rows:
        if partner == INTERNAL_PARTNER:
            name, tenant_partner = INTERNAL_TENANT, None
        else:
            name = f"{partner}-{client}"
            tenant_partner = None if partner == "direct" else partner
        previous = owner.get(account_id)
        if previous and previous != name:
            # Loud. One account in two tenants would file its evidence in
            # whichever was written last, and the other tenant's stream would
            # simply look quiet.
            sys.exit(
                f"account {account_id} maps to both '{previous}' and '{name}'. "
                "Refusing to publish: its evidence would file against one of "
                "them arbitrarily. Fix the registry record.")
        tenants.setdefault(name, {"partner": tenant_partner,
                                  "accounts": set()})
        tenants[name]["accounts"].add(account_id)
        owner[account_id] = name

    return {"tenants": {
        name: {"partner": facts["partner"],
               "accounts": sorted(facts["accounts"])}
        for name, facts in sorted(tenants.items()) if facts["accounts"]}}


def main(platform_path, registry_path=None):
    platform = _platform_accounts(platform_path)
    if not platform:
        # Loud. An empty map is not a small map: it makes every record
        # unattributed, and the pipeline would spend the next month raising
        # findings about a problem that is really here.
        sys.exit(
            "no 12-digit account ids were found in the platform account list. "
            "Refusing to publish an empty tenant map -- every evidence record "
            "would file as unattributed.")
    registry = _registry_accounts(registry_path) if registry_path else []
    print(json.dumps(build(platform, registry), separators=(",", ":")))


if __name__ == "__main__":
    main(*sys.argv[1:3])
