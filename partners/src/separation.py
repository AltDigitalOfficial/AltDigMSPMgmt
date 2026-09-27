"""Inter-entity segregation of duties. Prompt 14.6, design 15, open item B12.

What this adds that the quorum verifier does not
--------------------------------------------------
design 03's quorum already enforces that no approver is the requester, and
`quorum/src/verify.py` condition 2 does exactly that. That control is
INTRA-AltDigital: Jamie, Wayne and Art all act for AltDigital, and two of them
approving is two-party control within one entity.

design 15 names a different problem:

    Where a control requires one entity to request and another to approve, the
    same individual acting for both entities is not two-party control, and an
    auditor will say so.

The gap was never the quorum path. It is the actions OUTSIDE it, where one name
is recorded and the second party is assumed.

Entity-blind, by decision
---------------------------
B12 offered two shapes: require the two individuals to differ regardless of
which entity they act for, or track a person->entity mapping per action and
require the entities to differ.

Jamie chose entity-blind (2026-09-22), which is design 15's own recommended
minimum: "the requesting and approving individuals must be different people
regardless of which entity they are acting for. This is simple to enforce and
removes the question entirely."

The reason it is enforceable rather than aspirational is specific to how
AltDigital is staffed. Jamie's note when deciding: he will never own the dev
teams -- that is Wayne, Tina, an outsourced developer or the client -- so
Jamie acting as the MSP-side approver leaves a genuinely different person on
the other side every time. A rule that is correct and unsatisfiable teaches
people to route around it, and this one is not.

So it BLOCKS rather than warns. B-079 recorded the opposite case -- an approver
group of one person, which satisfied Jira and could not satisfy design 03 --
and the difference was that this rule could be met at the time. B-079 is
resolved as of 2026-09-26.

An unrecorded second party is a violation, not a pass
-------------------------------------------------------
The failure this is most likely to meet is not two names that match. It is ONE
name, with the second party implied by context.

One name cannot demonstrate two-party control. An auditor asking "who were the
two people" gets one answer and a story, which is the same answer as no
control. So an absent counterparty is a violation with its own reason, rather
than being skipped for lack of data.
"""
import json

CLEAR = "clear"
VIOLATION = "violation"


# The action list, which IS the decision. Written out rather than inferred from
# call sites so that "which actions claim inter-entity separation" is a
# question with a written answer -- design 15 asks for exactly that list and
# open item B12 is where it was owed.
#
# Chosen by Jamie, 2026-09-22: all four.
ACTIONS = {
    "notification-chain": {
        "sides": ("altdigital_to_partner", "partner_to_client"),
        "why": "design 15 makes AltDigital a subcontractor business associate "
               "whose obligation runs to the partner, and the partner's runs "
               "to their client. One person recording both legs makes the "
               "chain two notifications that were a single act.",
        "blocks": True,
    },
    "change-approval": {
        "sides": ("requester", "approver"),
        "why": "design 08 routes an unapproved deployment to the tenant's "
               "change approver, who may be a partner person. Already enforced "
               "for anything on the quorum path (verify.py condition 2); this "
               "covers approvals recorded outside it.",
        "blocks": True,
    },
    "exit-authorisation": {
        "sides": ("requested_by", "authorised_by"),
        "why": "an exit removes a partner's client. design 10 makes it a "
               "contractual commitment, and one person both asking for it and "
               "authorising it is not a commitment either party verified.",
        "blocks": True,
    },
    "go-live": {
        "sides": ("requested_by", "approved_by"),
        "why": "go-live starts SLA measurement, billing and the on-call "
               "rotation. Jamie owns the approval (design 13); the request "
               "comes from whoever owns the dev team, which is deliberately "
               "never Jamie.",
        "blocks": True,
    },
}


class SeparationViolation(Exception):
    """Two roles that must be held by different people are held by one."""


def check(action, side_a, side_b, names=None):
    """Are these two sides held by different, named people?

    `names` optionally overrides the side labels for the message, so a caller
    can say "leg" or "approver" in the words its own users recognise.
    """
    spec = ACTIONS.get(action)
    if spec is None:
        # Not in the list is not "fine" -- it is a question nobody answered.
        # A caller naming an action that was never decided on should find that
        # out rather than silently pass.
        return {"action": action, "verdict": VIOLATION,
                "why": f"'{action}' is not in the segregation action list, so "
                       "whether it claims inter-entity separation has never "
                       "been decided. Add it to ACTIONS with a reason, or use "
                       "an action that is there."}

    label_a, label_b = names or spec["sides"]

    missing = [l for l, v in ((label_a, side_a), (label_b, side_b))
               if not v or str(v).strip() in ("", "unrecorded", "unknown")]
    if missing:
        return {"action": action, "verdict": VIOLATION,
                "missing": missing,
                "why": f"{' and '.join(missing)} is not recorded. One name "
                       "cannot demonstrate two-party control -- an auditor "
                       "asking who the two people were gets one answer and a "
                       "story, which is the same answer as no control. "
                       + spec["why"]}

    if str(side_a).strip().lower() == str(side_b).strip().lower():
        return {"action": action, "verdict": VIOLATION,
                "person": side_a,
                "why": f"{side_a} holds both {label_a} and {label_b}. design "
                       "15: the same individual acting for both entities is "
                       "not two-party control, and an auditor will say so. "
                       + spec["why"]}

    return {"action": action, "verdict": CLEAR,
            label_a: side_a, label_b: side_b,
            "why": f"{side_a} and {side_b} are different people."}


def assert_separated(action, side_a, side_b, names=None):
    """check(), but raising. For callers that must not proceed."""
    result = check(action, side_a, side_b, names)
    if result["verdict"] == VIOLATION:
        raise SeparationViolation(result["why"])
    return result


def report(action, side_a, side_b, names=None):
    result = check(action, side_a, side_b, names)
    print(json.dumps({"record_type": "separation-check", **result},
                     default=str))
    return result
