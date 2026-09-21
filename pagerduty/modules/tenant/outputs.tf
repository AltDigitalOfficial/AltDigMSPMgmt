# The routing keys are the reason this module has outputs at all.
#
# design 13: "Routing keys -> Secrets Manager, referenced by the alarm layer."
# A key exists only after an apply, so it cannot be known in advance and cannot
# be stored by hand -- which is why sync-pagerduty-tenants.sh runs after the
# apply rather than before it.
#
# Marked sensitive. A routing key IS a credential: anyone holding one can raise
# incidents on the service, which means anyone holding one can page the
# rotation. Terraform still writes it to state in cleartext -- unavoidable, and
# why pagerduty/terraform.tfstate is gitignored and blocked by the pre-commit
# hook -- but `sensitive` keeps it out of plan output, which is the place it
# would otherwise leak from casually.

output "business_service_id" {
  value       = pagerduty_business_service.client.id
  description = "One per client. Subscribed to, never paged from."
}

output "services" {
  sensitive   = true
  description = "Production paging services keyed by app slug. The empty-string key is the production account with no app component."
  value = {
    for app, svc in pagerduty_service.app : app => {
      service_id  = svc.id
      routing_key = pagerduty_service_integration.cloudwatch[app].integration_key
    }
  }
}

output "production_app_count" {
  value       = length(pagerduty_service.app)
  description = <<-EOT
    Emitted unredacted so a consumer can tell "this client has no paging
    service because it has no production application" from "it should have one
    and the apply did not create it".

    Those two produce the same empty `services` map and mean opposite things. A
    sync step unable to distinguish them would either skip a real failure or
    report a phantom one for every pre-production client in the estate -- the
    B-086 shape, where zero examined and zero found read identically.
  EOT
}
