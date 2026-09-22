# Estate dump

`estate.json` is a single-file answer to *what have we actually built* — the
live AWS estate and the code that produced it, in one place, so nobody has to
open eight consoles or read fifty templates to find out.

Regenerate it with:

```bash
scripts/dump-estate.sh
```

It sweeps every active account in the Organization across `us-east-1`,
`us-east-2` and `us-west-2`, then appends the repository's own templates,
Lambda source, policies, scripts and Terraform.

## Why both halves are in it

The templates say what *should* exist and AWS says what *does*. Either alone
answers a narrower question than people think they are asking, and the
interesting part is usually the gap between them — a StackSet that reports
`CURRENT` in six accounts and never reached the seventh looks identical, from
the repository side, to one that reached all seven.

## What is deliberately not in it

Organization policy and `CLAUDE.md` both forbid personal data and secrets in
this repository, and a file like this is the obvious way to defeat both by
accident. So:

| excluded | what you get instead |
|---|---|
| Secrets Manager values | name, description, KMS key, rotation state |
| SSM parameter values | name and type, so "is it there and is it encrypted" is answerable |
| AWS alternate contacts | nothing — the API is never called |
| Root account emails | nothing |
| Identity Center membership | nothing. Group and permission set *names* live in the repository and are not personal data |
| SNS email and SMS endpoints | the protocol and that a subscription exists. The endpoint is a person |
| DynamoDB table contents | the table, its keys, its item count — never its rows |
| High-entropy Lambda env values | redacted by heuristic, as defence in depth |

Every collector names the fields it records; nothing dumps a raw API response.
That is the safety argument: a deny-list of sensitive fields fails the first
time AWS adds one, silently, into a file somebody is about to share. An
allow-list fails by omitting something useful, which is visible and harmless.

## The redaction is checked, not trusted

Before writing, the generator runs two checks and **refuses to write** if
either fails.

**Against the real personal data.** It scans the finished document for every
value in `config/contacts.env` and `config/identity.env` — gitignored for
exactly that reason. Group and permission set names are exempt, because
CLAUDE.md says those belong in the repository; the exemption is recorded in
the document under `redaction_check_exemptions` rather than silently applied.

**Against email addresses generally.** Every email-shaped string is treated as
a person unless it is an AWS service principal, an `example.*` documentation
fixture, or one of the `mspr+`/`platform@` role addresses the naming
convention specifies.

The second check exists because the first one was not enough. On the first
clean run a direct personal address — sitting in a *comment* in a committed
script — reached the document through the repository section and passed,
because a check that compares against two specific files has no reason to look
anywhere else. The right fixture for "did the collectors leak" is the wrong
fixture for "is there personal data in here at all."

If either file is missing, the check reports that it could not run rather than
passing. "Found nothing" and "looked at nothing" produce the same output
otherwise, which is the failure this codebase keeps rediscovering.

`redaction_verified: true` in the document means the check ran and found
nothing. Its absence means the file should not be shared.

## Read `errors` before believing an empty section

The sweep takes several minutes and will outlive an SSO session. A collector
that fails records an entry in `errors` and the sweep continues, because
aborting would throw away everything gathered so far for one expired token.

An account that could not be assumed carries `unreachable` instead of
`regions`. A collector that failed leaves an empty list — which looks exactly
like a region with nothing in it. The `errors` array is the only way to tell
those apart.
