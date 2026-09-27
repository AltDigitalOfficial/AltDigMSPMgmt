"""Quorum verification. Prompt 8.3, design 08 "Quorum verification".

design/03-identity-and-access.md names the actions:

    The following require two approvals from {Jamie, Wayne, Art}, with
    requester and approvers being distinct people:
      - kms:ScheduleKeyDeletion
      - Backup vault deletion or vault lock modification
      - Removal of a member account from the Organization
      - Disabling CloudTrail or the Config recorder

    These are denied outright in the relevant resource policies and SCPs. The
    only execution path is a platform pipeline that verifies two recorded
    approvals before assuming a permitted role.

design/08-change-and-release.md names the five conditions, and one instruction
shapes this whole module:

    The pipeline validates rather than trusting the ticket's status field.
    Jira permissions are easy to get subtly wrong.

That is not abstract caution here. The Authorize stage in AMI is satisfied by
approvals from the Approver groups -- two since 2026-09-26. But Jira's two are
any two group members, group membership is managed in Jira rather than against
the quorum list, and nothing in the workflow as configured is known to exclude
the requester. So Jira could call an approval set sufficient that design 03
would not.

The quorum set itself is wider than the quotation above: all four partners,
Tina included, per D-022 in docs/deviations.md. The list lives in SSM, so this
module does not change with it. Where
the two disagree, this module implements design 03 and ignores the workflow's
opinion -- which is exactly what "validates rather than trusts" means.

Three verdicts, not two
-----------------------
    approved         all five conditions hold. Execution may proceed.
    refused          a condition demonstrably fails. Execution must not.
    undeterminable   the question could not be answered.

The third is the one that keeps getting rebuilt in this codebase, because
collapsing it into "refused" looks harmless and is not. B-042, B-054 and the
`change_source_unavailable` outcome in reconcile.py are the same lesson: an
unreadable source is not a negative result. Here it inverts -- the safe
DIRECTION is still to refuse execution, so nothing dangerous happens either
way -- but the REPORT differs, and that is what matters. "Two approvals are
missing" sends somebody to chase approvers. "The approver list could not be
read from SSM" sends somebody to fix the platform. A pipeline that says the
first when it means the second wastes the outage.

So: undeterminable never executes, and never claims the request was refused.
It raises an exception record against the platform itself.
"""
import datetime
import json
import os
import re

import boto3

import jira

ssm = boto3.client("ssm")

APPROVERS_PARAM = os.environ.get("QUORUM_APPROVERS_PARAM",
                                 "/platform/quorum/approvers")

# The actions design 03 puts behind two-party control, mapped to the thing the
# pipeline is allowed to do about them. Data rather than a dispatch chain, so
# adding one is an edit to this table and a matching branch in execute.py.
QUORUM_ACTIONS = {
    "kms:ScheduleKeyDeletion": "schedule-key-deletion",
    "backup:DeleteBackupVault": "delete-backup-vault",
    "backup:PutBackupVaultLockConfiguration": "modify-vault-lock",
    "backup:DeleteBackupVaultLockConfiguration": "modify-vault-lock",
    "organizations:RemoveAccountFromOrganization": "remove-account",
    "cloudtrail:StopLogging": "disable-cloudtrail",
    "cloudtrail:DeleteTrail": "disable-cloudtrail",
    "config:StopConfigurationRecorder": "disable-config-recorder",
    "config:DeleteConfigurationRecorder": "disable-config-recorder",
}

VERDICT_APPROVED = "approved"
VERDICT_REFUSED = "refused"
VERDICT_UNDETERMINABLE = "undeterminable"

# How the change record declares what it authorises.
#
# Condition 5 -- "the approval is for this specific resource and action" -- is
# unanswerable unless the record says which resource and which action. A
# summary that reads "delete the old KMS key" does not, and approving it
# authorises nothing checkable.
#
# A marker block in the description rather than two more custom fields,
# because every field added to this project has needed separate configuration
# on the issue screen AND the request type form (B-076, B-078), and a change
# record that cannot be raised is worse than one that is slightly ugly.
#
# Strict on purpose: no fuzzy matching, no "did you mean". An ambiguous target
# is undeterminable, not approved.
TARGET_ACTION_RE = re.compile(r"^\s*quorum-action:\s*(\S+)\s*$",
                              re.MULTILINE | re.IGNORECASE)
TARGET_RESOURCE_RE = re.compile(r"^\s*quorum-resource:\s*(\S+)\s*$",
                                re.MULTILINE | re.IGNORECASE)

# Changelog fields whose movement after approval does not change what was
# approved. Everything else is material.
#
# Deliberately short. The default is that an edit invalidates, because design
# 08's condition 4 exists to catch a record being altered after somebody signed
# it, and a generous ignore list is how that check quietly stops working.
IGNORED_CHANGELOG_FIELDS = {
    "rank",            # board ordering
    "worklogid", "timespent", "timeestimate", "timeoriginalestimate",
    "comment",         # comments are additive and do not rewrite the record
    "attachment",      # so is evidence
    "remoteissuelink",
    "actual start", "actual end",   # written by the pipeline AFTER execution
}


class Undeterminable(Exception):
    """The question could not be answered. Not the same as a refusal."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(value):
    """Jira timestamps, including the offset form without a colon.

    Jira returns 2026-09-19T17:20:41.056-0600. `fromisoformat` rejects that on
    Python < 3.11 and accepts it on 3.11+, so normalising here keeps this
    independent of which runtime the Lambda lands on. reconcile.py carries the
    same helper for the same reason.
    """
    if not value:
        return None
    if isinstance(value, dict):
        # An approval date arrives as {"iso8601": ..., "epochMillis": ...}.
        # epochMillis is unambiguous, so prefer it over parsing anything.
        millis = value.get("epochMillis")
        if millis is not None:
            return datetime.datetime.fromtimestamp(
                millis / 1000.0, datetime.timezone.utc)
        value = value.get("iso8601") or value.get("jira")
    try:
        text = str(value).replace("Z", "+00:00")
        if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
            text = text[:-2] + ":" + text[-2:]
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _adf_text(node):
    """Flatten ADF to plain text so the target markers can be read.

    The description comes back as a document, not a string, and the markers
    live in it. Recursive because a paragraph can nest.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    inner = _adf_text(node.get("content"))
    # Paragraphs and list items are separate lines; the markers are matched
    # line-anchored, so losing the break would lose the marker.
    if node.get("type") in ("paragraph", "listItem", "heading",
                            "codeBlock", "blockquote"):
        return inner + "\n"
    return inner


def authorised_approvers():
    """Who may authorise, and how many are needed. From SSM, never from code.

    Returns (accountIds, minimum). Raises Undeterminable if the list cannot be
    read -- which must not be reported as "nobody is authorised", because that
    is indistinguishable from a correctly refused request and sends whoever is
    on call to argue with approvers instead of fixing a parameter. B-054, and
    B-042 before it.
    """
    try:
        raw = ssm.get_parameter(Name=APPROVERS_PARAM)["Parameter"]["Value"]
    except Exception as exc:                       # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        raise Undeterminable(
            f"could not read {APPROVERS_PARAM}: {code or type(exc).__name__}. "
            "The authorised approver list is unavailable, so no approval can "
            "be checked against it. Run scripts/sync-quorum-approvers.sh."
        ) from exc
    try:
        data = json.loads(raw)
        approvers = {str(a) for a in data["approvers"] if str(a).strip()}
        minimum = int(data.get("minimum", 2))
    except (ValueError, KeyError, TypeError) as exc:
        raise Undeterminable(
            f"{APPROVERS_PARAM} is not the expected JSON "
            '{"approvers": [...], "minimum": n}') from exc
    if not approvers:
        raise Undeterminable(
            f"{APPROVERS_PARAM} lists no approvers. This is a platform "
            "configuration fault, not a refused request.")
    if minimum < 2:
        # design 03 says two. A parameter that says otherwise is more likely to
        # be a mistake or a workaround than a decision, and the whole value of
        # the control is that one person cannot satisfy it.
        raise Undeterminable(
            f"{APPROVERS_PARAM} sets minimum={minimum}. design 03 requires two "
            "approvals from distinct people; a quorum of one is not a quorum.")
    return approvers, minimum


def declared_target(fields):
    """What the record says it authorises. Condition 5's raw material.

    Returns (action, resource). Raises Undeterminable when the record does not
    say, because an approval of an unstated target authorises nothing
    specific -- and treating that as a refusal would blame the approvers for
    what is actually a malformed request.
    """
    text = _adf_text(fields.get("description"))
    actions = TARGET_ACTION_RE.findall(text)
    resources = TARGET_RESOURCE_RE.findall(text)
    if not actions or not resources:
        raise Undeterminable(
            "the change record does not declare what it authorises. It needs "
            "two lines in the description:\n"
            "        quorum-action: kms:ScheduleKeyDeletion\n"
            "        quorum-resource: arn:aws:kms:us-east-2:...:key/...\n"
            "      Without them, condition 5 -- the approval is for this "
            "specific resource and action -- cannot be checked, and an "
            "approval of an unstated target authorises nothing.")
    if len(set(actions)) > 1 or len(set(resources)) > 1:
        raise Undeterminable(
            f"the change record declares more than one target: "
            f"actions={sorted(set(actions))} resources={sorted(set(resources))}. "
            "One record authorises one action on one resource. Split it.")
    action, resource = actions[0], resources[0]
    if action not in QUORUM_ACTIONS:
        raise Undeterminable(
            f"'{action}' is not a quorum-controlled action. Known: "
            f"{', '.join(sorted(QUORUM_ACTIONS))}. Either the record has a "
            "typo, or this action does not belong in this pipeline.")
    return action, resource


def decided_approvals(approvals):
    """Distinct people who APPROVED, with when. Ignores everything else.

    The live shape, confirmed against AMI-4 rather than taken from the docs:

        [{"id": "2", "name": "Authorize", "finalDecision": "approved",
          "approvers": [{"approver": {"accountId": ...},
                         "approverDecision": "approved"}],
          "completedDate": {"epochMillis": ...}}]

    One approval OBJECT per workflow step, containing a list of approvers. So
    design 03's "two approvals" means two distinct approvers, not two steps --
    a pipeline that counted objects would find one and refuse forever.

    accountId only. The response also carries displayName and emailAddress;
    neither is recorded, because the evidence snapshot is retained long-term
    and an accountId is both stable and not personal data in the way a name
    and a direct address are.
    """
    seen = {}
    for approval in approvals or []:
        when = _parse(approval.get("completedDate")) \
            or _parse(approval.get("createdDate"))
        for entry in (approval.get("approvers") or []):
            if (entry.get("approverDecision") or "").lower() != "approved":
                continue
            account = ((entry.get("approver") or {}).get("accountId"))
            if not account:
                continue
            # Earliest decision wins if somebody appears twice across steps.
            if account not in seen or (when and seen[account] and
                                       when < seen[account]):
                seen[account] = when
    return seen


def material_changes_since(changelog, since):
    """Edits after `since` that change what was approved. Condition 4.

    Everything is material unless it is in the ignore list, and that default is
    the point: condition 4 exists to catch a record being rewritten after
    somebody signed it, and a generous ignore list is how such a check quietly
    stops working.

    Status is included. design 08 says "reopened or edited", and a reopen is a
    status movement -- but so is the transition INTO the post-approval status,
    which the approval itself causes. Both are reported; the caller decides,
    because a transition to "Awaiting implementation" is the approval working
    and a transition back to "Review" is the record being reopened.
    """
    out = []
    if not since:
        return out
    for entry in (changelog or {}).get("histories") or []:
        at = _parse(entry.get("created"))
        if not at or at <= since:
            continue
        for item in entry.get("items") or []:
            field = (item.get("field") or "").strip().lower()
            if field in IGNORED_CHANGELOG_FIELDS:
                continue
            out.append({
                "field": item.get("field"),
                "from": item.get("fromString"),
                "to": item.get("toString"),
                "at": at.isoformat(),
                # accountId, not a name. Same reasoning as the approvals.
                "by": ((entry.get("author") or {}).get("accountId")),
            })
    return out


def verify(event, context=None):
    """Answer whether this action is authorised, and say why either way.

    event: {"issue": "AMI-9",
            "action": "kms:ScheduleKeyDeletion",     # what we intend to do
            "resource": "arn:aws:kms:..."}           # to what

    The action and resource come from the CALLER, not from the ticket, and are
    then checked against what the ticket declares. That direction matters: if
    the pipeline read the target out of the ticket and then executed it, a
    ticket edited between approval and execution would redirect the action, and
    condition 4 would be the only thing standing in the way. Asking the caller
    to state its intent and refusing on mismatch means the two have to agree.
    """
    key = event.get("issue")
    intended_action = event.get("action")
    intended_resource = event.get("resource")
    checked_at = _now()

    base = {"record_type": "quorum-verification", "issue": key,
            "intended_action": intended_action,
            "intended_resource": intended_resource,
            "checked_at": checked_at.isoformat()}

    if not key or not intended_action or not intended_resource:
        return _undeterminable(base, "verify needs issue, action and resource")

    try:
        approvers, minimum = authorised_approvers()
        issue = jira.issue_with_changelog(key)
        approvals = jira.approvals(key)
    except Undeterminable as exc:
        return _undeterminable(base, str(exc))
    except jira.JiraUnavailable as exc:
        # Jira being unreachable is emphatically not "this was not approved".
        return _undeterminable(base, f"Jira could not be read: {exc}")

    fields = issue.get("fields") or {}
    status = ((fields.get("status") or {}).get("name"))
    requester = ((fields.get("reporter") or {}).get("accountId"))
    requested_at = _parse(fields.get("created"))
    base.update({"status": status, "requester_account_id": requester,
                 "requested_at": requested_at.isoformat()
                 if requested_at else None})

    try:
        declared_action, declared_resource = declared_target(fields)
    except Undeterminable as exc:
        return _undeterminable(base, str(exc))
    base.update({"declared_action": declared_action,
                 "declared_resource": declared_resource})

    decided = decided_approvals(approvals)
    base["approver_account_ids"] = sorted(decided)
    base["approval_count"] = len(decided)

    # No approvals at all has three causes and only one is a refusal. Told
    # apart by the status, because the API will not volunteer which it is:
    # an approval record does not exist until the workflow ENTERS the approval
    # step, so a record awaiting submission looks exactly like one everybody
    # declined. B-078.
    if not approvals:
        return _undeterminable(
            base,
            f"{key} has no approval record at all, and it is in status "
            f"'{status}'. Either it has not yet reached the approval step, or "
            "the workflow has no approval step on this request type. Neither "
            "is a refusal by an approver, and neither is fixed by asking one.")

    failures = []

    # ---- Condition 1: both approvers are on the authorised list
    unauthorised = sorted(a for a in decided if a not in approvers)
    if unauthorised:
        failures.append({
            "condition": 1,
            "rule": "every approver is on the authorised list",
            "detail": f"{len(unauthorised)} approver(s) not in "
                      f"{APPROVERS_PARAM}: {unauthorised}"})

    # ---- Condition 2: neither approver is the requester
    #
    # Currently fails for every record the platform files, and that is a real
    # finding rather than a quirk: the Jira credential is a person's own
    # account (B-070), so the platform files as that person, and their approval
    # is a self-approval. A service account fixes it. Nothing here papers over
    # it, because a two-party control that accepts one party is not one.
    if requester and requester in decided:
        failures.append({
            "condition": 2,
            "rule": "no approver is the requester",
            "detail": f"the requester ({requester}) is among the approvers. "
                      "Two-party control is not satisfied by one party, "
                      "whichever hats they are wearing."})

    # ---- Condition 3: approvals postdate the request
    if requested_at:
        early = sorted(a for a, when in decided.items()
                       if when and when < requested_at)
        if early:
            failures.append({
                "condition": 3,
                "rule": "approvals postdate the request",
                "detail": f"approval(s) recorded before the request was "
                          f"created: {early}"})

    # ---- Condition 4: not reopened or edited since approval
    latest = max((w for w in decided.values() if w), default=None)
    changes = material_changes_since(issue.get("changelog"), latest)
    reopened = [c for c in changes
                if (c["field"] or "").lower() == "status"]
    edited = [c for c in changes if c not in reopened]
    base["changes_since_approval"] = changes
    if edited:
        failures.append({
            "condition": 4,
            "rule": "the record has not been edited since approval",
            "detail": f"{len(edited)} material edit(s) after the last "
                      f"approval: "
                      f"{sorted({c['field'] for c in edited})}"})
    # A status move after approval is expected -- the approval causes it. Only
    # a move BACKWARDS is a reopen, and the pipeline cannot tell which from the
    # name alone, so it records them and refuses only if the record left an
    # approved state entirely.
    if reopened and not approvals_still_final(approvals):
        failures.append({
            "condition": 4,
            "rule": "the record has not been reopened since approval",
            "detail": f"status moved after approval and the approval is no "
                      f"longer final: {[c['to'] for c in reopened]}"})

    # ---- Condition 5: the approval is for THIS resource and action
    if declared_action != intended_action:
        failures.append({
            "condition": 5,
            "rule": "the approval is for this action",
            "detail": f"record authorises '{declared_action}', pipeline "
                      f"intends '{intended_action}'"})
    if declared_resource != intended_resource:
        failures.append({
            "condition": 5,
            "rule": "the approval is for this resource",
            "detail": f"record authorises '{declared_resource}', pipeline "
                      f"intends '{intended_resource}'"})

    # ---- The count, which is separate from the conditions
    #
    # Checked last so the report names every problem at once. A pipeline that
    # stopped at the first failure would have somebody fix the approver count,
    # re-run, and only then discover the resource was wrong too.
    authorised_count = len([a for a in decided if a in approvers])
    if authorised_count < minimum:
        failures.append({
            "condition": 0,
            "rule": f"at least {minimum} authorised approvals",
            "detail": f"{authorised_count} authorised approval(s), "
                      f"{minimum} required"})

    verdict = VERDICT_REFUSED if failures else VERDICT_APPROVED
    out = {**base, "verdict": verdict, "failures": failures,
           "authorised_approval_count": authorised_count,
           "minimum_required": minimum}
    print(json.dumps(out, default=str))
    return out


def approvals_still_final(approvals):
    """Is every approval step still showing a final decision?

    A reopened issue re-enters the approval step, and JSM resets that step's
    finalDecision. So "the approval is no longer final" is the durable signal
    for a reopen, rather than trying to rank status names the platform does not
    define.
    """
    for approval in approvals or []:
        if (approval.get("finalDecision") or "").lower() != "approved":
            return False
    return True


def _undeterminable(base, reason):
    out = {**base, "verdict": VERDICT_UNDETERMINABLE, "failures": [],
           "reason": reason}
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"verify": verify}


def handler(event, context):
    action = event.get("action_type") or "verify"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(sorted(HANDLERS))}")
    return fn(event, context)
