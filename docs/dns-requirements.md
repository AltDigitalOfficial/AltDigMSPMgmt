# DNS records required for `altdigital.ai`

**Written for:** whoever administers the `altdigital.ai` DNS zone. Nothing here
needs AWS knowledge — each row is a record to create, and the reason is given
so the request can be judged rather than just actioned.

**Why this exists.** The platform sends one email: the instrumentation digest,
which goes to each client's named technical contact. Design doc 06 makes that
digest the documented evidence that monitoring requirements were communicated —
*"if something later fails unmonitored, the trail shows we asked."* AWS SES will
not send from a domain it cannot verify the sender controls, and verification is
DNS.

Until these records exist the digest runs in **report-only** mode: it gathers,
renders and logs, and sends nothing. That is deliberate and safe, but it
produces no evidence.

---

## Status

| Item | State |
|---|---|
| SES identity for `altdigital.ai` | **created**, `us-east-2`, Platform Tooling account `751479507989` |
| Verification | **pending** — waiting on the records below |
| Digest sender | report-only until verification completes |

---

## 1. DKIM — three CNAME records (required)

These prove the domain is ours and sign outbound mail so receiving servers do
not treat it as forged. SES generated them; they are specific to this identity
and cannot be guessed or reused.

| Type | Name | Value |
|---|---|---|
| CNAME | `rdrpb72bvywdulqb5rfv2x6uatho57uu._domainkey.altdigital.ai` | `rdrpb72bvywdulqb5rfv2x6uatho57uu.dkim.amazonses.com` |
| CNAME | `ioask4howkzus3rorqjx73qbwulrt7nw._domainkey.altdigital.ai` | `ioask4howkzus3rorqjx73qbwulrt7nw.dkim.amazonses.com` |
| CNAME | `4hwrxc3jus6p7bryb4c4jbwbcvso27ei._domainkey.altdigital.ai` | `4hwrxc3jus6p7bryb4c4jbwbcvso27ei.dkim.amazonses.com` |

**All three.** SES marks the identity verified only when all three resolve, and
rotates them periodically — so leaving one out fails verification without
saying which.

Some DNS providers append the zone name automatically. If yours does, enter
only the part before `.altdigital.ai`. A doubled suffix
(`..._domainkey.altdigital.ai.altdigital.ai`) is the usual reason verification
never completes.

---

## 2. Custom MAIL FROM — two records (recommended, not required)

Without these, the envelope sender is an `amazonses.com` address while the
visible From is `altdigital.ai`. Mail still delivers, but SPF aligns to Amazon
rather than to us, which weakens DMARC and looks like a mismatch to a recipient
who checks.

Suggested subdomain `mail.altdigital.ai`:

| Type | Name | Value | Priority |
|---|---|---|---|
| MX | `mail.altdigital.ai` | `feedback-smtp.us-east-2.amazonses.com` | 10 |
| TXT | `mail.altdigital.ai` | `"v=spf1 include:amazonses.com ~all"` | — |

**Region-specific.** `us-east-2` is in the MX value because that is where the
identity lives. Moving SES regions later means changing this record.

---

## 3. DMARC — one TXT record (recommended)

| Type | Name | Value |
|---|---|---|
| TXT | `_dmarc.altdigital.ai` | `"v=DMARC1; p=none; rua=mailto:dmarc@altdigital.ai"` |

**`p=none` deliberately.** It asks receivers to report but not to reject, so
the reports can be read before any policy is enforced. Going straight to
`p=reject` on a domain that also sends via Microsoft 365 risks silently
discarding legitimate business mail — and the failure is invisible to the
sender.

Tighten to `quarantine` and then `reject` once the reports show only expected
sources. That is a decision for Art rather than a platform setting.

**Check whether a `_dmarc` record already exists.** If `altdigital.ai` sends
through Microsoft 365, one probably does, and it must be merged rather than
replaced — a second TXT record at the same name is a configuration error and
receivers ignore both.

---

## What happens after the records are added

1. SES verifies automatically, usually within minutes, occasionally up to 72
   hours depending on propagation.
2. Confirm:
   `aws sesv2 get-email-identity --email-identity altdigital.ai --region us-east-2`
   — `VerifiedForSendingStatus` becomes `true`.
3. The digest is redeployed with `DigestSender` and `DigestCc` set, which takes
   it out of report-only mode.

---

## Two things worth deciding at the same time

**The sending address.** `platform@altdigital.ai` is the obvious choice. It
should be a monitored mailbox, not a no-reply: design doc 06 ends the digest
with an invitation to reply asking for different monitoring, and that
invitation is dishonest if nobody reads the replies.

**The CC address.** Every digest is copied to AltDigital so that a bounce or a
departed contact is visible to us rather than lost. Without it a rotted contact
is invisible — the send succeeds, the mailbox is gone, and the evidence trail
claims a communication that did not happen. Worse than no record, because a
false one is harder to spot.

---

## Sandbox

A new SES account starts in the sandbox: it can only send **to verified
addresses**, and at low volume. Verifying the domain does not lift that.

So after verification the first digests will still fail to reach an unverified
client contact. Moving out of the sandbox is a support request to AWS
describing the sending use case, and it is approved on the basis of what you
tell them — worth submitting early, since the answer is not instant and the
description ("transactional monitoring digests to named contacts at customer
organisations, low volume, recipients under contract") is straightforward.

Tracked as **B-028**.
