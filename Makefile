# AltDigital Managed Platform
#
# Thin dispatcher to scripts/. All logic lives in the scripts so the repo is
# usable on the Windows dev box (Git Bash, no make) and in CI (make) without
# duplicating anything.

SHELL := /usr/bin/env bash

.PHONY: help setup lint guard validate validate-aws validate-policies bootstrap org-structure commercial-access

help:
	@echo "setup           install cfn-lint and cfn-guard"
	@echo "lint            cfn-lint all templates"
	@echo "guard           cfn-guard all templates against policies/guard"
	@echo "validate-aws    validate all templates against the CloudFormation API"
	@echo "validate-policies  check SCPs/RCPs with IAM Access Analyzer"
	@echo "validate        validate-aws + validate-policies + lint + guard"
	@echo "bootstrap       bootstrap the management account (add DRY=1 for dry run)"
	@echo "org-structure   deploy the OU skeleton (add DRY=1 for dry run)"
	@echo "commercial-access  deploy CommercialReadOnly + reconcile membership"

DRYFLAG := $(if $(DRY),--dry-run,)

setup:
	@scripts/setup-tooling.sh

lint:
	@scripts/lint.sh

guard:
	@scripts/guard.sh

validate-aws:
	@scripts/validate.sh

validate-policies:
	@scripts/validate-policies.sh

validate: validate-aws validate-policies lint guard

bootstrap:
	@scripts/bootstrap-management-account.sh $(DRYFLAG)

org-structure:
	@scripts/deploy-org-structure.sh $(DRYFLAG)

commercial-access:
	@scripts/deploy-commercial-access.sh $(DRYFLAG)
