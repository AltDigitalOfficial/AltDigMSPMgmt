terraform {
  required_version = ">= 1.5"

  required_providers {
    pagerduty = {
      source  = "PagerDuty/pagerduty"
      version = "~> 3.0"
    }
  }

  # State holds integration keys in cleartext. That is not a Terraform defect —
  # a routing key IS the credential, and any tool that can create one can read
  # it back. The consequences are what matter:
  #
  #   - terraform.tfstate must never be committed. Enforced in .gitignore and
  #     by the pre-commit hook, not by anyone remembering.
  #   - local state means one operator at a time and no locking.
  #
  # A remote backend belongs in the Log Archive account alongside the other
  # evidence, with the same encryption and versioning. Not configured here
  # because the bucket does not exist yet and pointing at a missing backend
  # fails init rather than degrading. Tracked as B-016.
}

provider "pagerduty" {
  # Read from the PAGERDUTY_TOKEN environment variable. Deliberately NOT a
  # Terraform variable: a variable ends up in .tfvars, in shell history, or in
  # a CI log the first time someone debugs a plan.
  #
  # See open item B10 — this should be a service account token, not a personal
  # one. A personal token ties platform automation to one person's employment
  # and inherits their permissions rather than the automation's.
}
