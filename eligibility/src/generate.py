"""The HIPAA eligibility SCP, built from the catalog. Pure.

docs/hipaa-eligibility.md §3 and §4.1. B-160.

    Statement A  DenyNonHipaaEligibleServices
                 Deny NotAction [eligible + carve-out + platform-only], every
                 principal. NO exemption, not even OrganizationAccountAccessRole
                 or stacksets-exec-*: an exemption would hand a principal every
                 non-eligible service, which is far broader than any need, and
                 would let AltDigital's own automation put PHI where the BAA
                 does not reach.

    Statement B  DenyPlatformOnlyServices (only if any prefix is platform-only)
                 Deny Action [platform-only], except named platform principals
                 in the B-135 v2 shape: exact arn:aws:iam::*:role/<Name>.
                 TenantAppDeployer-* may never be among them (B-136).

    Statement C+ DenyHipaaExcludedFeatures[...]
                 Feature-level exclusions from the page that map to actions:
                 Simple AD, voice messages, public and vendor workforces.

Nothing here reads the network or the environment. The output is a document
that scripts/generate-hipaa-scp.py writes to policies/scp/, where it is
validated, reviewed as a diff and deployed like every other SCP. Generated,
never hand-edited: the catalog is the thing a human edits.
"""

import json
import re

import catalog

POLICY_FILE = "09-hipaa-eligible-only.json"
QUOTA = 5120
FORBIDDEN_EXEMPTIONS = re.compile(r"TenantAppDeployer|\*")


def _sid(text):
    return re.sub(r"[^A-Za-z0-9]", "", text)


def build(rows, platform_principals=(), extra_prefixes=()):
    """The policy document as a dict.

    extra_prefixes is how an exception variant (B-162) differs from the base:
    the same document with approved prefixes added to Statement A's NotAction.
    Nothing else differs, so the review of a variant is the review of exactly
    those prefixes.
    """
    for p in platform_principals:
        if FORBIDDEN_EXEMPTIONS.search(p):
            raise ValueError(f"{p!r} may not be exempt from the HIPAA eligibility SCP. "
                             "Exact platform role names only; never TenantAppDeployer (B-136)")
    allow, platform_only, _ = catalog.prefix_sets(rows)
    not_action = sorted(set(allow) | set(platform_only) | set(extra_prefixes))
    statements = [{
        "Sid": "DenyNonHipaaEligibleServices",
        "Effect": "Deny",
        "NotAction": [f"{p}:*" for p in not_action],
        "Resource": "*",
    }]
    if platform_only:
        if not platform_principals:
            raise ValueError("platform-only prefixes exist but no platform principal is named")
        statements.append({
            "Sid": "DenyPlatformOnlyServices",
            "Effect": "Deny",
            "Action": [f"{p}:*" for p in platform_only],
            "Resource": "*",
            "Condition": {"ArnNotLike": {"aws:PrincipalArn": [
                f"arn:aws:iam::*:role/{n}" for n in sorted(platform_principals)]}},
        })
    unconditional, conditional = [], []
    for d in catalog.deny_entries(rows):
        (conditional if d["key"] else unconditional).append(d)
    if unconditional:
        statements.append({
            "Sid": "DenyHipaaExcludedFeatures",
            "Effect": "Deny",
            "Action": sorted({a for d in unconditional for a in d["actions"]}),
            "Resource": "*",
        })
    for d in conditional:
        statements.append({
            "Sid": "DenyHipaaExcludedFeaturesWhen" + _sid(d["key"].split(":")[-1]),
            "Effect": "Deny",
            "Action": sorted(d["actions"]),
            "Resource": "*",
            "Condition": {"StringEquals": {d["key"]: d["values"]}},
        })
    return {"Version": "2012-10-17", "Statement": statements}


def minified_size(doc):
    """What AWS counts against the quota: deploy-policies.sh sends it minified."""
    return len(json.dumps(doc, separators=(",", ":")))
