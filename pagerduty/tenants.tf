# Per-tenant PagerDuty, driven by DATA rather than by generated code.
#
# ---------------------------------------------------------------------------
# Why the saga renders a tfvars file and not a .tf file
# ---------------------------------------------------------------------------
# Provisioning step 3 writes `tenants.auto.tfvars.json` from the registry. It
# does not write Terraform.
#
# Generated .tf is reviewable only by reading Terraform, diffs noisily when the
# generator's formatting changes, and makes it possible for a generator bug to
# create a resource nobody intended. Generated DATA can only populate the
# shapes this file already declares: a generator bug produces a wrong value,
# never a wrong resource. Adding a tenant becomes a one-line diff in a JSON
# map, which is something Art or Wayne can read in a pull request without
# knowing Terraform.
#
# ---------------------------------------------------------------------------
# Why tenants.json and not tenants.auto.tfvars.json
# ---------------------------------------------------------------------------
# The tfvars name was the obvious choice and is the wrong one. Both .gitignore
# and the pre-commit hook block `*.tfvars.json`, because tfvars files here hold
# responder email addresses -- personal data under CLAUDE.md.
#
# This file holds client slugs, partner slugs and app slugs. None of that is
# personal data, all of it is already in the registry, and it MUST be
# reviewable: the thing a reviewer needs to see is which clients exist and
# which of them page, and hiding the estate's paging surface in a gitignored
# file would put it beyond review entirely.
#
# The fix is a rename rather than an exception. Adding `!tenants.auto.tfvars.json`
# to the ignore rules would weaken a data-protection control to make one safe
# file convenient, and the next tfvars file to want an exception would have a
# precedent. A plain .json read through jsondecode gets the same result and
# leaves the control intact.

locals {
  tenants_file = "${path.module}/tenants.json"

  # Absent is treated as empty, and that is a deliberate choice rather than
  # laziness: a fresh clone that has never run the renderer should plan
  # cleanly rather than fail on a missing file. The renderer reports how many
  # registry records it examined, so "no clients" is distinguishable from
  # "never looked" at the point where that distinction can still be acted on.
  tenants = fileexists(local.tenants_file) ? jsondecode(file(local.tenants_file)).tenants : {}
}

module "tenant" {
  source   = "./modules/tenant"
  for_each = local.tenants

  tenant_id = each.key
  partner   = each.value.partner
  tiers     = each.value.tiers

  # Every tier falls back to the platform escalation policy today.
  #
  # design 13 says the escalation policy references "the on-call schedule",
  # singular, and there is one. A per-tier policy over the same schedule would
  # be separation in name only -- the same phone rings either way -- so none is
  # created until there is a second rotation to point at.
  #
  # Non-production escalation is an open question owned by Jamie and Wayne
  # (B-117). Dev is the developer's own production, so the answer is probably a
  # different policy rather than no policy; `tier_escalation_policy_ids` is
  # where it lands, as data.
  default_escalation_policy_id = pagerduty_escalation_policy.platform.id

  # Per-application support teams (2026-10-01). Keys in terraform.tfvars are
  # "<client>/<app>"; each tenant receives only its own, keyed by app.
  application_support = {
    for key, cfg in var.application_support : split("/", key)[1] => cfg
    if split("/", key)[0] == each.key
  }
  platform_schedule_id = pagerduty_schedule.platform.id

  acknowledgement_timeout_minutes = var.acknowledgement_timeout_minutes
  auto_resolve_timeout_minutes    = var.auto_resolve_timeout_minutes
}
