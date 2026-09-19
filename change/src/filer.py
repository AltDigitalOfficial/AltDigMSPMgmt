"""Files change records. Prompt 8.2's write half, kept deliberately apart.

Why this is a separate module from jira.py
------------------------------------------
`jira.py` is read-only by construction and `scripts/publish-change.sh` refuses
to package it if a write-shaped function appears. That guard exists because a
credential able to edit issues could manufacture the approval it then verifies,
which would make the quorum control (8.3) circular.

None of that changes because the platform now files records. What changes is
that there are two jobs, and they get two modules, two Lambda functions and two
IAM roles:

    jira.py      reads.  Used by reconciliation and by quorum verification.
    filer.py     writes. Used by nothing that verifies anything.

WHAT THIS DOES NOT BUY, stated plainly: real separation needs two Atlassian
accounts, and there is one. The account behind the shared credential can write
regardless of which module reads the secret, and it is currently a human who is
also an approver. So this is code-level separation over a credential that does
not yet have it. B-070 carries the fix — a genuine service account, which will
not be an approver — and that is the thing that makes the quorum control sound
rather than merely tidy.

One hard line, enforced in the publish guard: this module never calls an
APPROVAL endpoint. Filing a change and approving it are different acts, and the
platform may only do the first. If it could do both, the record it files would
prove nothing.

Field ids are resolved by NAME, never hardcoded
-----------------------------------------------
`customfield_10059` is "Implementation plan" on this site and something else
entirely on another. The ids are discovered per run from the same read-only
metadata call that answered "what does a change record need", so this survives
a different Jira, a reconfigured project, and the tenant projects that do not
exist yet.
"""
import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

import jira

secrets = boto3.client("secretsmanager")

JIRA_SECRET_NAME = os.environ.get("JIRA_SECRET_NAME",
                                  "platform/jira/service-account")
OPS_PROJECT = os.environ.get("JIRA_OPS_PROJECT", "")
CHANGE_ISSUETYPE = os.environ.get("JIRA_CHANGE_ISSUETYPE", "[System] Change")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "30"))

# Defaults for build-phase records, decided with Jamie 2026-09-19.
#
# Standard, and the label is not decoration. "Standard" in ITIL means
# pre-approved, low-risk and repeatable, and novel platform build work is none
# of those — it is being called Standard so that records count as approved
# while the platform is being built, and `build-phase` is what stops that
# category being silently overclaimed later. Switch to Normal once the platform
# is in service.
DEFAULT_CHANGE_TYPE = os.environ.get("CHANGE_TYPE_DEFAULT", "Standard")
DEFAULT_REASON = os.environ.get("CHANGE_REASON_DEFAULT", "New functionality")
BUILD_LABEL = "build-phase"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _jira_dt(dt):
    """Jira wants an offset without a colon: 2026-09-19T12:00:00.000+0000."""
    return dt.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000+0000")


def _adf(text):
    """Plain text as an Atlassian Document Format paragraph.

    API v3 takes rich text as ADF, not as a string. Passing a string gets
    "Operation value must be an Atlassian Document" — an error that names a
    format most people have never heard of. Newlines become separate
    paragraphs, because a plan collapsed onto one line is not a plan anybody
    can follow at 3am.
    """
    paragraphs = [p for p in str(text or "").split("\n")]
    return {
        "type": "doc", "version": 1,
        "content": [
            {"type": "paragraph",
             "content": ([{"type": "text", "text": p}] if p.strip() else [])}
            for p in paragraphs
        ] or [{"type": "paragraph", "content": []}],
    }


# Jira's custom field types that take ADF rather than a plain string.
# Substring match: Atlassian namespaces these as
# com.atlassian.jira.plugin.system.customfieldtypes:textarea and JSM adds its
# own, so the discriminating part is the tail.
_RICH_TEXT_CUSTOM_TYPES = ("textarea", "richtext")

# System fields that are rich text and carry no `custom` type at all.
_RICH_TEXT_SYSTEM_FIELDS = ("description", "environment")


def _is_rich_text(field):
    """Does this field take ADF?

    `schema.type` says "string" for both plain and rich text, which is the
    trap that cost the first filing attempt: three fields rejected with "not
    valid Atlassian Document Format" and nothing in the metadata obviously
    saying why. The custom type is the discriminator.
    """
    custom = (field.get("custom") or "").lower()
    if any(t in custom for t in _RICH_TEXT_CUSTOM_TYPES):
        return True
    return field.get("id") in _RICH_TEXT_SYSTEM_FIELDS


def _post(path, payload, method="POST"):
    url = f"{jira._site()}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": jira._auth_header(),
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:600]
        except Exception:                          # noqa: BLE001
            pass
        # Jira reports a rejected field by its CUSTOM FIELD ID, not its label,
        # so the raw body is kept whole. "customfield_10059 is not on the
        # appropriate screen" means nothing without it.
        raise jira.JiraUnavailable(
            f"Jira rejected {method} {path} with HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise jira.JiraUnavailable(f"could not reach Jira: {exc}") from exc


def _field_ids(project, issue_type_name):
    """Map field NAME -> id for this project and issue type."""
    id_to_name = jira.project_issue_type_map(project)
    type_id = {v: k for k, v in id_to_name.items()}.get(issue_type_name)
    if not type_id:
        raise jira.JiraUnavailable(
            f"project {project} has no issue type '{issue_type_name}'. "
            f"It has: {', '.join(sorted(id_to_name.values()))}")
    by_name = {}
    for f in jira.field_requirements(project, type_id):
        if f.get("name"):
            by_name[f["name"]] = f
    return by_name


def file_change(event, context=None):
    """Open a change record BEFORE the work, per Jamie's decision.

    Returns the issue key. Everything the platform then does is expected to
    reference it, which is what lets reconciliation match a deployment to the
    record that authorised it rather than to whatever else was open at the time.
    """
    project = event.get("project") or OPS_PROJECT
    if not project:
        raise ValueError("no project: set JIRA_OPS_PROJECT or pass project")
    summary = event.get("summary")
    if not summary:
        raise ValueError("a change record without a summary is not a record")

    available = _field_ids(project, CHANGE_ISSUETYPE)
    start = _now()
    end = start + datetime.timedelta(
        hours=float(event.get("window_hours") or 4))

    fields = {
        "project": {"key": project},
        "issuetype": {"name": CHANGE_ISSUETYPE},
        "summary": summary[:250],
    }

    # Everything below is set only if the field EXISTS on this screen. A
    # project configured differently gets a record with fewer fields rather
    # than a rejection, which matters because tenant projects are not built
    # yet and will not all look like this one.
    wrapped = []

    def put(name, value):
        """Set a field if this project HAS it, in the shape that field takes.

        The shape is decided from the field's own metadata, not from a list
        here. A hand-maintained list of "these three are rich text" was wrong
        within an hour of the first record being filed -- a fourth rich text
        field was added to the screen and would have been sent as a string and
        rejected. Asking the metadata means a new field works without a code
        change, which is the point of resolving fields by name in the first
        place.
        """
        f = available.get(name)
        if f is None or value is None:
            return
        if _is_rich_text(f) and isinstance(value, str):
            value = _adf(value)
            wrapped.append(name)
        fields[f["id"]] = value

    labels = [BUILD_LABEL] + [f"account-{a}" for a in (event.get("accounts") or [])]
    put("Labels", labels)
    put("Change type", {"value": event.get("change_type") or DEFAULT_CHANGE_TYPE})
    put("Change reason", {"value": event.get("reason") or DEFAULT_REASON})
    put("Change risk", {"value": event.get("risk") or "Low"})
    put("Impact", {"value": event.get("impact") or "Minor / Localized"})
    put("Planned start", _jira_dt(start))
    put("Planned end", _jira_dt(end))
    # The narrative fields. Plain strings in, ADF out -- `put` handles the
    # conversion from each field's own metadata, so a rich text field added to
    # the screen later needs a key here and nothing else.
    #
    # `extra` is how a caller reaches a field this code has never heard of:
    # {"Business Test Plan": "..."} sets it if the project has it and is
    # ignored if it does not. Tenant projects will not all look like AMI.
    put("Implementation plan", event.get("plan"))
    put("Backout plan", event.get("backout"))
    put("Test plan", event.get("test_plan"))
    put("Business Test Plan", event.get("business_test_plan"))
    for name, value in (event.get("extra") or {}).items():
        put(name, value)

    put("Description", event.get("description") or event.get("reason_text"))

    created = _post("/rest/api/3/issue", {"fields": fields})
    key = created.get("key")
    out = {"record_type": "change-record-filed", "key": key,
           "project": project, "summary": summary,
           "planned_start": _jira_dt(start), "planned_end": _jira_dt(end),
           "labels": labels,
           # Which fields needed ADF, so a filing that silently sent a string
           # to a rich text field is visible in the log rather than only in
           # the 400 it would have caused.
           "rich_text_fields": sorted(wrapped),
           "fields_set": sorted(k for k in fields if k not in
                                ("project", "issuetype", "summary")),
           "fields_unavailable": sorted(
               n for n in ("Change type", "Change reason", "Change risk",
                           "Impact", "Planned start", "Planned end",
                           "Implementation plan", "Backout plan", "Test plan",
                           "Business Test Plan", "Description")
               if n not in available)}
    print(json.dumps(out, default=str))
    return out


def complete_change(event, context=None):
    """Record what actually happened, after the work.

    A comment rather than a field edit. The plan is what was intended and
    should stay legible as such; the outcome is a separate statement with its
    own timestamp. Overwriting the plan with the result would destroy the one
    comparison an auditor wants to make.
    """
    key = event.get("key")
    if not key:
        raise ValueError("complete_change needs the issue key")
    result = event.get("result") or "Completed."
    body = (f"*Completed* {_now().isoformat()}\n\n{result}")
    _post(f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment",
          {"body": _adf(body)})
    out = {"record_type": "change-record-completed", "key": key}
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"file": file_change, "complete": complete_change}


def handler(event, context):
    action = event.get("action") or "file"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(sorted(HANDLERS))}")
    return fn(event, context)
