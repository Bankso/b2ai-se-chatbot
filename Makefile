# Local equivalent of .github/workflows/deploy-copilot-sql.yml, so the
# Bridge2AI Standards Explorer Copilot stacks can be deployed from a developer
# machine instead of only through CI.
#
# Unlike CI, there's no OIDC role-assumption step here — you need AWS
# credentials already configured locally (`aws configure` or `aws sso
# login`) with permission to update the relevant Lambda functions and
# CloudFormation stacks. Run `make check-aws` first to confirm which
# identity/account you're about to act as.
#
# The combined deploy-sql-{dev,prod} targets run the same three steps as the
# workflow: upload the package, deploy the stack (which creates the Lambda on
# a first deploy), then push the code. The -lambda and -stack targets run a
# subset when you know what changed; -lambda needs the stack to exist.
#
# SQL stack deploys don't take a Synapse token override: SynapseAuthToken
# defaults to "" in the template, and `aws cloudformation deploy` reuses
# the existing stack's current value for any parameter left out of
# --parameter-overrides on an update, so omitting it is correct once the
# stack has a token set.
#
# Optional overrides:
#   AWS_PROFILE            (default: your default AWS CLI profile)
#   AWS_REGION             (default: us-east-1, matches the workflows)
#   FOUNDATION_MODEL_ID    (default: anthropic.claude-sonnet-4-6)
#   S3_BUCKET              (default: b2ai-se-copilot)

.DEFAULT_GOAL := help

AWS_REGION ?= us-east-1
FOUNDATION_MODEL_ID ?= anthropic.claude-sonnet-4-6
S3_BUCKET ?= b2ai-se-copilot
AWS_PROFILE ?=

AWS := aws --region $(AWS_REGION) $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),)

SQL_TEMPLATE := agents/b2ai-copilot/cloudformation.sql.yaml
SQL_LAMBDA_DIR := agents/b2ai-copilot/lambda/b2aiSqlRag

# Mirror the names in .github/workflows/deploy-copilot-sql.yml. No B2AI stack
# is deployed yet, so these are the intended names, not confirmed live ones.
SQL_STACK_NAME_DEV ?= b2ai-copilot-sql-dev
SQL_STACK_NAME_PROD ?= b2ai-copilot-sql-prod
SQL_AGENT_NAME_DEV ?= b2ai-copilot-sql-dev
SQL_AGENT_NAME_PROD ?= b2ai-copilot-sql
SQL_LAMBDA_FN_DEV ?= b2ai-copilot-sql-dev-sqlrag
SQL_LAMBDA_FN_PROD ?= b2ai-copilot-sql-sqlrag

CONFIRM_PROD = @echo "About to deploy to PRODUCTION ($(1)). Type 'yes' to continue:" && read -r ans && [ "$$ans" = "yes" ] || (echo "Aborted."; exit 1)

.PHONY: help check-aws \
        deploy-sql-dev deploy-sql-dev-lambda deploy-sql-dev-stack \
        deploy-sql-dev-upload deploy-sql-dev-code \
        deploy-sql-prod deploy-sql-prod-lambda deploy-sql-prod-stack \
        deploy-sql-prod-upload deploy-sql-prod-code deploy-sql-prod-stack-unconfirmed

help:
	@echo "Local deploy targets for agents/b2ai-copilot (mirrors .github/workflows/deploy-copilot-sql.yml):"
	@echo ""
	@echo "  make check-aws                    confirm local AWS credentials/identity"
	@echo ""
	@echo "  make deploy-sql-dev               upload package, deploy the dev stack, push the code"
	@echo "                                    (use this for a first deploy -- it creates the Lambda)"
	@echo "  make deploy-sql-dev-lambda        upload + push dev Lambda code only (stack must exist)"
	@echo "  make deploy-sql-dev-stack         deploy the dev CloudFormation stack only"
	@echo "                                    (expects the package already uploaded)"
	@echo "  make deploy-sql-prod              same, against PRODUCTION (asks for confirmation)"
	@echo "  make deploy-sql-prod-lambda"
	@echo "  make deploy-sql-prod-stack"
	@echo ""
	@echo "Optional: AWS_PROFILE, AWS_REGION, FOUNDATION_MODEL_ID, S3_BUCKET,"
	@echo "          SQL_STACK_NAME_DEV/PROD, SQL_AGENT_NAME_DEV/PROD, SQL_LAMBDA_FN_DEV/PROD."

check-aws:
	$(AWS) sts get-caller-identity

# ---------------------------------------------------------------------------
# SQL backend
# ---------------------------------------------------------------------------

# A full deploy uploads the package, then deploys the stack (which creates the
# Lambda from that package on a first deploy), then pushes the code into the
# function (so a code-only change still lands when the stack is unchanged).
# The -lambda targets only work once the stack -- and so the function -- exists.

deploy-sql-dev: deploy-sql-dev-upload deploy-sql-dev-stack deploy-sql-dev-code

deploy-sql-dev-lambda: deploy-sql-dev-upload deploy-sql-dev-code

deploy-sql-dev-upload:
	cd $(SQL_LAMBDA_DIR) && zip -q /tmp/b2aiSqlRag.zip lambda_function.py d4d_data.json
	$(AWS) s3 cp /tmp/b2aiSqlRag.zip "s3://$(S3_BUCKET)/lambda/b2aiSqlRag-dev.zip"

deploy-sql-dev-code:
	$(AWS) lambda update-function-code \
		--function-name $(SQL_LAMBDA_FN_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/b2aiSqlRag-dev.zip

deploy-sql-dev-stack:
	$(AWS) cloudformation deploy \
		--template-file $(SQL_TEMPLATE) \
		--stack-name $(SQL_STACK_NAME_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SQL_AGENT_NAME_DEV) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/b2aiSqlRag-dev.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset

deploy-sql-prod:
	$(call CONFIRM_PROD,SQL Lambda + stack)
	$(MAKE) --no-print-directory deploy-sql-prod-upload deploy-sql-prod-stack-unconfirmed deploy-sql-prod-code

deploy-sql-prod-lambda:
	$(call CONFIRM_PROD,SQL Lambda)
	$(MAKE) --no-print-directory deploy-sql-prod-upload deploy-sql-prod-code

deploy-sql-prod-stack:
	$(call CONFIRM_PROD,SQL stack)
	$(MAKE) --no-print-directory deploy-sql-prod-stack-unconfirmed

deploy-sql-prod-upload:
	cd $(SQL_LAMBDA_DIR) && zip -q /tmp/b2aiSqlRag.zip lambda_function.py d4d_data.json
	$(AWS) s3 cp /tmp/b2aiSqlRag.zip "s3://$(S3_BUCKET)/lambda/b2aiSqlRag.zip"

deploy-sql-prod-code:
	$(AWS) lambda update-function-code \
		--function-name $(SQL_LAMBDA_FN_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/b2aiSqlRag.zip

deploy-sql-prod-stack-unconfirmed:
	$(AWS) cloudformation deploy \
		--template-file $(SQL_TEMPLATE) \
		--stack-name $(SQL_STACK_NAME_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SQL_AGENT_NAME_PROD) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/b2aiSqlRag.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset
