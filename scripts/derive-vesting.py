#!/usr/bin/env python3
"""Validate a completed onboarding questionnaire and compute its derived fields.

    scripts/derive-vesting.py --questionnaire path.yaml [--json]

Prompt 3.1: "The derived fields ([D] in the questionnaire) must be computed by
the pipeline, not supplied." This is that computation, split out from the
vesting pipeline so the derived values can be reviewed before anything is
created — which is also what the prompt's --dry-run requirement is for.

Separating it buys three things:

  * derivation is testable without an AWS account
  * a questionnaire can be reviewed and argued about before vesting
  * the rules live in vesting/derivation-rules.yaml as data, so changing a
    retention floor is a reviewable diff rather than a code change

------------------------------------------------------------------------------
Why this is Python and the rest of the repository is bash
------------------------------------------------------------------------------
The derivation is data manipulation over a nested document with cross-field
validation. In bash that means either a pile of `jq` invocations or string
surgery, and the failure mode of getting it wrong is an account vested with the
wrong retention floor — a mistake that Object Lock COMPLIANCE makes permanent
for six years.

Bash remains right for orchestrating AWS CLI calls. It is the wrong tool for
this specific job, and using it anyway to preserve language uniformity would be
a preference dressed up as a standard.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema is required: python -m pip install --user jsonschema")


# The Windows console defaults to cp1252, which renders every em-dash in this
# file's output as a replacement character. Forcing UTF-8 on the streams is
# cheaper than restricting the vocabulary of the messages, and these messages
# are read by someone deciding whether to vest three AWS accounts.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "vesting" / "questionnaire.schema.json"
RULES_PATH = REPO_ROOT / "vesting" / "derivation-rules.yaml"

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Halt(Exception):
    """A condition under which vesting must not proceed at all.

    Distinct from a validation error. A validation error means the form is
    wrong and can be corrected. A Halt means the answers are internally
    coherent and describe something this platform must not do.
    """


def load(path, loader):
    if not path.exists():
        sys.exit(f"Not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return loader(fh)


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# Cross-field checks
# ---------------------------------------------------------------------------
# Everything JSON Schema cannot express, or can express only in a form nobody
# would be able to read six months from now. Each returns a list of error
# strings rather than raising, so a reviewer sees every problem at once instead
# of fixing them one round-trip at a time.


def check_frameworks(q):
    fw = q["compliance"]["frameworks"]
    if "none" in fw and len(fw) > 1:
        return ["compliance.frameworks: 'none' cannot be combined with other frameworks."]
    return []


def check_phi_baa(q):
    """PHI without a notification window is the gap the incident clock hangs on."""
    c = q["compliance"]
    if c["phi"] and c.get("baa_window_partner_to_client_hours") is None:
        return [
            "compliance: phi is true but baa_window_partner_to_client_hours is null.\n"
            "    A BAA notification window is the input to the incident clock. Vesting an\n"
            "    account that holds PHI with no window means that at the moment of a breach\n"
            "    there is no defensible answer to 'by when'. Field 2.6 is owned by Art and\n"
            "    Wayne — this blocks until they supply it."
        ]
    if c["phi"] and "hipaa" not in c["frameworks"]:
        return [
            "compliance: phi is true but 'hipaa' is not in frameworks.\n"
            "    Possible, but almost always a form error. If deliberate, add 'other' and\n"
            "    record why in the vesting evidence."
        ]
    return []


def check_hoop(q):
    errs = []
    env = q["environments"]
    for name, h in env["hoop"].items():
        if h["always_on"]:
            continue
        for field in ("days", "start", "end"):
            if not h.get(field):
                errs.append(f"environments.hoop.{name}: always_on is false, so '{field}' is required.")
        if h.get("start") and h.get("end") and minutes(h["start"]) >= minutes(h["end"]):
            errs.append(
                f"environments.hoop.{name}: start {h['start']} is not before end {h['end']}.\n"
                "    Overnight HOOPs are not supported; split them or use always_on."
            )
    if env["uat_required"]:
        if "uat" not in env["hoop"]:
            errs.append("environments.hoop.uat is required when uat_required is true.")
        if not env.get("release_cadence"):
            errs.append("environments.release_cadence is required when uat_required is true (3a.2).")
    return errs


def check_maintenance_window(q):
    """4.5 — the window must fall INSIDE the HOOP, or patching never runs.

    This is the check most worth having. If the maintenance window sits outside
    the HOOP the instances are stopped when Patch Manager fires. Nothing errors.
    Patch compliance reports keep returning green against an instance that was
    switched off, and the gap is found during an audit or an incident.
    """
    errs = []
    mw = q["resilience"]["maintenance_window"]
    mw_start, mw_end = minutes(mw["start"]), minutes(mw["end"])

    for name, h in q["environments"]["hoop"].items():
        if h["always_on"]:
            continue
        h_start, h_end = minutes(h["start"]), minutes(h["end"])
        if not (h_start <= mw_start and mw_end <= h_end):
            errs.append(
                f"resilience.maintenance_window ({mw['start']}-{mw['end']}) falls outside the "
                f"{name} HOOP ({h['start']}-{h['end']}).\n"
                "    The instances are stopped when Patch Manager runs, so patching silently\n"
                "    never happens and compliance reporting stays green against a powered-off\n"
                "    host. Move the window inside the HOOP, or set that environment always_on."
            )
        missing = [d for d in mw["days"] if d not in h.get("days", [])]
        if missing:
            errs.append(
                f"resilience.maintenance_window runs on {', '.join(missing)}, which are not "
                f"{name} HOOP days ({', '.join(h.get('days', []))}). Same failure: patching never runs."
            )
    return errs


def check_dedicated_infrastructure(q):
    s = q["security"]
    if s["dedicated_infrastructure_required"] and not s.get("dedicated_infrastructure_driver"):
        return [
            "security.dedicated_infrastructure_driver is required when "
            "dedicated_infrastructure_required is true (5.9a).\n"
            "    Contract clause, framework control or procurement policy — the answer\n"
            "    determines whether it is negotiable. All three are legitimate; only one is\n"
            "    a security argument."
        ]
    if s.get("dedicated_infrastructure_driver") == "framework-control" and not s.get("dedicated_infrastructure_control_ref"):
        return [
            "security.dedicated_infrastructure_control_ref is required when the driver is a "
            "framework control (5.9b).\n"
            "    Frameworks are cited far more often than they are read. None of HIPAA,\n"
            "    PCI-DSS or SOC 2 requires dedicated physical hardware, and PCI-DSS\n"
            "    explicitly contemplates shared hosting with appropriate controls. Ask which\n"
            "    control; Art can confirm whether it says what the client believes."
        ]
    return []


def check_residency(q):
    if q["compliance"]["non_us_data_subjects"]:
        raise Halt(
            "compliance.non_us_data_subjects is true (question 2.2).\n\n"
            "    Vesting stops here. This is not a configuration problem.\n\n"
            "    2.2 asks about DATA SUBJECTS, not customer location, and a 'yes' means the\n"
            "    region choice carries a residency and cross-border transfer position — GDPR\n"
            "    Article 44 onward, or the UK equivalent. That is a legal determination and\n"
            "    the pipeline must not make it by picking a region.\n\n"
            "    The platform's allowed regions are us-east-1, us-east-2 and us-west-2. All\n"
            "    are US. There is no correct automatic answer available.\n\n"
            "    Escalate to Art. If the customer needs an EU or UK region, that is a\n"
            "    platform expansion decision with its own controls, not a parameter."
        )
    return []


CHECKS = [
    check_frameworks,
    check_phi_baa,
    check_hoop,
    check_maintenance_window,
    check_dedicated_infrastructure,
    check_residency,
]


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive(q, rules):
    d = {}
    c = q["compliance"]
    fw = [f for f in c["frameworks"] if f != "none"]

    # 2.7 — conformance packs. Union across frameworks, order-stable.
    packs = []
    for f in fw:
        for p in rules["conformance_packs"].get(f, []):
            if p not in packs:
                packs.append(p)
    d["conformance_packs"] = packs

    # 2.8 — retention floor. "Union of applicable frameworks" means the
    # LONGEST, not the sum. Sum would be arithmetic on unrelated obligations.
    floors = {f: rules["retention_floor_days"][f] for f in fw if f in rules["retention_floor_days"]}
    d["retention_floor_days"] = max(floors.values()) if floors else rules["default"]
    d["retention_floor_driver"] = (
        max(floors, key=floors.get) if floors else "platform-default"
    )

    # 3.1 — region. check_residency has already halted on the other branch.
    d["primary_region"] = rules["primary_region"]["default"]

    # 7.7 — monitoring tier, per environment.
    envs = list(rules["environments_always"])
    if q["environments"]["uat_required"]:
        envs += rules["environments_optional"]
    d["environments"] = envs
    d["monitoring_tier"] = {e: rules["monitoring_tier"][e] for e in envs}
    d["pages_out_of_hours"] = {e: rules["pages_out_of_hours"][e] for e in envs}

    # 6.4 — dedicated host, from LICENCES not from security posture.
    held = set(q["licensing"]["customer_owned_licences"])
    triggers = sorted(held & set(rules["dedicated_host_required_for_licences"]))
    d["dedicated_host_required"] = bool(triggers)
    d["dedicated_host_driver"] = triggers or None

    # 2.3 -> egress default, and whether the customer overrode it.
    default_egress = rules["egress_profile_default"].get(
        c["data_classification"], rules["default_egress_profile"]
    )
    chosen = q["network"]["egress_profile"]
    d["egress_profile_default"] = default_egress
    d["egress_profile"] = chosen
    d["egress_profile_overridden"] = chosen != default_egress

    # 3a.9 — not asked. UAT is production-equivalent for data handling because
    # real users bring real data.
    d["uat_compliance_posture"] = "production-equivalent" if q["environments"]["uat_required"] else None

    # 5.5 — quorum is a platform property, not a customer answer.
    d["key_deletion_quorum"] = "any two of Jamie, Wayne, Art"

    # Account naming, derived so it cannot disagree with the scripts.
    partner = q["identity"]["partner"]
    client = q["identity"]["client_slug"]
    app = q["identity"]["application_code"]
    tier = {"dev": "d", "test": "t", "uat": "u", "prod": "p"}
    d["accounts"] = [
        {
            "environment": e,
            "alias": f"altdig-{partner}-{client}-{app}-{e}",
            "root_email": f"mspr+{partner}-{client}-{app}-{tier[e]}@altdigital.ai",
        }
        for e in envs
    ]

    return d


def warnings_for(q, d):
    """Things that do not block but that a reviewer should see before approving."""
    w = []
    if d["egress_profile"] == "locked":
        w.append(
            "egress_profile 'locked' is NOT IMPLEMENTED (B-010). Vesting will fail at the\n"
            "    network step, after the accounts exist. Choose 'isolated' or 'dns-filtered',\n"
            "    or implement B-010 first. Note that 'isolated' is both cheaper and MORE\n"
            "    restrictive than 'locked' — see docs/isolation-tiers.md."
        )
    if d["egress_profile_overridden"]:
        w.append(
            f"egress_profile '{d['egress_profile']}' differs from the '{d['egress_profile_default']}'\n"
            f"    derived from data_classification '{q['compliance']['data_classification']}'.\n"
            "    Legitimate — 3.2 is the customer's to answer — but it is a declared override\n"
            "    and belongs in the registry with a reason and an owner."
        )
    if d["retention_floor_days"] >= 2190:
        w.append(
            f"retention_floor_days is {d['retention_floor_days']} ({d['retention_floor_days'] // 365}"
            " years), driven by "
            f"'{d['retention_floor_driver']}'.\n"
            "    This becomes Object Lock COMPLIANCE retention: no principal can delete those\n"
            "    objects for the full period, including account root and AWS Support. It is\n"
            "    also an unbreakable commitment to storage cost for the same period."
        )
    if q["security"]["external_key_material"]:
        w.append(
            "external_key_material (BYOK) creates an EXTERNAL-origin KMS key. AWS cannot\n"
            "    rotate it and cannot recover it. If the customer loses the key material,\n"
            "    the data encrypted under it is unrecoverable by anyone."
        )
    if q["security"]["dedicated_infrastructure_required"]:
        w.append(
            f"dedicated infrastructure requested, driver "
            f"'{q['security']['dedicated_infrastructure_driver']}'.\n"
            "    Price it explicitly. A Network Firewall per account is roughly $350/month\n"
            "    single-AZ because this platform forbids a shared inspection VPC. Do not\n"
            "    absorb it — docs/isolation-tiers.md."
        )
    if not d["conformance_packs"]:
        w.append(
            "No conformance packs derived. Either frameworks is ['none'] or only 'other' was\n"
            "    named. The account still gets the full baseline; it just gets no\n"
            "    framework-specific Config rules, which is worth confirming rather than\n"
            "    discovering at audit."
        )
    return w


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questionnaire", required=True, help="Completed questionnaire, YAML or JSON.")
    ap.add_argument("--json", action="store_true", help="Emit derived values as JSON for the pipeline.")
    args = ap.parse_args()

    schema = load(SCHEMA_PATH, json.load)
    rules = load(RULES_PATH, yaml.safe_load)
    q = load(Path(args.questionnaire), yaml.safe_load)

    # Schema first. Cross-field checks assume the shape is right, and running
    # them against a malformed document produces KeyErrors instead of advice.
    errors = sorted(Draft202012Validator(schema).iter_errors(q), key=lambda e: list(e.path))
    if errors:
        print("Questionnaire failed schema validation:\n", file=sys.stderr)
        for e in errors:
            loc = ".".join(str(p) for p in e.path) or "(root)"
            print(f"  {loc}: {e.message}", file=sys.stderr)
        sys.exit(1)

    problems = []
    try:
        for check in CHECKS:
            problems.extend(check(q))
    except Halt as h:
        print(f"\nHALT — vesting must not proceed.\n\n    {h}\n", file=sys.stderr)
        sys.exit(2)

    if problems:
        print("Questionnaire is well-formed but not vestable:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        sys.exit(1)

    d = derive(q, rules)

    if args.json:
        print(json.dumps(d, indent=2))
        return

    ident = q["identity"]
    print("-" * 70)
    print(f"Derived vesting parameters — {ident['partner']}/{ident['client_slug']}/{ident['application_code']}")
    print("-" * 70)
    print(f"  primary region        : {d['primary_region']}")
    print(f"  environments          : {', '.join(d['environments'])}")
    print(f"  retention floor       : {d['retention_floor_days']} days  (driver: {d['retention_floor_driver']})")
    print(f"  conformance packs     : {', '.join(d['conformance_packs']) or '(none)'}")
    print(f"  egress profile        : {d['egress_profile']}"
          f"{'  [OVERRIDES ' + d['egress_profile_default'] + ']' if d['egress_profile_overridden'] else '  (derived)'}")
    print(f"  dedicated host        : {d['dedicated_host_required']}"
          f"{'  (' + ', '.join(d['dedicated_host_driver']) + ')' if d['dedicated_host_driver'] else ''}")
    print(f"  UAT posture           : {d['uat_compliance_posture'] or 'n/a — no UAT'}")
    print(f"  key deletion quorum   : {d['key_deletion_quorum']}")
    print()
    print("  Accounts to be vested:")
    for a in d["accounts"]:
        print(f"    {a['environment']:5s}  {a['alias']:45s}  {a['root_email']}")
    print()
    print("  Monitoring tier:")
    for e in d["environments"]:
        pages = "pages out of hours" if d["pages_out_of_hours"][e] else "no out-of-hours paging"
        print(f"    {e:5s}  {d['monitoring_tier'][e]:22s}  {pages}")

    w = warnings_for(q, d)
    if w:
        print()
        print("-" * 70)
        print("  Review before approving:")
        print("-" * 70)
        for item in w:
            print(f"  ! {item}\n")
    print("-" * 70)


if __name__ == "__main__":
    main()
