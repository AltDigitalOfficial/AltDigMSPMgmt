#!/usr/bin/env python3
"""Write policies/scp/09-hipaa-eligible-only.json from the catalog.

    python scripts/generate-hipaa-scp.py [--check]

The policy is generated, never hand-edited (docs/hipaa-eligibility.md §1). A
human edits eligibility/hipaa-eligible-services.tsv; this writes the policy;
the git diff of the policy is the security review; validate-policies.sh,
the harness and deploy-policies.sh take it from there, exactly as for every
other SCP.

--check exits non-zero if the file on disk differs from what the catalog
produces, and writes nothing. Run it before deploying: a catalog edit that was
never regenerated would leave the deployed policy saying something the
catalog does not.

Platform-only principals come from PLATFORM_ONLY_PRINCIPALS below, empty while
no prefix is platform-only (E1 undecided). They are exact role names, checked
by eligibility/src/generate.py: never a pattern, never TenantAppDeployer.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eligibility" / "src"))
import catalog  # noqa: E402
import generate  # noqa: E402

CATALOG = ROOT / "eligibility" / "hipaa-eligible-services.tsv"
OUT = ROOT / "policies" / "scp" / generate.POLICY_FILE

# Exact role names allowed the platform-only prefixes (Statement B). Each entry
# needs a reason in docs/hipaa-eligibility.md §3 before it goes here.
PLATFORM_ONLY_PRINCIPALS = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    version, rows = catalog.load(CATALOG)
    problems = catalog.validate(rows)
    if problems:
        print("FAIL  the catalog does not validate:")
        for p in problems:
            print(f"      {p}")
        return 1

    doc = generate.build(rows, PLATFORM_ONLY_PRINCIPALS)
    size = generate.minified_size(doc)
    if size > generate.QUOTA:
        print(f"FAIL  {size} bytes minified, over the {generate.QUOTA}-byte SCP quota. "
              "See docs/hipaa-eligibility.md §4.1 (d) for the allow-list fallback.")
        return 1

    # Readable on disk, one entry per line, so the diff of a catalog change is
    # one line per prefix. deploy-policies.sh minifies on the wire.
    text = json.dumps(doc, indent=2) + "\n"
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    a, p, d = catalog.prefix_sets(rows)
    summary = (f"list_version {version}: {len(doc['Statement'][0]['NotAction'])} prefixes "
               f"permitted, {size} of {generate.QUOTA} bytes minified")
    if current == text:
        print(f"ok    {OUT.name} is current — {summary}")
        return 0
    if args.check:
        print(f"FAIL  {OUT.name} differs from what the catalog produces. "
              "Run scripts/generate-hipaa-scp.py and commit the result.")
        return 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"ok    wrote {OUT.name} — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
