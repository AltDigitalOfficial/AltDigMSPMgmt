"""Builds the evidence tenant map from the account registry.

A file rather than a heredoc inside sync-evidence-tenants.sh, because that
arrangement had the script on stdin AND the data on stdin, and the data lost.
The result was a valid, empty map -- every evidence record filing as
unattributed, silently.
"""
import json
import sys

# Build-phase reality, stated rather than dressed up.
#
# There are no client tenants yet. Every account in the registry is
# AltDigital's own platform estate, so all of it files under one internal
# tenant with no partner: design 15's carve-out chain has nobody on the far end
# of it until a client is onboarded.
#
# Not a placeholder that gets forgotten. Provisioning (phase 13) writes a
# tenant per client as accounts are vested and this reads them; the internal
# tenant keeps carrying the platform's own accounts, which still produce
# evidence that AltDigital's own SOC 2 needs.
INTERNAL_TENANT = "altdigital-platform"


def main(path):
    accounts = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            value = parts[-1].strip()
            if value.isdigit() and len(value) == 12:
                accounts.append(value)
    if not accounts:
        # Loud. An empty map is not a small map: it makes every record
        # unattributed, and the pipeline would spend the next month raising
        # findings about a problem that is really here.
        sys.exit(
            "no 12-digit account ids were found in the registry output. "
            "Refusing to publish an empty tenant map -- every evidence record "
            "would file as unattributed.")
    print(json.dumps(
        {"tenants": {INTERNAL_TENANT: {"partner": None,
                                       "accounts": sorted(set(accounts))}}},
        separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv[1])
