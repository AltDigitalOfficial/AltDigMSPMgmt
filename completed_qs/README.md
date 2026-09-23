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

Anything. Nothing reads these files automatically. `client-slug.md` matching
question 1.3 is the obvious choice and makes the directory sort usefully.
