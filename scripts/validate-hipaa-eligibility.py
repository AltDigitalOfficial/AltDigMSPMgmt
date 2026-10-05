#!/usr/bin/env python3
"""Check eligibility/hipaa-eligible-services.tsv before anything consumes it.

    python scripts/validate-hipaa-eligibility.py [--offline] [--page FILE]

Three layers of check, each catching a different way the file can be wrong:

  1. Structure (always). Statuses, duplicates, prefixes allowlisted and denied
     at once, eligible rows that are not on the AWS list, names where an opaque
     id belongs. eligibility/src/catalog.py holds the rules.

  2. Prefixes exist (default; skipped with --offline). Every IAM prefix is
     checked against AWS's machine-readable Service Authorization Reference.
     A typo in an allowlist denies a service in prod; a typo in a denylist
     permits one. Neither produces an error anywhere else.

  3. Page parity (with --page). The rows marked on_aws_list=yes are compared,
     verbatim after normalisation, with the service list in a saved copy of the
     AWS HIPAA Eligible Services Reference page. This is the same comparison
     the weekly upstream check runs; here it is run by a human who is editing
     the file and wants to know the edit is complete.

Exits non-zero on any problem. A fetch failure in layer 2 is a failure, not a
skip: "could not check" must not read as "checked and fine".
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eligibility" / "src"))
import catalog  # noqa: E402

CATALOG = ROOT / "eligibility" / "hipaa-eligible-services.tsv"
SERVICE_REFERENCE = "https://servicereference.us-east-1.amazonaws.com/"


def known_prefixes():
    with urllib.request.urlopen(SERVICE_REFERENCE, timeout=30) as resp:
        data = json.load(resp)
    prefixes = {entry["service"] for entry in data}
    # A truncated or changed response would make every prefix look unknown,
    # or worse, an empty set would be read as "nothing to check against".
    if len(prefixes) < 300:
        raise RuntimeError(f"Service Authorization Reference returned only {len(prefixes)} "
                           "prefixes; refusing to validate against it")
    return prefixes


def page_list(path):
    """Service names from a saved copy of the AWS page.

    The same parser the weekly upstream check uses on the live page
    (catalog.parse_page), so a parity pass here means the check will agree.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    _, names = catalog.parse_page(text)
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip the Service Authorization Reference check")
    ap.add_argument("--page", help="saved HTML of the AWS reference page, for a parity check")
    args = ap.parse_args()

    try:
        version, rows = catalog.load(CATALOG)
    except catalog.CatalogError as exc:
        print(f"FAIL  {CATALOG.name}: {exc}")
        return 1

    known = None
    if not args.offline:
        try:
            known = known_prefixes()
        except Exception as exc:  # noqa: BLE001 — any failure here is a failed check
            print(f"FAIL  could not read the Service Authorization Reference: {exc}")
            print("      Re-run with --offline to check structure only, and say so in the PR.")
            return 1

    problems = catalog.validate(rows, known)

    if args.page:
        page = page_list(args.page)
        mine = catalog.page_services(rows)
        for name in page:
            if name not in mine:
                problems.append(f"on the AWS page, not in the file: {name}")
        for name in sorted(mine):
            if name not in page:
                problems.append(f"in the file as on_aws_list=yes, not on the AWS page: {name}")

    allow, platform, denied = catalog.prefix_sets(rows)
    pending_review = sum(1 for r in rows if r["reviewed_by"] == "pending")
    print(f"list_version {version}: {len(rows)} rows, {len(allow)} allowlisted prefixes, "
          f"{len(platform)} platform-only, {len(denied)} denied")
    if pending_review:
        print(f"note  {pending_review} rows not yet reviewed by a human (reviewed_by=pending)")

    for p in problems:
        print(f"FAIL  {p}")
    if problems:
        return 1
    print("ok    " + ("structure" if known is None else "structure and prefixes")
          + (" and page parity" if args.page else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
