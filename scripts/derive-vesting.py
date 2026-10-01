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
import datetime
import json
import sys
import zoneinfo
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


def check_cardholder_scope(q):
    """Any PCI answer halts vesting until the platform has a CDE design (D-025).

    The platform has no cardholder data environment: no CDE OU, no `locked`
    egress profile (B-010), and no position on what a CDE in Members does to
    the PCI scope of the shared Security and Infrastructure accounts. Vesting a
    PCI tenant would put it on a platform that cannot meet the requirement, and
    the gap would be found by its assessor.

    Either answer halts, not only cardholder_data. 'pci-dss' with
    cardholder_data false is the processor-hosted case (SAQ A): the tenant never
    touches a card number and the platform's scope is arguably unaffected. It
    halts anyway, deliberately, because the platform has no process for
    deciding "arguably" and the founders chose a hard stop. The message names
    that case, so the answer is escalated rather than changed to get past the
    gate. If SAQ A tenants become common, this is the line D-025's close
    condition revisits.

    A Halt and not an error: the answers are coherent, and describe something
    this platform must not yet do. Same stance as check_residency.
    """
    c = q["compliance"]
    cardholder, pci = c["cardholder_data"], "pci-dss" in c["frameworks"]
    if not (cardholder or pci):
        return []
    declared = " and ".join(
        s for s, on in (("cardholder_data is true", cardholder),
                        ("'pci-dss' is in frameworks", pci)) if on)
    raise Halt(
        f"compliance: {declared} (questions 2.1 / 2.4).\n\n"
        "    Vesting stops here. The platform has no cardholder data environment\n"
        "    design, and PCI tenants are out of scope until it does (D-025).\n\n"
        "    Vesting a PCI tenant needs a founder decision on four things first:\n"
        "      1. a CDE design for the platform\n"
        "      2. a dedicated OU for CDE accounts, with its own SCP set\n"
        "      3. the 'locked' egress profile, which is not built (B-010)\n"
        "      4. the PCI scope impact on the shared Security and Infrastructure\n"
        "         accounts, which every tenant's logs and findings pass through\n\n"
        "    If the tenant never stores, processes or transmits card numbers and a\n"
        "    processor carries that scope (SAQ A), that still halts. Do not change\n"
        "    the answer to get past this gate. Escalate it to the founders with the\n"
        "    processor's name and integration method; it is the case D-025 expects\n"
        "    to revisit first."
    )


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
                "    A HOOP cannot cross midnight; the scheduler cannot run one. If these are\n"
                "    developers' hours converted into another timezone, give them in the\n"
                "    developers' own timezone instead (hoop.<env>.timezone) and the crossing\n"
                "    usually disappears. Otherwise split them, or use always_on."
            )
    if env["uat_required"]:
        if "uat" not in env["hoop"]:
            errs.append("environments.hoop.uat is required when uat_required is true.")
        if not env.get("release_cadence"):
            errs.append("environments.release_cadence is required when uat_required is true (3a.2).")
    return errs


# ---------------------------------------------------------------------------
# Schedules across timezones
# ---------------------------------------------------------------------------
# Every schedule may name its own timezone, falling back to
# environments.timezone. A HOOP is usually in the developers' zone and a
# production window in the users'. They are compared as UTC intervals rather
# than as clock times, because clock times in two zones cannot be compared at
# all, and they are compared twice -- once per daylight-saving state -- because
# the offset between two zones is not constant when only one of them changes
# its clocks.


def tz_name(schedule, q):
    return (schedule or {}).get("timezone") or q["environments"]["timezone"]


def hhmm(total):
    total %= 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def check_timezones(q):
    """Every named zone must resolve. The schema checks shape; this checks existence."""
    named = {"environments.timezone": q["environments"]["timezone"]}
    for env, h in q["environments"]["hoop"].items():
        if h.get("timezone"):
            named[f"environments.hoop.{env}.timezone"] = h["timezone"]
    for which, w in q["resilience"]["maintenance_windows"].items():
        if isinstance(w, dict) and w.get("timezone"):
            named[f"resilience.maintenance_windows.{which}.timezone"] = w["timezone"]

    errs = []
    for where, name in named.items():
        try:
            zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            errs.append(
                f"{where}: '{name}' is not a timezone this machine can resolve.\n"
                "    If every zone fails, the timezone database is missing: on Windows,\n"
                "    python -m pip install --user tzdata (scripts/setup-tooling.sh checks it)."
            )
    return errs


def check_maintenance_windows(q):
    """What the schema cannot say about 5.6 / 5.7."""
    errs = []
    env = q["environments"]
    for which, w in q["resilience"]["maintenance_windows"].items():
        if isinstance(w, dict) and w["start"] == w["end"]:
            errs.append(
                f"resilience.maintenance_windows.{which}: start and end are both {w['start']}.\n"
                "    That could mean no time at all or the whole day. Give the real end."
            )

    choice = q["resilience"]["maintenance_windows"]["nonprod"]
    if isinstance(choice, str):
        for name, h in env["hoop"].items():
            if h["always_on"]:
                errs.append(
                    f"resilience.maintenance_windows.nonprod is '{choice}', but {name} is always on.\n"
                    "    An always-on environment has no developer hours to be inside or outside\n"
                    "    of, so there is nothing to compute the window from. Give a window for\n"
                    "    5.7 instead."
                )
    return errs


def occurrences(days, start, end, tz, monday):
    """UTC (start, end, start-day) for a schedule over the week beginning `monday`.

    An end at or before the start crosses midnight and finishes the next day;
    `days` are the days it starts. Local times are resolved in `tz` on each
    date separately, which is what makes a daylight-saving change visible.
    """
    zone = zoneinfo.ZoneInfo(tz)
    s_t = datetime.time.fromisoformat(start)
    e_t = datetime.time.fromisoformat(end)
    out = []
    for offset in range(7):
        date = monday + datetime.timedelta(days=offset)
        day = DAY_ORDER[date.weekday()]
        if day not in days:
            continue
        s = datetime.datetime.combine(date, s_t, tzinfo=zone)
        e_date = date + datetime.timedelta(days=1) if e_t <= s_t else date
        e = datetime.datetime.combine(e_date, e_t, tzinfo=zone)
        out.append((s.astimezone(datetime.timezone.utc),
                    e.astimezone(datetime.timezone.utc), day))
    return out


def uncovered(window, hoop, q, rules):
    """Which occurrences of `window` fall outside `hoop`, per sample week.

    Returns {season: [start-day, ...]} holding only the seasons with gaps. The
    HOOP is laid out over the week either side as well, because a window in
    another zone can land on a different UTC date from the HOOP it sits in.
    """
    gaps = {}
    for season, iso in rules["schedule_sample_weeks"].items():
        monday = datetime.date.fromisoformat(iso)
        hoop_occ = []
        for shift in (-7, 0, 7):
            hoop_occ += occurrences(hoop["days"], hoop["start"], hoop["end"], tz_name(hoop, q),
                                    monday + datetime.timedelta(days=shift))
        missed = [day for ws, we, day in occurrences(window["days"], window["start"],
                                                     window["end"], tz_name(window, q), monday)
                  if not any(hs <= ws and we <= he for hs, he, _ in hoop_occ)]
        if missed:
            gaps[season] = missed
    return gaps


def describe_gaps(gaps):
    seasons = list(gaps.values())
    if len(gaps) > 1 and all(s == seasons[0] for s in seasons):
        return f"on {', '.join(seasons[0])}, all year"
    return "; ".join(f"in {season.capitalize()} on {', '.join(days)}"
                     for season, days in gaps.items())


def derive_nonprod_window(choice, hoop, q, rules):
    """A window computed from a HOOP, for the 5.7 choices. Same zone, same days."""
    rule = rules["nonprod_maintenance"][choice]
    start_total = minutes(hoop["end"]) + rule["offset_from_hoop_end_minutes"]
    # A start pushed past midnight starts on the next day, so the days move too.
    day_shift = start_total // 1440
    days = [DAY_ORDER[(DAY_ORDER.index(d) + day_shift) % 7] for d in hoop["days"]]
    return {
        "days": sorted(days, key=DAY_ORDER.index),
        "start": hhmm(start_total),
        "end": hhmm(start_total + rule["duration_minutes"]),
        "timezone": tz_name(hoop, q),
    }


def derive_maintenance_windows(q, rules, envs):
    """The window each environment will actually use, and whether it must be woken for it.

    `wake_required` is the B-141 condition: the window falls outside that
    environment's HOOP, so the environment is switched off when the window
    opens. Nothing patches yet, so it is reported as a warning. Once patching
    exists it is only safe if the wake exists too.
    """
    mw = q["resilience"]["maintenance_windows"]
    hoops = q["environments"]["hoop"]
    out = {"prod": {**mw["prod"], "timezone": tz_name(mw["prod"], q),
                    "source": "specified", "wake_required": False, "gaps": {}}}
    for env in envs:
        if env == "prod":
            continue
        h = hoops[env]
        if isinstance(mw["nonprod"], str):
            window = derive_nonprod_window(mw["nonprod"], h, q, rules)
            source = f"derived: {mw['nonprod']}"
        else:
            window, source = dict(mw["nonprod"]), "specified"
        window["timezone"] = tz_name(window, q)
        gaps = {} if h["always_on"] else uncovered(window, h, q, rules)
        out[env] = {**window, "source": source, "wake_required": bool(gaps), "gaps": gaps}
    return out


def check_ticketing(q):
    """Client form 12.4: the fields each ticketing system needs, and no others."""
    t = q["change"]["ticketing"]
    system = t["system"]
    errs = []
    if system == "none":
        extra = [k for k in ("product", "site", "project_key") if t.get(k)]
        if extra:
            errs.append(f"change.ticketing: system is 'none' but {', '.join(extra)} "
                        "given. Either name the system, or remove them.")
        return errs
    for field in ("product", "project_key"):
        if not t.get(field):
            errs.append(f"change.ticketing.{field} is required when system is '{system}'.")
    if system == "client-jira" and not t.get("site"):
        errs.append(
            "change.ticketing.site is required when system is 'client-jira'.\n"
            "    The client's Atlassian site, https://<name>.atlassian.net. Reconciliation\n"
            "    reads change records there with a read-only service account the client\n"
            "    creates (scripts/set-jira-token.sh --tenant <partner>-<client>).")
    if system == "altdigital-jira" and t.get("site"):
        errs.append("change.ticketing.site is given but system is 'altdigital-jira', "
                    "which is always AltDigital's own site. Remove it, or use 'client-jira'.")
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
    check_timezones,
    check_maintenance_windows,
    check_ticketing,
    check_dedicated_infrastructure,
    # Halting checks last, so every form error is collected before a Halt ends
    # the run. main() prints both; the operator fixes the form once, not twice.
    check_residency,
    check_cardholder_scope,
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
    # Named, and nothing applied for it. `other` and `hitrust` today. Said per
    # framework because "hipaa, hitrust" derives the HIPAA pack, and a warning
    # that fired only when NO pack was derived would stay silent about HITRUST.
    d["frameworks_without_pack"] = [f for f in fw if not rules["conformance_packs"].get(f)]

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

    # 4.5 / form 5.6-5.7 — the window each environment uses, computed from the
    # 5.7 choice where one was given, and whether it needs waking (B-141).
    d["maintenance_windows"] = derive_maintenance_windows(q, rules, envs)

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
    # No PCI warning here: any PCI answer halts in check_cardholder_scope (D-025).
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
            "    This is the client's Truveon evidence retention floor, and cannot be\n"
            "    shortened for evidence already kept. The AWS log archive is COMPLIANCE-locked\n"
            "    for 2190 days for every tenant regardless (D-024), so this answer does not\n"
            "    change what is kept in AWS."
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
    # A warning, not an error, until B-141. If the window sits outside the HOOP
    # the environment is switched off when patching runs. Nothing errors, and
    # patch compliance keeps reporting green against a powered-off instance.
    # Nothing patches yet, so there is no job to fail silently, and refusing to
    # vest would block a tenant on infrastructure it may never need. Whoever
    # builds patching must build the wake with it, or turn this into an error
    # in the same change.
    for env, mw in d["maintenance_windows"].items():
        if not mw["wake_required"]:
            continue
        chosen = mw["source"] == "derived: outside-developer-hours"
        w.append(
            f"{env} maintenance window {mw['start']}-{mw['end']} {mw['timezone']} falls outside its\n"
            f"    HOOP {describe_gaps(mw['gaps'])}"
            f"{' — as chosen, outside developer hours' if chosen else ''}.\n"
            "    The environment must be woken for it: turn up, patch, turn down. That wake is\n"
            "    not built (B-141). Harmless today because nothing patches; not harmless the\n"
            "    day patching ships without it."
        )
    ticketing = q["change"]["ticketing"]
    if ticketing["system"] == "none":
        w.append(
            "change.ticketing.system is 'none'. Change reconciliation will report every\n"
            "    APPLICATION deployment as UNAVAILABLE -- neither approved nor unapproved,\n"
            "    simply not checked. Infrastructure deployments are unaffected. For an\n"
            "    audited tenant this needs a declared deviation and an owner.")
    elif ticketing.get("product") == "jira-software":
        w.append(
            "change.ticketing.product is 'jira-software'. Jira Software has no approval\n"
            "    records, so reconciliation can only infer approval from the status name and\n"
            "    cannot check WHO approved. Jira Service Management is needed for that.")
    if d["frameworks_without_pack"] and d["conformance_packs"]:
        w.append(
            f"No conformance pack is applied for: {', '.join(d['frameworks_without_pack'])}.\n"
            "    The framework is recorded and passed to Truveon, but no AWS Config rules\n"
            "    specific to it are deployed. Confirm the client's assessor accepts that,\n"
            "    and confirm Truveon recognises the framework name."
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
        if problems:
            print("The questionnaire also has these errors:\n", file=sys.stderr)
            for p in problems:
                print(f"  - {p}\n", file=sys.stderr)
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
    print()
    print("  Maintenance windows:")
    for e, mw in d["maintenance_windows"].items():
        days = ",".join(mw["days"]) if len(mw["days"]) < 7 else "daily"
        wake = "  WAKE REQUIRED (B-141)" if mw["wake_required"] else ""
        print(f"    {e:5s}  {days:20s} {mw['start']}-{mw['end']}  {mw['timezone']:20s} {mw['source']}{wake}")

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
