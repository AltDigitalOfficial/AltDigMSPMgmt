# The routing keys are the reason this module has outputs at all.
#
# design 13: "Routing keys -> Secrets Manager, referenced by the alarm layer."
# A key exists only after an apply, so it cannot be known in advance and cannot
# be stored by hand -- which is why sync-pagerduty-tenants.sh runs after the
# apply rather than before it.
#
# Marked sensitive. A routing key IS a credential: anyone holding one can raise
# incidents on the service. Terraform still writes it to state in cleartext --
# unavoidable, and why pagerduty/terraform.tfstate is gitignored and blocked by
# the pre-commit hook -- but `sensitive` keeps it out of plan output, which is
# the place it would otherwise leak from casually.

output "conversations_business_service_id" {
  value       = pagerduty_business_service.conversations.id
  description = "One per client, commercial rather than operational. Subscribed to, never paged from."
}

output "tiers" {
  description = "Business service id per environment tier."
  value       = { for tier, bs in pagerduty_business_service.tier : tier => bs.id }
}

output "objects" {
  sensitive   = true
  description = "Technical services keyed by \"<tier>/<object>\". An empty object is the account's single unnamed application."
  value = {
    for key, svc in pagerduty_service.object : key => {
      tier        = local.services[key].tier
      object      = local.services[key].object
      service_id  = svc.id
      routing_key = pagerduty_service_integration.cloudwatch[key].integration_key
      urgency     = lookup(var.tier_urgency, local.services[key].tier, "low")
    }
  }
}

output "counts" {
  description = <<-EOT
    Emitted unredacted so a consumer can tell "this client has no service in
    that tier because the tier does not exist" from "it should have one and the
    apply did not create it".

    Those produce the same absent entry and mean opposite things. A sync step
    unable to distinguish them would either skip a real failure or report a
    phantom one for every tier a client simply does not run -- the B-086 shape,
    where zero examined and zero found read identically.
  EOT
  value = {
    tiers   = length(pagerduty_business_service.tier)
    objects = length(pagerduty_service.object)
  }
}
