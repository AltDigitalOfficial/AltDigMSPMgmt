"""Builds the Truveon handoff pack for one client.

Called by scripts/truveon-tenant-handoff.sh, which does the AWS reads. This
file does no I/O beyond reading its inputs and writing the pack.

    truveon_handoff.py --questionnaire Q [--roster R] --derived D.json
                       --registry-accounts A.txt --pipeline-account ID
                       --out DIR

Why a pack and not an API call
------------------------------
Truveon cannot create a tenant over an API, deliberately (Truveon D-26): tenant
creation is DDL, and the application's database role is built never to hold
DDL rights. So a person runs Truveon's operator CLI. This produces everything
that person needs, so the run is a set of commands rather than a transcription
exercise -- and so it is the same run every time.

What goes where, and why it matters
-----------------------------------
    manifest.json       tenant config, applications, accounts. NO personal data.
    legal-entities.csv  company facts. NO personal data.
    users.csv           the Truveon roster. PERSONAL DATA.
    commands.sh         the exact commands. NO personal data.
    PROMPT.md           a Claude Code prompt for the Truveon repository.
                        NO personal data.

The split is the point. PROMPT.md and commands.sh are the files most likely to
be read into an assistant's context or pasted into a chat, so they refer to
users.csv by path and never reproduce what is in it -- the owner included,
which is why Truveon takes the owner from a column in users.csv rather than
from --owner-email on the command line.

The whole pack is written under completed_qs/, which is gitignored as a
directory and blocked by the pre-commit hook.
"""
import argparse
import csv
import datetime
import json
import pathlib
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROSTER_SCHEMA = REPO_ROOT / "vesting" / "truveon-roster.schema.json"

MANIFEST_VERSION = 1

# Questionnaire 2.1 frameworks that carry a notification window, and the
# Truveon window kind each maps to. Only the BAA window is asked for in the
# questionnaire today (baa_window_partner_to_client_hours); the others are set
# in Truveon during the walkthrough when a contract supplies them.
BAA_KIND = "baa"


def fail(msg):
    sys.exit(msg)


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def validate_roster(roster, q, creating):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        fail("jsonschema is required: python -m pip install --user jsonschema")
    schema = json.loads(ROSTER_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(roster),
                    key=lambda e: list(e.path))
    if errors:
        # The path, never the value. A validation message that echoed the
        # offending value would print an email address to the terminal.
        fail("The Truveon roster does not match vesting/truveon-roster.schema.json:\n"
             + "\n".join(f"    {'/'.join(str(p) for p in e.path) or '(root)'}: "
                         f"{e.validator} check failed" for e in errors))

    ident = q["identity"]
    tenant = roster["tenant"]
    if (tenant["partner"], tenant["client_slug"]) != (ident["partner"], ident["client_slug"]):
        # Refused, not warned. A roster applied to the wrong client gives real
        # people accounts in another client's compliance evidence.
        fail(f"The Truveon roster names {tenant['partner']}/{tenant['client_slug']}; "
             f"the questionnaire is for {ident['partner']}/{ident['client_slug']}. "
             "Refusing: a roster applied to the wrong client grants people access "
             "to another client's evidence.")

    emails = [u["email"].strip().lower() for u in roster["users"]]
    dupes = sorted({e for e in emails if emails.count(e) > 1})
    if dupes:
        fail(f"The Truveon roster lists {len(dupes)} email address(es) more than once. "
             "Truveon matches accounts by email, so the second row would silently "
             "overwrite the first's role.")

    owners = [u for u in roster["users"] if u.get("owner")]
    if creating and len(owners) != 1:
        fail(f"Creating a Truveon tenant needs exactly one owner in the roster; "
             f"found {len(owners)}. The owner is Truveon's first administrator and "
             "is a deliberate choice, not the first row.")
    if not creating and owners:
        # Not fatal: the flag is simply meaningless for an existing tenant.
        print("  note: 'owner' is ignored for an existing tenant -- it already has one.",
              file=sys.stderr)


def registry_accounts(path, partner, client):
    """This client's accounts from `registry.sh list-accounts` output."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 5:
                continue
            account_id, p, c, env, state = parts[:5]
            if (p, c) != (partner, client) or state == "devested":
                continue
            if not (account_id.isdigit() and len(account_id) == 12):
                continue
            rows.append({"aws_account_id": account_id, "environment": env})
    return rows


def build_manifest(q, derived, accounts, pipeline_account, truveon_slug, truveon_name):
    ident = q["identity"]
    comp = q["compliance"]
    app_ref = ident["application_code"]

    windows = []
    baa_hours = comp.get("baa_window_partner_to_client_hours")
    if comp.get("phi") and baa_hours:
        windows.append({
            "kind": BAA_KIND,
            "label": "BAA breach notification, partner to client",
            "window_hours": int(baa_hours),
            "authority": "questionnaire 4 (baa_window_partner_to_client_hours)",
        })

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "altdigital-msp",
        "tenant": {"slug": truveon_slug, "name": truveon_name},
        # The platform's name for the same client. Informational on the Truveon
        # side; on this side it is the Secrets Manager key for the ingest key.
        "platform_tenant": f"{ident['partner']}-{ident['client_slug']}",
        "partner": ident["partner"],
        # Questionnaire 1.x service model. Truveon uses the same A/B/C letters.
        "service_model": ident["service_model"],
        # Derived, never asked: the union of the frameworks' floors
        # (vesting/derivation-rules.yaml 2.8). The same number the Log Archive's
        # Object Lock uses, so the two stores cannot disagree about how long
        # evidence must be kept.
        "evidence_retention_floor_days": int(derived["retention_floor_days"]),
        "retention_floor_driver": derived.get("retention_floor_driver"),
        # Informational. Frameworks are set up in Truveon during the white-glove
        # walkthrough, not pre-loaded -- Truveon's onboarding tool deliberately
        # does not load them.
        "frameworks": [f for f in comp["frameworks"] if f != "none"],
        "notification_windows": windows,
        "applications": [{"ref": app_ref, "name": ident["application_name"]}],
        "member_accounts": [dict(a, application_ref=app_ref) for a in accounts],
        # Registered in every client tenant. Records that describe the tenant's
        # pipeline rather than one of its accounts arrive claiming this account
        # (docs/truveon-ingestion-contract.md, "Records with no account"), and
        # registering it lets Truveon's own silence detection watch the
        # heartbeat.
        "pipeline_account": {"aws_account_id": pipeline_account,
                             "environment": "prod",
                             "purpose": "altdigital-evidence-pipeline"},
    }


PLATFORM_TENANT = "altdigital-platform"      # scripts/lib/evidence_tenants.py INTERNAL_TENANT
PLATFORM_TRUVEON_SLUG = "altdigital"         # provisioned in Truveon 2026-09-08
PLATFORM_RETENTION_DAYS = 365                # derivation-rules.yaml `default`; the Log Archive's lock


def platform_accounts(ssm_path, registry_path=None):
    """AltDigital's own accounts: the SAME union scripts/lib/evidence_tenants.py files under the internal
    tenant -- every SSM platform account, plus every registry account under the `altdigital` partner.

    The union matters. Built from SSM alone, the manifest missed the management account, which is in the
    registry but not under /platform/org/account/, and Truveon would have flagged every record from it as
    coming from an unregistered account. The environment comes from the registry where it has one (two
    platform accounts are dev), and defaults to prod otherwise.
    """
    envs = {}
    if registry_path:
        with open(registry_path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if (len(parts) >= 5 and parts[1] == "altdigital" and parts[4] != "devested"
                        and parts[0].isdigit() and len(parts[0]) == 12):
                    envs[parts[0]] = parts[3]
    ids = set(envs)
    with open(ssm_path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 12:
                ids.add(parts[-1])
    return [{"aws_account_id": i, "environment": envs.get(i, "prod")} for i in sorted(ids)]


def build_platform_manifest(accounts, pipeline_account):
    """The manifest for AltDigital's own estate, filed in Truveon's `altdigital` tenant.

    No questionnaire exists for it, so every value is stated here with its reason instead:

    - environments as the registry records them, prod where it records none;
    - no service model: A/B/C describe what AltDigital does FOR a client, and this is AltDigital;
    - 365-day floor: derivation-rules.yaml's default, which is also the Log Archive's Object Lock default,
      so the two stores agree about how long the platform's own evidence is kept;
    - no notification windows: those are contractual windows TO a client. AltDigital's own incident
      obligations to its partners live on the partner records, not here.
    """
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "altdigital-msp",
        "tenant": {"slug": PLATFORM_TRUVEON_SLUG, "name": "AltDigital"},
        "platform_tenant": PLATFORM_TENANT,
        "partner": None,
        "service_model": None,
        "evidence_retention_floor_days": PLATFORM_RETENTION_DAYS,
        "retention_floor_driver": "platform default",
        "frameworks": ["soc2"],
        "notification_windows": [],
        "applications": [],
        # The pipeline account is itself a platform account; listed once, as the pipeline account.
        "member_accounts": [a for a in accounts if a["aws_account_id"] != pipeline_account],
        "pipeline_account": {"aws_account_id": pipeline_account, "environment": "prod",
                             "purpose": "altdigital-evidence-pipeline"},
    }


def platform_commands_text(pack_win, msp_win):
    return f"""#!/usr/bin/env bash
# Truveon handoff for AltDigital's own estate -> Truveon tenant '{PLATFORM_TRUVEON_SLUG}'.
# Generated by scripts/truveon-tenant-handoff.sh prepare-platform. No personal data in this file.
# Run from the Truveon repository's infra/scripts directory, with the Truveon AWS profile.
set -euo pipefail
PACK="{pack_win}"
MSP="{msp_win}"

# 1. The tenant exists (provisioned 2026-09-08). Not created.

# 2. Configure: retention floor, platform accounts, the pipeline heartbeat's cadence. Idempotent.
./87-apply-msp-manifest.sh --manifest "$PACK/manifest.json"

# 3. Mint the evidence-pipeline ingest key and store it for the platform tenant. Once.
./87-apply-msp-manifest.sh --manifest "$PACK/manifest.json" --mint-ingest-key msp-evidence-pipeline \\
  | ( cd "$MSP" && bash scripts/truveon-tenant-handoff.sh store-key --partner altdigital --client platform --truveon-tenant {PLATFORM_TRUVEON_SLUG} )

# 4. Confirm from the platform side:
( cd "$MSP" && bash scripts/truveon-tenant-handoff.sh status --partner altdigital --client platform )
"""


def write_users_csv(path, roster):
    cols = ["email", "display_name", "title", "phone", "role", "team_code",
            "is_global_admin", "is_owner"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for u in roster["users"]:
            owner = bool(u.get("owner"))
            w.writerow({
                "email": u["email"].strip(),
                "display_name": u["display_name"],
                "title": u.get("title") or "",
                "phone": "",
                "role": u.get("role") or ("admin" if owner else "contributor"),
                "team_code": "",
                "is_global_admin": "true" if (owner or u.get("is_global_admin")) else "false",
                "is_owner": "true" if owner else "false",
            })


def write_entities_csv(path, entities):
    cols = ["ref", "parent_ref", "legal_name", "short_name", "entity_kind",
            "jurisdiction", "registration_number", "registered_address",
            "data_residency", "headcount_in_scope"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        for e in entities:
            w.writerow({c: e.get(c, "") or "" for c in cols})


def commands_text(pack_win, msp_win, manifest, creating, has_users, has_entities,
                  send_invites, partner, client):
    slug = manifest["tenant"]["slug"]
    name = manifest["tenant"]["name"].replace('"', '\\"')
    lines = [
        "#!/usr/bin/env bash",
        f"# Truveon handoff for {partner}/{client} -> Truveon tenant '{slug}'.",
        "# Generated by scripts/truveon-tenant-handoff.sh. No personal data in this file.",
        "#",
        "# Run from the Truveon repository's infra/scripts directory, with the Truveon",
        "# AWS profile. Step 3 pipes the ingest key straight into the platform's",
        "# Secrets Manager; it is never displayed and must never be copied by hand.",
        "set -euo pipefail",
        f'PACK="{pack_win}"',
        f'MSP="{msp_win}"',
        "",
    ]
    if creating:
        args = [f"--slug {slug}", f'--name "{name}"', '--owner-from-users-csv',
                '--users-csv "$PACK/users.csv"']
        if has_entities:
            args.append('--legal-entities-csv "$PACK/legal-entities.csv"')
        if send_invites:
            args.append("--send-invites")
        lines += [
            "# 1. Create the tenant (DDL -- the only step that needs the admin connection).",
            "./44-onboard-tenant.sh \\",
            "  " + " \\\n  ".join(args),
            "# Then, per 44-onboard-tenant.sh's own closing message, redeploy so the new",
            "# tenant's KMS key is wired in:  ./12-ecs.sh",
            "",
        ]
    else:
        lines += [
            f"# 1. Tenant '{slug}' already exists (questionnaire 16.1). Not created.",
        ]
        if has_users:
            lines += [
                "#    users.csv is NOT loaded by any command: Truveon's onboarding tool",
                "#    creates tenants and cannot add people to an existing one. Invite them",
                "#    from Truveon Settings > Members, working from users.csv.",
            ]
        lines.append("")
    lines += [
        "# 2. Configure: service model, retention floor, notification windows,",
        "#    applications and member accounts. Idempotent -- safe to re-run, and it",
        "#    MUST be re-run after vesting, when the pack is regenerated with accounts.",
        './87-apply-msp-manifest.sh --manifest "$PACK/manifest.json"',
        "",
        "# 3. Mint the evidence-pipeline ingest key and store it in the platform.",
        "#    Only needed once per tenant (and on rotation). stdout carries ONLY the",
        "#    key; the platform side reads it from the pipe without echoing it.",
        './87-apply-msp-manifest.sh --manifest "$PACK/manifest.json" --mint-ingest-key msp-evidence-pipeline \\',
        f'  | ( cd "$MSP" && bash scripts/truveon-tenant-handoff.sh store-key --partner {partner} --client {client} --truveon-tenant {slug} )',
        "",
        "# 4. Confirm from the platform side:",
        f'( cd "$MSP" && bash scripts/truveon-tenant-handoff.sh status --partner {partner} --client {client} )',
    ]
    return "\n".join(lines) + "\n"


def prompt_text(pack_win, msp_win, manifest, creating, has_users, partner, client):
    slug = manifest["tenant"]["slug"]
    accounts = manifest["member_accounts"]
    acct_note = (f"{len(accounts)} member account(s) + pipeline account"
                 if accounts else
                 "NO member accounts yet (pre-vesting run) -- pipeline account only; "
                 "re-run step 2 after vesting with the regenerated manifest")
    create_line = (
        f"1. CREATE tenant `{slug}`: run step 1 of commands.sh verbatim. It needs the "
        "admin DB connection (DDL; D-26). Afterwards run ./12-ecs.sh as the script's "
        "closing message says."
        if creating else
        f"1. Tenant `{slug}` EXISTS (questionnaire 16.1). Do not create. "
        + ("users.csv lists people to invite via Settings > Members -- tell the operator; "
           "do not open the file." if has_users else ""))
    return f"""# Truveon: onboard platform client {partner}/{client} -> tenant `{slug}`

Context: AltDigital MSP platform handoff. Run in the Truveon repo (provenir). Pack dir: `{pack_win}`. MSP repo: `{msp_win}`.
Contract: MSP `docs/truveon-ingestion-contract.md`. Manifest v{manifest['manifest_version']}: {acct_note}.

HARD CONSTRAINTS
- DO NOT open, print, summarise or quote `users.csv` or any email address. Personal data; org policy forbids it in any conversation. Pass the path only.
- The ingest key must NEVER appear in output, logs, files or chat. Step 3 pipes it; never run the mint without the pipe; never `echo`/`cat` it.
- `git status` in provenir first: 44/87 wrappers run the WORKING TREE's api/ against production. Uncommitted unrelated api/ changes = stop and report.
- Prod DB. No DDL outside step 1. No deletes.

STEPS (commands in `{pack_win}/commands.sh`; run each step separately, check exit code, stop on failure)
0. Verify `infra/scripts/87-apply-msp-manifest.sh` and `api/apply_msp_manifest.py` exist and `onboard_tenant.py` supports `--owner-from-users-csv`. If not: STOP, report which is missing. Do not improvise equivalents.
{create_line}
2. CONFIGURE: step 2 of commands.sh. Expect a per-item summary (config, windows, applications, accounts: created/unchanged). Report it.
3. KEY: step 3 of commands.sh exactly as written (pipe into MSP store-key). Success = MSP prints a fingerprint and `linked to Truveon tenant`. If the tenant already has an active `msp-evidence-pipeline` credential, ask the operator before minting another (rotation = mint, store, then revoke old).
4. VERIFY: step 4 of commands.sh. Report its output.

REPORT: per step -> ran/skipped, exit code, summary lines. Anything unexpected -> stop and ask; do not work around.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questionnaire")
    ap.add_argument("--roster")
    ap.add_argument("--derived")
    ap.add_argument("--registry-accounts")
    ap.add_argument("--platform-accounts",
                    help="AltDigital's own estate instead of a client: no questionnaire, no roster.")
    ap.add_argument("--pipeline-account", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pack-win", required=True)
    ap.add_argument("--msp-win", required=True)
    a = ap.parse_args()

    if not (a.pipeline_account.isdigit() and len(a.pipeline_account) == 12):
        fail("The pipeline (Audit) account id is not a 12-digit account id.")

    if a.platform_accounts:
        accounts = platform_accounts(a.platform_accounts, a.registry_accounts)
        if not accounts:
            fail("No 12-digit account ids in the platform account list. Refusing to write a manifest "
                 "that would register nothing.")
        manifest = build_platform_manifest(accounts, a.pipeline_account)
        out = pathlib.Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (out / "commands.sh").write_text(platform_commands_text(a.pack_win, a.msp_win),
                                         encoding="utf-8", newline="\n")
        print(json.dumps({
            "truveon_tenant": PLATFORM_TRUVEON_SLUG, "creating": False,
            "member_accounts": len(manifest["member_accounts"]), "users": 0, "legal_entities": 0,
            "retention_floor_days": PLATFORM_RETENTION_DAYS, "notification_windows": 0,
        }))
        return

    if not (a.questionnaire and a.derived and a.registry_accounts):
        fail("--questionnaire, --derived and --registry-accounts are required for a client pack.")
    q = load_yaml(a.questionnaire)
    tv = q.get("truveon")
    if not tv:
        fail("The questionnaire has no `truveon` section (section 16). It is optional "
             "in the schema so older questionnaires still validate, but a tenant cannot "
             "be prepared without it. See vesting/questionnaire.example.yaml.")
    creating = not tv["existing_tenant"]
    ident = q["identity"]

    roster = load_yaml(a.roster) if a.roster else None
    if creating and roster is None:
        fail("Creating a Truveon tenant needs a Truveon roster (--truveon-roster): the "
             "tenant's owner is chosen there. Start from vesting/truveon.roster.example.yaml.")
    if roster is not None:
        validate_roster(roster, q, creating)

    derived = json.loads(pathlib.Path(a.derived).read_text(encoding="utf-8"))
    accounts = registry_accounts(a.registry_accounts, ident["partner"], ident["client_slug"])
    slug = tv.get("tenant_slug") or ident["client_slug"]
    name = tv.get("tenant_name") or ident["client_name"]
    manifest = build_manifest(q, derived, accounts, a.pipeline_account, slug, name)

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    entities = tv.get("legal_entities") or []
    if entities:
        write_entities_csv(out / "legal-entities.csv", entities)
    if roster is not None:
        write_users_csv(out / "users.csv", roster)

    (out / "commands.sh").write_text(
        commands_text(a.pack_win, a.msp_win, manifest, creating, roster is not None,
                      bool(entities), bool(tv.get("send_invites")),
                      ident["partner"], ident["client_slug"]),
        encoding="utf-8", newline="\n")
    (out / "PROMPT.md").write_text(
        prompt_text(a.pack_win, a.msp_win, manifest, creating, roster is not None,
                    ident["partner"], ident["client_slug"]),
        encoding="utf-8", newline="\n")

    # Summary for the caller. Counts, never contents.
    print(json.dumps({
        "truveon_tenant": slug,
        "creating": creating,
        "member_accounts": len(accounts),
        "users": len(roster["users"]) if roster else 0,
        "legal_entities": len(entities),
        "retention_floor_days": manifest["evidence_retention_floor_days"],
        "notification_windows": len(manifest["notification_windows"]),
    }))


if __name__ == "__main__":
    main()
