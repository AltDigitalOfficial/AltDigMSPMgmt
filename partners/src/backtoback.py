"""Back-to-back verification. Prompt 14.2, design 15.

What "back-to-back" means and why it is checkable
---------------------------------------------------
AltDigital sits in the middle of a chain it does not control both ends of:

    AltDigital  ->  Partner  ->  Client  ->  individuals / HHS

Two commitments run along it in opposite directions, and both have an ordering
that must hold or the chain cannot be satisfied:

    availability    AltDigital's target must be AT LEAST AS STRONG as what the
                    partner promised their client. A partner owing 99.9% who is
                    sold 99.5% has committed to something AltDigital has not.
    notification    AltDigital's window must be TIGHTER than the partner's. The
                    partner cannot notify their client until AltDigital has
                    notified them, so the partner's time to act is exactly the
                    difference. design 15: "If the partner owes their client 72
                    hours, AltDigital owing the partner 72 hours leaves the
                    partner zero time."

Both are arithmetic over numbers captured at onboarding. Neither needs anything
to be running, which is why prompt 14.2 asks for the check at onboarding rather
than at incident time -- the moment a clock is running is the worst moment to
discover it cannot be met.

It flags; it does not refuse
------------------------------
prompt 14.2: "flag it rather than accepting it silently."

Deliberately weaker than the refusals elsewhere in this codebase, and the
difference is who the finding is about. A saga step that blocks is protecting
the platform from a half-built tenant. This is a COMMERCIAL finding about a
contract somebody has probably already signed, and the resolution is a
conversation with the partner, not a code change.

Refusing to onboard would put the platform in the position of vetoing a
commercial agreement on arithmetic, which is not its call. Recording the
mismatch loudly, in the registry and in the onboarding output, is.

The exception is the notification chain, where an inverted ordering is not a
commercial preference but an obligation that cannot be discharged. That one
refuses, because there is no arrangement of the numbers under which it works.

"Materially tighter" needs a number, and design 15 does not give one
--------------------------------------------------------------------
design 15 says AltDigital's window "must be materially tighter" and does not
define material. Strictly tighter is the floor -- an equal window leaves the
partner zero time, which is the case the design names. But one hour of headroom
on a 72-hour window is technically tighter and practically useless.

`MATERIAL_FRACTION` below is an ASSUMPTION: AltDigital's window should be no
more than half the partner's, leaving the partner at least as long as
AltDigital had. It is flagged as a warning rather than enforced, and it is
recorded here rather than in a comment somewhere downstream so that whoever
sets the real number knows what they are replacing.
"""
import json
import os

CLEAR = "clear"
FLAG = "flag"
REFUSE = "refuse"

# See the module docstring. Not from design 15; replace deliberately.
MATERIAL_FRACTION = float(os.environ.get("BACKTOBACK_MATERIAL_FRACTION", "0.5"))


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_notification(altdig_hours, partner_hours):
    """AltDigital's window against the partner's downstream window.

    Three outcomes. The refusal is the inverted or equal case, because no
    arrangement of those numbers lets the partner notify in time.
    """
    altdig = _number(altdig_hours)
    partner = _number(partner_hours)

    if altdig is None or partner is None:
        missing = [n for n, v in (("AltDigital->partner", altdig),
                                  ("partner->client", partner)) if v is None]
        return {"check": "notification-window", "verdict": REFUSE,
                "why": f"window(s) not set: {', '.join(missing)}. The incident "
                       "clock cannot run without both, and an unset window is "
                       "discovered during an incident."}

    if altdig >= partner:
        return {"check": "notification-window", "verdict": REFUSE,
                "altdig_hours": altdig, "partner_hours": partner,
                "why": f"AltDigital owes the partner {altdig:g}h and the "
                       f"partner owes their client {partner:g}h. The partner "
                       "cannot notify before being notified, so this leaves "
                       f"{partner - altdig:g}h to act. There is no "
                       "arrangement of these numbers that satisfies the "
                       "chain -- this is not a commercial preference."}

    headroom = partner - altdig
    if altdig > partner * MATERIAL_FRACTION:
        return {"check": "notification-window", "verdict": FLAG,
                "altdig_hours": altdig, "partner_hours": partner,
                "headroom_hours": headroom,
                "why": f"AltDigital's {altdig:g}h window is tighter than the "
                       f"partner's {partner:g}h but leaves only {headroom:g}h "
                       "to act. design 15 asks for materially tighter; the "
                       f"{MATERIAL_FRACTION:.0%} guide is an assumption of "
                       "this platform rather than a figure from the design, "
                       "so this is a conversation and not a rule."}

    return {"check": "notification-window", "verdict": CLEAR,
            "altdig_hours": altdig, "partner_hours": partner,
            "headroom_hours": headroom,
            "why": f"AltDigital's {altdig:g}h leaves the partner {headroom:g}h "
                   "to notify their client."}


def check_availability(altdig_pct, downstream_pct):
    """AltDigital's availability target against the partner's downstream one.

    Flags rather than refusing: the partner may have sold something AltDigital
    did not agree to, and that is a contract conversation. The platform's job
    is to make sure nobody discovers it during the first outage.
    """
    altdig = _number(altdig_pct)
    downstream = _number(downstream_pct)

    if downstream is None:
        return {"check": "availability", "verdict": FLAG,
                "why": "the partner's downstream SLA commitment is not "
                       "recorded. design 15 captures it at partner onboarding "
                       "precisely so that AltDigital's number can be compared "
                       "with it BEFORE signing; without it the comparison "
                       "cannot be made at all."}
    if altdig is None:
        return {"check": "availability", "verdict": FLAG,
                "why": "AltDigital's SLA tier for this partner is not "
                       "recorded, so there is nothing to compare."}

    if altdig < downstream:
        return {"check": "availability", "verdict": FLAG,
                "altdig_pct": altdig, "downstream_pct": downstream,
                "why": f"the partner has committed {downstream:g}% to their "
                       f"clients and AltDigital has committed {altdig:g}% to "
                       "the partner. The partner is carrying the difference "
                       "and will owe credits on outages AltDigital does not. "
                       "design 15: AltDigital's number must be at least as "
                       "strong."}

    return {"check": "availability", "verdict": CLEAR,
            "altdig_pct": altdig, "downstream_pct": downstream,
            "why": f"AltDigital's {altdig:g}% is at least the partner's "
                   f"{downstream:g}% downstream commitment."}


def verify(partner_record):
    """Both checks over one partner record.

    Returns the findings and an overall verdict. `refuse` wins over `flag`
    wins over `clear` -- an aggregate that reported the best of its parts
    would hide the one that matters.
    """
    record = partner_record or {}
    results = [
        check_notification(record.get("altdig_hours"),
                           record.get("notify_hours")),
        check_availability(record.get("altdig_sla_pct"),
                           record.get("downstream_sla_pct")),
    ]

    verdicts = [r["verdict"] for r in results]
    overall = (REFUSE if REFUSE in verdicts
               else FLAG if FLAG in verdicts
               else CLEAR)

    out = {
        "record_type": "partner-back-to-back",
        "partner": record.get("slug"),
        "verdict": overall,
        # Always reported, so "clear" is distinguishable from "checked
        # nothing". A partner record missing both numbers produces two
        # findings, not silence.
        "checks_run": len(results),
        "results": results,
    }
    print(json.dumps(out, default=str))
    return out


def handler(event, context=None):
    return verify((event or {}).get("partner"))
