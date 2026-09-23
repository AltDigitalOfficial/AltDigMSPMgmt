# Completed questionnaires

Filled-in copies of
[docs/client-onboarding-questionnaire.md](../docs/client-onboarding-questionnaire.md)
go here.

**Everything in this directory is gitignored.** This README is the only file
that is not.

## Why the whole directory

A completed questionnaire carries a client's full developer roster — names,
email addresses, phone numbers — alongside a description of their architecture,
their compliance scope and their known failure modes. It is the richest single
piece of personal data this repository will ever sit next to.

`config/contacts.env` and the other personal-data files are matched by
filename, because their names are predictable. A completed questionnaire will
be called whatever the client is called, so there is nothing to match on. The
boundary has to be the directory, and anything inside it is assumed personal.

Two layers, matching how the rest of the repository handles this:

- `.gitignore` excludes `completed_qs/*`
- the pre-commit hook blocks anything staged under `completed_qs/`, which
  catches `git add -f` and an edited ignore rule

## Gitignored is not the same as stored safely

This is worth being explicit about, because the directory's existence invites
the opposite assumption.

`.gitignore` is a **commit** control. It is not a **storage** control. A
completed questionnaire in this directory is still:

- on a laptop, in whatever that laptop's backup reaches
- inside any archive of the repository folder
- readable by anything with filesystem access, including editor plugins and
  assistants indexing the working tree

So this directory is a **working location, not a system of record.** The
authoritative copy of a completed questionnaire belongs in AltDigital's
document management, with access control and retention that a `.gitignore`
cannot provide. Delete the local copy when the tenant is vested and its answers
have been transferred into `config/contacts.env`, `config/identity.env` and the
tenant manifest.

## What to name them

`client-slug.questionnaire.yaml`, matching question 1.3. The directory then
sorts usefully and the two files for one client sit together.

## The developer roster is a second file

Questionnaire section 3b — the developer roster — lives **beside** the
questionnaire rather than inside it, as `client-slug.roster.yaml`.

`vesting/roster.example.yaml` is the template and
`vesting/roster.schema.json` is the schema. Both are committed, because
neither contains a real person.

Two reasons for the split, and the second is the better one:

- `vesting/questionnaire.schema.json` sets `additionalProperties: false` at
  every level, so there is nowhere in the manifest to put a roster without
  amending the schema — and the questionnaire has a committed example copy,
  which is exactly the file a roster must never be able to reach.
- **The lifetimes differ.** A questionnaire is answered once and is largely
  irreversible. A roster changes every time somebody joins or leaves. Treating
  them as one document means re-opening the irreversible answers to add a
  contractor.

The roster repeats the tenant's partner, client and application slugs, and
`scripts/derive-roster.py` refuses to run unless all three match the
questionnaire. That check is the reason the repetition is there: a roster
copied from the last client and edited carelessly would otherwise grant real
people access to another client's accounts, and nothing downstream would
notice — the accounts resolve, the groups are created, the assignments apply,
and the run reports success.

## What reads these files

Two scripts, both of which take the paths explicitly. Nothing scans the
directory.

```
scripts/onboard-client.sh --questionnaire completed_qs/<client>.questionnaire.yaml \
                          --roster        completed_qs/<client>.roster.yaml --dry-run
```

`onboard-client.sh` runs design 13's whole sequence;
`scripts/grant-developer-access.sh` does the roster half on its own when
somebody joins or leaves.
