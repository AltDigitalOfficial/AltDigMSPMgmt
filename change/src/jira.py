"""Minimal Jira Cloud client. Shared by reconciliation (8.2) and quorum (8.3).

design/08-change-and-release.md is explicit about what Jira is and is not:

    Truveon is the system of record for audit. Jira is the workflow layer
    feeding it, not the record.

    This distinction matters because Jira history can be altered by a project
    admin, and auditors will ask about that.

So nothing here treats a Jira response as authoritative. It is evidence of what
the workflow system SAYS, captured with a timestamp, and the quorum pipeline
snapshots it immutably at the moment of execution precisely because it can
change afterwards.

Read-only by construction
-------------------------
There is no create, update or transition method in this module, and that is a
control rather than an omission. A credential able to edit issues could be used
to manufacture the approval it then verifies, which would make the whole quorum
check circular. The service account should be scoped read-only in Jira as well;
this is the second half of that, so a mis-scoped account still cannot write
through this code path.

urllib, not the atlassian SDK
-----------------------------
Three endpoints are needed. A dependency would mean a Lambda layer or a vendored
package, both of which have to be rebuilt and re-published when the SDK moves,
for no gain over forty lines of urllib. The same reasoning alerting/src/router.py
applies to the PagerDuty Events API.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

secrets = boto3.client("secretsmanager")

JIRA_SECRET_NAME = os.environ.get("JIRA_SECRET_NAME",
                                  "platform/jira/service-account")
JIRA_SITE = os.environ.get("JIRA_SITE", "")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "20"))

_AUTH = {}


class JiraUnavailable(Exception):
    """Jira could not be reached or could not be read.

    A DISTINCT type, and the distinction is the whole point. Everything that
    consumes this module has to tell "Jira says there is no approved change"
    apart from "nobody could ask Jira", because the first is a finding about a
    deployment and the second is a finding about the platform. Collapsing them
    is B-054 -- a denied lookup reported as a negative result -- and here it
    would mean every deployment in the estate being reported as unapproved the
    moment a token expires.
    """


def _site():
    # Trailing slash tolerated. config/platform.env asks for it without one,
    # and a value pasted from a browser address bar will have it; the
    # difference is a double slash in every request path, which Atlassian
    # happens to accept and which would look like a client bug the first time
    # it did not.
    return (JIRA_SITE or "").rstrip("/")


def _auth_header():
    """Basic auth from the stored service account.

    Cached for the life of the execution environment. Rotation takes effect on
    the next cold start, which is the right trade for a credential read on
    every reconciliation run.
    """
    if "header" not in _AUTH:
        try:
            raw = secrets.get_secret_value(
                SecretId=JIRA_SECRET_NAME)["SecretString"]
        except Exception as exc:                   # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            raise JiraUnavailable(
                f"could not read {JIRA_SECRET_NAME}: {code or type(exc).__name__}. "
                "The Jira credential is missing from this account. Run "
                "scripts/set-jira-token.sh.") from exc
        try:
            creds = json.loads(raw)
            pair = f"{creds['email']}:{creds['api_token']}"
        except (ValueError, KeyError) as exc:
            raise JiraUnavailable(
                "the Jira secret is not the expected JSON "
                '{"email": ..., "api_token": ...}') from exc
        _AUTH["header"] = "Basic " + base64.b64encode(
            pair.encode()).decode()
    return _AUTH["header"]


def _get(path, params=None):
    if not _site():
        raise JiraUnavailable(
            "JIRA_SITE is unset. Nothing can be asked of Jira, so no change "
            "record can be confirmed or ruled out.")
    url = f"{_site()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": _auth_header(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")[:400]
        except Exception:                          # noqa: BLE001
            pass
        # 401 and 403 are the interesting ones and are NOT "no results".
        #
        # Jira answers a bad credential with "Client must be authenticated to
        # access this resource", which is true and useless — it does not
        # distinguish a wrong token from a wrong identity from the wrong auth
        # SCHEME. The causes are few and worth naming here, because the
        # alternative is somebody regenerating a perfectly good token.
        hint = ""
        if exc.code == 401:
            hint = (
                "\n      A 401 from Jira Cloud is almost always one of:"
                "\n      (1) the email is not the Atlassian account's PRIMARY "
                "address. A mail alias or distribution address is not an "
                "Atlassian identity — the account is known by the address it "
                "was registered with, and Basic auth must use that one."
                "\n      (2) the token is a SCOPED api token. Scoped tokens "
                "authenticate as Bearer, not Basic, and will 401 against this "
                "client. Create a classic unscoped API token instead."
                "\n      (3) the token was revoked, expired, or belongs to a "
                "different Atlassian site."
                "\n      Re-run scripts/set-jira-token.sh to correct it; the "
                "previous version is retained by Secrets Manager.")
        elif exc.code == 403:
            hint = (
                "\n      403 means the credential is VALID but the account "
                "lacks permission. Check it has product access to Jira and "
                "browse permission on the project.")
        raise JiraUnavailable(
            f"Jira returned HTTP {exc.code} for {path}: {body}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JiraUnavailable(f"could not reach Jira: {exc}") from exc


# ---------------------------------------------------------------------------
# The three things the platform asks Jira
# ---------------------------------------------------------------------------


def project_issue_types(project_key):
    """Issue types that actually exist in a project.

    Called before any search, and the reason is a specific failure: if
    PLATFORM_JIRA_CHANGE_ISSUETYPE names a type the project does not have, a
    JQL search for it returns zero issues, and an engine that trusted that
    would report EVERY deployment as having no change record. A configuration
    mistake would read as an estate-wide compliance failure.

    open-items.md B9 -- the golden Jira scheme -- is what settles which type is
    correct. Until it does, this check is what stops a wrong guess looking like
    a finding.
    """
    data = _get(f"/rest/api/3/project/{urllib.parse.quote(project_key)}")
    return [t.get("name") for t in (data.get("issueTypes") or [])]


def search(jql, fields=None, max_results=100):
    """JQL search. Returns the raw issue list.

    Paginated deliberately conservatively: a reconciliation window is hours,
    not months, so a run that needs more than a few hundred issues is a signal
    that the window or the JQL is wrong rather than a reason to page through
    thousands.
    """
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ",".join(fields or ["summary", "status", "issuetype",
                                      "created", "updated", "reporter",
                                      "description", "labels"]),
    }
    data = _get("/rest/api/3/search/jql", params)
    return data.get("issues") or []


def issue_with_changelog(issue_key):
    """One issue plus its full history.

    The changelog is what makes design 08's condition 4 checkable -- "the issue
    has not been reopened or edited since approval". The status field alone
    cannot answer it: an issue can be approved, edited, and still show
    "Approved", which is exactly the case design 08 warns about when it says
    Jira history can be altered.
    """
    return _get(f"/rest/api/3/issue/{urllib.parse.quote(issue_key)}",
                {"expand": "changelog"})


def health():
    """Can we talk to Jira at all, and as whom.

    Used by reconciliation before it concludes anything, and by
    scripts/jira-check.sh. Returns (ok, detail) rather than raising, because
    the caller's next move is to record the answer either way.
    """
    try:
        me = _get("/rest/api/3/myself")
        return True, {
            "account_id": me.get("accountId"),
            "display_name": me.get("displayName"),
            "site": _site(),
        }
    except JiraUnavailable as exc:
        return False, {"error": str(exc), "site": _site()}
