#!/usr/bin/env python3
"""Which platform roles hold an action each SCP statement denies.

    python scripts/derive-scp-exemptions.py [--fetch-managed] [--json]

Every SCP statement exempts a list of principals. The list should hold only
the roles that actually need to perform the denied action (B-135). Anything
longer is an exemption nobody uses, and anything shorter breaks platform
automation in a way nothing reports. This computes the list from the
templates rather than from memory.

For each IAM role defined in a CloudFormation template under this repository,
it collects the actions the role is ALLOWED: inline Policies, AWS::IAM::Policy
and AWS::IAM::ManagedPolicy resources that reference the role, and
ManagedPolicyArns. It then matches them against each Deny statement in
policies/scp/01-07. A role appears under a statement if any action it holds
overlaps any action the statement denies, wildcards included in both
directions.

What it cannot see, and says so:
  * roles created outside templates (the harness roles, OrganizationAccount-
    AccessRole, stacksets-exec-*, Identity Center roles). Listed separately as
    deployment/harness principals, which are decided by hand.
  * AWS-managed policies, unless --fetch-managed reads them from IAM
    (read-only, from whatever account the default credentials reach).
  * statement conditions. A role that holds ec2:CreateTags is listed under the
    tag-tamper deny even if it never writes a protected key. So the output is
    the CANDIDATE list, and each entry still needs a reason in the review.
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    import cfnlint.decode
except ImportError:
    sys.exit("cfn-lint is required: python -m pip install --user cfn-lint")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"design", "estate", ".git", "node_modules"}


def as_list(x):
    return x if isinstance(x, list) else [x]


def role_name_pattern(props, logical_id):
    """RoleName as a glob. !Sub '${X}' pieces become '*'."""
    rn = props.get("RoleName")
    if rn is None:
        return f"<no RoleName: {logical_id}>"
    if isinstance(rn, str):
        return rn
    if isinstance(rn, dict) and "Fn::Sub" in rn:
        s = rn["Fn::Sub"]
        s = s[0] if isinstance(s, list) else s
        return re.sub(r"\$\{[^}]+\}", "*", s)
    return f"<unresolved RoleName: {logical_id}>"


def actions_of(doc):
    """Allowed actions in a policy document. NotAction allows are returned as
    '*' with a marker, since they allow almost everything."""
    out = set()
    if not isinstance(doc, dict):
        return out
    for st in as_list(doc.get("Statement", [])):
        if not isinstance(st, dict) or st.get("Effect") != "Allow":
            continue
        if "Action" in st:
            for a in as_list(st["Action"]):
                if isinstance(a, str):
                    out.add(a.lower())
        elif "NotAction" in st:
            out.add("*  (NotAction allow)")
    return out


def refs(v):
    """Logical ids a Roles list refers to via !Ref."""
    out = []
    for r in as_list(v or []):
        if isinstance(r, dict) and "Ref" in r:
            out.append(r["Ref"])
    return out


def load_templates():
    for p in sorted(REPO.rglob("*.yaml")):
        if SKIP_DIRS & set(p.relative_to(REPO).parts):
            continue
        tpl, errs = cfnlint.decode.decode(str(p))
        if not isinstance(tpl, dict) or "Resources" not in tpl:
            continue
        yield p.relative_to(REPO).as_posix(), tpl


def fetch_managed(arn, cache):
    if arn in cache:
        return cache[arn]
    acts = set()
    try:
        v = subprocess.run(["aws", "iam", "get-policy", "--policy-arn", arn,
                            "--query", "Policy.DefaultVersionId", "--output", "text"],
                           capture_output=True, text=True, check=True).stdout.strip()
        doc = subprocess.run(["aws", "iam", "get-policy-version", "--policy-arn", arn,
                              "--version-id", v, "--query", "PolicyVersion.Document",
                              "--output", "json"],
                             capture_output=True, text=True, check=True).stdout
        acts = actions_of(json.loads(doc))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        acts = {f"<could not read {arn}>"}
    cache[arn] = acts
    return acts


def collect_roles(fetch):
    roles = {}              # role pattern -> {"files": set, "actions": set, "unread": set}
    cache = {}
    for rel, tpl in load_templates():
        res = tpl["Resources"]
        by_logical = {}
        for lid, r in res.items():
            if isinstance(r, dict) and r.get("Type") == "AWS::IAM::Role":
                props = r.get("Properties", {}) or {}
                name = role_name_pattern(props, lid)
                ent = roles.setdefault(name, {"files": set(), "actions": set(), "unread": set()})
                ent["files"].add(rel)
                by_logical[lid] = ent
                for pol in as_list(props.get("Policies", []) or []):
                    if isinstance(pol, dict):
                        ent["actions"] |= actions_of(pol.get("PolicyDocument"))
                for arn in as_list(props.get("ManagedPolicyArns", []) or []):
                    # !Sub 'arn:${AWS::Partition}:iam::aws:policy/...' is how
                    # most templates here write it; the partition is always aws.
                    if isinstance(arn, dict) and isinstance(arn.get("Fn::Sub"), str):
                        arn = arn["Fn::Sub"].replace("${AWS::Partition}", "aws")
                    if isinstance(arn, str) and arn.startswith("arn:aws:iam::aws:policy/"):
                        if arn.endswith("/AdministratorAccess"):
                            ent["actions"].add("*")
                        elif fetch:
                            ent["actions"] |= fetch_managed(arn, cache)
                        else:
                            ent["unread"].add(arn.rsplit("/", 1)[-1])
                    else:
                        ent["unread"].add(json.dumps(arn) if not isinstance(arn, str) else arn)
        for lid, r in res.items():
            if not isinstance(r, dict):
                continue
            if r.get("Type") in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                props = r.get("Properties", {}) or {}
                for rl in refs(props.get("Roles")):
                    if rl in by_logical:
                        by_logical[rl]["actions"] |= actions_of(props.get("PolicyDocument"))
    return roles


def overlaps(a, b):
    a, b = a.lower(), b.lower()
    return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-managed", action="store_true",
                    help="Read AWS-managed policy documents from IAM (read-only).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roles = collect_roles(args.fetch_managed)
    result = {}
    for f in sorted((REPO / "policies" / "scp").glob("0[1-7]-*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        for st in doc["Statement"]:
            if st.get("Effect") != "Deny":
                continue
            key = f"{f.name.split('-')[0]}/{st.get('Sid', '?')}"
            exempts = (st.get("Condition", {}) or {}).get("ArnNotLike", {}).get("aws:PrincipalArn")
            if not exempts:
                result[key] = "no exemption list: applies to every principal it matches"
                continue
            if "NotAction" in st:
                result[key] = ("condition-scoped (NotAction): an exemption is needed only by a "
                               "principal that meets the condition, which actions cannot show")
                continue
            denied = [a.lower() for a in as_list(st.get("Action", []))]
            hits = {}
            for name, ent in roles.items():
                m = sorted({d for d in denied for a in ent["actions"] if overlaps(d, a.split()[0])})
                if m:
                    hits[name] = m
            result[key] = hits

    if args.json:
        print(json.dumps({"statements": result,
                          "roles": {k: {"files": sorted(v["files"]),
                                        "unread_managed": sorted(v["unread"])}
                                    for k, v in roles.items()}}, indent=2))
        return

    for key, hits in result.items():
        print(f"\n{key}")
        if isinstance(hits, str):
            print(f"    ({hits})")
            continue
        if not hits:
            print("    (no template role holds a denied action)")
        for name, m in sorted(hits.items()):
            shown = ", ".join(m[:4]) + (f", +{len(m) - 4}" if len(m) > 4 else "")
            print(f"    {name:40} {shown}")
    unread = {k: v["unread"] for k, v in roles.items() if v["unread"]}
    if unread:
        print("\nManaged policies NOT read (rerun with --fetch-managed):")
        for k, v in sorted(unread.items()):
            print(f"    {k:40} {', '.join(sorted(v))}")


if __name__ == "__main__":
    main()
