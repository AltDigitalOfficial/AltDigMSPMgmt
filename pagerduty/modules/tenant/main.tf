# One client's PagerDuty footprint: a business service per tier, a technical
# service per object, and the dependencies between them.
#
# ---------------------------------------------------------------------------
# Business and technical services are different kinds of thing
# ---------------------------------------------------------------------------
#   technical service   INGESTS events and can page. Has an integration and an
#                       escalation policy. One per monitored OBJECT.
#   business service    is SUBSCRIBED to and notifies. Ingests nothing, has no
#                       integration. One per TIER, representing "this client's
#                       dev environment" as a capability.
#
# Getting this backwards is not a naming mistake. A business service cannot
# receive events at all, so routing alarms at one produces events that vanish
# silently, with no error anywhere.
#
# The dependency is what makes the pair useful: an incident on any technical
# service rolls up to the tier's business service, so "is ACME's UAT healthy"
# is answerable without knowing which objects are underneath it.
#
# ---------------------------------------------------------------------------
# Every tier, not production only -- D-018
# ---------------------------------------------------------------------------
# design 13 says "Service per application (production only)". This creates
# services for every tier, which is a deliberate departure recorded in
# docs/deviations.md.
#
# The design's reasoning is sound and incompletely applied: a dev account that
# pages the production rotation trains that rotation to ignore pages, and the
# habit does not stay confined to dev. But the conclusion drawn from it -- that
# non-production gets no service at all -- confuses "must not page the on-call
# rotation" with "nobody is told".
#
# A dev environment is the developer's own production. When it breaks, their
# work stops, and the person who needs to know is the developer or whoever
# supports their infrastructure. Creating no service means that alert has
# nowhere to go and the failure is discovered by someone noticing.
#
# So every tier gets services, and the SEPARATION is expressed as urgency and
# escalation policy rather than as absence. This mirrors what the platform
# already does for itself with its own low-urgency service.

terraform {
  required_providers {
    pagerduty = {
      source  = "PagerDuty/pagerduty"
      version = "~> 3.0"
    }
  }
}

locals {
  # The reserved object every tier carries. B-127.
  #
  # Inside one tenant account two teams own resources: AltDigital owns the VPC,
  # KMS keys, log groups and backup vault the baseline puts there, and a
  # separate team owns the application. They page different people even when
  # both teams work for the same company. This is the service AltDigital's half
  # routes to, and `platform-layer: infrastructure` on the resource is what
  # selects it.
  #
  # Added HERE rather than in scripts/render-pagerduty-tenants.sh on purpose.
  # The renderer derives objects from the `app` field on each account, so an
  # account with no app, or a renderer that changed, could produce a tier with
  # no infrastructure service -- and the failure would be silent: infrastructure
  # alarms would fall back to the platform's own service and page the right
  # people for the wrong reason, until the first tenant where that was not the
  # same thing.
  infra_object = "infra"

  # Flattened tier x object, because Terraform has no nested for_each. The key
  # is "<tier>/<object>" and an empty object is a real key meaning the
  # account's single unnamed application.
  services = merge([
    for tier, cfg in var.tiers : {
      for obj in distinct(concat(cfg.objects, [local.infra_object])) :
      "${tier}/${obj}" => { tier = tier, object = obj }
    }
  ]...)

  # Written into every description rather than living only in Terraform state.
  # Somebody looking at this service in the PagerDuty console at 3am should be
  # able to see that it is managed here, and whose client it is, without going
  # to find out. The drift detector also keys on this string -- the account
  # holds services this configuration does not own, and a sweep that treated
  # everything it found as drift would report other people's work.
  provenance = "Managed by AltDigital platform Terraform (pagerduty/modules/tenant). Client ${var.tenant_id}, partner ${var.partner}. Console edits are drift and are reported."
}

# ---------------------------------------------------------------------------
# One business service per tier
# ---------------------------------------------------------------------------

resource "pagerduty_business_service" "tier" {
  for_each = var.tiers

  name             = "tenant-${var.tenant_id}-${each.key}"
  description      = "${var.tenant_id} ${each.key} environment, as a capability. Technical services for its objects roll up here. ${local.provenance}"
  point_of_contact = var.commercial_point_of_contact
}

# ---------------------------------------------------------------------------
# One technical service per object
# ---------------------------------------------------------------------------

resource "pagerduty_service" "object" {
  for_each = local.services

  name = "tenant-${var.tenant_id}-${each.value.tier}-${each.value.object == "" ? "app" : each.value.object}"
  description = join(" ", [
    each.value.object == local.infra_object
    ? "AltDigital-owned infrastructure alarms for"
    : "${each.value.object == "" ? "Application" : each.value.object} alarms for",
    "${var.tenant_id} ${each.value.tier}.",
    local.provenance,
  ])

  # Infrastructure escalates to AltDigital REGARDLESS of tier, because the
  # thing that determines who answers is which team owns the resource, not
  # which environment it is in. A tenant's dev-tier VPC failing is still
  # AltDigital's to fix.
  #
  # Everything else takes the per-tier override, falling back to the platform
  # policy. B-117 will replace that fallback for non-production once Jamie and
  # Wayne settle what should happen there.
  escalation_policy = (
    each.value.object == local.infra_object
    ? coalesce(var.infrastructure_escalation_policy_id, var.default_escalation_policy_id)
    : lookup(var.tier_escalation_policy_ids, each.value.tier, var.default_escalation_policy_id)
  )

  alert_creation          = "create_alerts_and_incidents"
  auto_resolve_timeout    = var.auto_resolve_timeout_minutes == 0 ? "null" : tostring(var.auto_resolve_timeout_minutes * 60)
  acknowledgement_timeout = tostring(var.acknowledgement_timeout_minutes * 60)

  incident_urgency_rule {
    type = "constant"
    # Low still NOTIFIES. See variables.tf: the tier separation is urgency,
    # not silence.
    # Infrastructure keeps the tier's urgency like everything else. The layer
    # decides WHO is woken, not how loudly -- a dev-tier infrastructure fault is
    # still a dev-tier fault, and making it high because AltDigital owns it
    # would page us at 3am for an environment design 14 gives no SLA.
    urgency = lookup(var.tier_urgency, each.value.tier, "low")
  }
}

# ---------------------------------------------------------------------------
# CloudWatch integration -- the routing key the alarm layer uses
# ---------------------------------------------------------------------------
# The vendor integration, not a generic Events API v2 one, for the same reason
# the platform service uses it: the vendor integration understands the SNS
# envelope and produces an incident titled with the alarm name. A generic
# integration renders the whole SNS payload as an unparsed blob, which is what
# a woken engineer would then be reading.

data "pagerduty_vendor" "cloudwatch" {
  name = "Amazon CloudWatch"
}

resource "pagerduty_service_integration" "cloudwatch" {
  for_each = local.services

  name    = "AWS CloudWatch"
  service = pagerduty_service.object[each.key].id
  vendor  = data.pagerduty_vendor.cloudwatch.id
}

# ---------------------------------------------------------------------------
# Objects roll up to their tier
# ---------------------------------------------------------------------------
# The relationship is what makes the business service answer a question. More
# nuanced relationships are expected -- an object depending on another object,
# a tier depending on a shared service -- and they attach here without
# disturbing what exists, because a dependency is its own resource.

resource "pagerduty_service_dependency" "object_to_tier" {
  for_each = local.services

  dependency {
    dependent_service {
      id   = pagerduty_business_service.tier[each.value.tier].id
      type = "business_service"
    }
    supporting_service {
      id   = pagerduty_service.object[each.key].id
      type = "service"
    }
  }
}

# ---------------------------------------------------------------------------
# The client-conversation business service -- commercial, not operational
# ---------------------------------------------------------------------------
# Separate from the per-tier services and deliberately not one of them.
# design 12's client-conversation class is defined by who needs to have a
# conversation, not by which environment broke; a commercial contact
# subscribing to it should not also inherit every dev alert.

resource "pagerduty_business_service" "conversations" {
  name             = "tenant-${var.tenant_id}-conversations"
  description      = "Client-conversation event class for ${var.tenant_id} (design 12). Subscribed to, never paged from. ${local.provenance}"
  point_of_contact = var.commercial_point_of_contact
}
