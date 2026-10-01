<!--
For AltDigital — remove this block before sending.

Written for the client's owner: a business owner, not a technologist. First
use: OEight / Arc8, 2026-09-30. Reusable for any tenant: replace each
[BRACKETED] value.

Why it exists: AltDigital's key-deletion quorum (design 03, "two approvals from
{Jamie, Wayne, Art}") is an intra-AltDigital control. Where AltDigital people
also act for the client, as with OEight, the quorum alone gives the client no
independent say over the one action that destroys its data permanently. This
adds the client's written approval as a precondition.

Have Art (and counsel, if the client's BAA or consulting agreement is being
revised at the same time) review the wording before first use. This is not
legal advice.

The signed copy is evidence. File it in both Truveon workspaces (the client's
and AltDigital's), not in the repository.
-->

# Consent to the deletion of encryption keys

**Between** AltDigital, LLC ("AltDigital") **and** [CLIENT LEGAL NAME]
("the Client"), for the application **[APPLICATION NAME]**.

---

## What this is about, in plain terms

Everything [APPLICATION NAME] stores in AWS is **encrypted**. The data is
scrambled, and only a matching **encryption key** can unscramble it. AltDigital
manages those keys for you, inside your own AWS accounts.

**Deleting a key destroys every piece of data in your accounts that was locked
with it.** Not just the key, the data too, permanently. Nobody can reverse it:
not you, not AltDigital, not Amazon.

One thing it does **not** destroy: the backup copies we keep in a separate,
locked backup account. Those are protected by that account's own keys, so they
survive a key deletion in your accounts, and they are destroyed only when their
own retention period ends (at least 35 days). If you ask us to destroy your
data, we will tell you exactly when the last of those copies expires.

That makes key deletion the single most consequential action anyone can take
on your environment. This document makes sure it can never happen without your
written approval.

## When a key would ever be deleted

Rarely, and on purpose. The usual reasons are:

- **You leave**, and you ask us to destroy your data rather than return it.
- **A retention period ends** and the data must be destroyed, by law or by
  contract.
- **You ask us to**, for example to destroy a specific dataset.

Our platform **never** deletes a key automatically. Our security automation
isolates and preserves; it does not destroy. A key is only ever deleted
because people decided to delete it.

## How a deletion is approved

All four of these must happen, in this order, before a key can be scheduled for
deletion:

1. **A written request**, saying which key, what data it protects, and why.
2. **Your written approval**, from **[CLIENT APPROVER — the owner, or a person
   the owner names in writing]**. Approval from anyone who works for AltDigital
   does not count as your approval, even if they hold an account or a title
   at [CLIENT LEGAL NAME].
3. **Two approvals inside AltDigital**, from two different people among its
   named principals, neither of whom made the request.
4. **The request, your approval and AltDigital's approvals are recorded** as
   compliance evidence before anything is done.

## After approval: a 30-day safety window

When a key is scheduled for deletion, AWS **waits 30 days** before destroying
it. During those 30 days:

- the deletion **can be cancelled**, and the data is fully recoverable;
- the key **cannot be used**, so anything that relies on it stops working.
  That is deliberate: it makes a mistake visible while it can still be undone;
- we tell you on the day the deletion is scheduled, and again
  **[7] days** before it becomes final.

After 30 days the deletion is final.

## What you are agreeing to

By signing, the Client agrees that:

1. AltDigital will not delete, or schedule the deletion of, any encryption key
   protecting the Client's data without the Client's written approval as set
   out above.
2. Once a deletion completes, the data protected by that key is **permanently
   unrecoverable by anyone**, and the Client accepts that consequence for any
   deletion it has approved.
3. The Client's approver is **[CLIENT APPROVER]**. The Client will tell
   AltDigital in writing if that changes. Until it does, approval from anyone
   else, including AltDigital personnel who represent the Client to others,
   is not the Client's approval.
4. If the Client cannot be reached, **no key is deleted.** There is no
   emergency exception. Anything urgent is handled by disabling access, which
   can be undone, never by deletion.

And AltDigital agrees that:

1. It will follow the four approval steps above, every time, and keep the
   record of each as compliance evidence.
2. It will notify the Client when a deletion is scheduled and before it
   becomes final, and will cancel it if the Client asks during the 30 days.
3. It will never delete a key as part of automated security or operational
   response.

---

| | AltDigital, LLC | [CLIENT LEGAL NAME] |
|---|---|---|
| Name | | |
| Title | | |
| Signature | | |
| Date | | |
