# Local equivalents of .github/workflows/deploy-copilot-sql.yml and
# deploy-copilot-sparql.yml, so the CCKP Copilot stacks can be deployed from
# a developer machine instead of only through CI.
#
# Unlike CI, there's no OIDC role-assumption step here — you need AWS
# credentials already configured locally (`aws configure` or `aws sso
# login`) with permission to update the relevant Lambda functions and
# CloudFormation stacks. Run `make check-aws` first to confirm which
# identity/account you're about to act as.
#
# The workflows use a git-diff check against the previous commit to decide
# whether to update the Lambda code, the CloudFormation stack, or both —
# that heuristic doesn't translate well to an ad hoc local run, so this
# Makefile instead exposes `-lambda`, `-stack`, and a combined target for
# each variant/environment; pick whichever matches what you actually
# changed.
#
# Required env vars (not committed — export these yourself; same names as
# the GitHub repo secrets):
#   CCKP_SPARQL_ENDPOINT   — for *-sparql-*-stack deploys (not deployable
#                            yet — no hosted CCKP SPARQL endpoint exists;
#                            see cloudformation.sparql.yaml's usage comments)
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
#   FOUNDATION_MODEL_ID    (default: anthropic.claude-sonnet-5)
#   S3_BUCKET              (default: cckp-chatbot)
#
# `make sparql-test` is unrelated to deployment — a standalone connectivity/
# smoke test against the live SageBrain SPARQL endpoint (see
# plans/implement-sparql-backend.md). It needs a Synapse PAT: checks
# ~/.sagebrain-pat first, then ~/.synapseConfig's `authtoken` field, or set
# SPARQL_PAT yourself. Override QUERY to run something other than the
# default graph-enumeration query.

.DEFAULT_GOAL := help

AWS_REGION ?= us-east-1
FOUNDATION_MODEL_ID ?= anthropic.claude-sonnet-4-6
S3_BUCKET ?= cckp-chatbot

AWS := aws --region $(AWS_REGION) $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),)

SQL_TEMPLATE := agents/cckp-copilot/cloudformation.sql.yaml
SQL_LAMBDA_DIR := agents/cckp-copilot/lambda/cckpSqlRag

# Current live resource names (confirmed 2026-08-21 against the actual
# deployed stacks — the GitHub workflow's own STACK_NAME/AGENT_NAME values
# are stale and don't match what's actually running).
SQL_STACK_NAME_DEV ?= obanks-cckp-search-agent-8-21-2026-dev
SQL_STACK_NAME_PROD ?= obanks-cckp-search-agent-8-21-2026
SQL_AGENT_NAME_DEV ?= Cephy-sql-alpha-dev
SQL_AGENT_NAME_PROD ?= Cephy-sql-alpha-prod
SQL_LAMBDA_FN_DEV ?= Cephy-sql-alpha-dev-sqlrag
SQL_LAMBDA_FN_PROD ?= Cephy-sql-alpha-prod-sqlrag

SPARQL_TEMPLATE := agents/cckp-copilot/cloudformation.sparql.yaml
SPARQL_LAMBDA_DIR := agents/cckp-copilot/lambda/cckpGraphRag

# Mirrors the SQL variant's Cephy-alpha naming convention. No SPARQL stack
# is deployed anywhere yet, so these are the intended names, not confirmed
# live ones — adjust once a real stack exists.
SPARQL_STACK_NAME_DEV ?= Cephy-sparql-alpha-dev
SPARQL_STACK_NAME_PROD ?= Cephy-sparql-alpha-prod
SPARQL_AGENT_NAME_DEV ?= Cephy-sparql-alpha-dev
SPARQL_AGENT_NAME_PROD ?= Cephy-sparql-alpha-prod
SPARQL_LAMBDA_FN_DEV ?= Cephy-sparql-alpha-dev-graphrag
SPARQL_LAMBDA_FN_PROD ?= Cephy-sparql-alpha-prod-graphrag

CONFIRM_PROD = @echo "About to deploy to PRODUCTION ($(1)). Type 'yes' to continue:" && read -r ans && [ "$$ans" = "yes" ] || (echo "Aborted."; exit 1)

# ---------------------------------------------------------------------------
# SPARQL endpoint smoke test (not a deploy target — see plans/implement-sparql-backend.md)
# ---------------------------------------------------------------------------

SPARQL_TEST_ENDPOINT ?= https://vyar2xyj0k.execute-api.us-east-1.amazonaws.com/prod/query

# The PAT is deliberately NOT a Make variable: `make -n` (dry-run) prints
# fully-substituted recipe text regardless of `@` silencing, so any secret
# assigned via `?=`/`$(shell ...)` here would leak into terminal
# scrollback/history the moment anyone dry-runs this target. Resolution
# (env SPARQL_PAT, then ~/.sagebrain-pat, then ~/.synapseConfig) happens
# entirely inside scripts/sparql_test_query.py instead — see its
# resolve_pat(). Export SPARQL_PAT yourself before calling `make
# sparql-test` if you want to override the file-based lookup.
#
# QUERY doesn't need its own PREFIX declarations for cckp:/rdfs:/etc. — the
# script automatically prepends the real deployed Lambda's own
# DEFAULT_PREFIXES (agents/cckp-copilot/lambda/cckpGraphRag/lambda_function.py),
# the same prefixes every real sparqlQuery/getSchema/etc. call gets. Set
# NO_DEFAULT_PREFIXES=1 to submit QUERY completely raw instead.

# Default query resolves the open unknown in plans/implement-sparql-backend.md
# finding #4: which named graphs actually exist right now, so the real CCKP
# portal token in the graph URI can be confirmed. Override QUERY for anything
# else, e.g.:
#   make sparql-test QUERY='SELECT (COUNT(*) AS ?n) WHERE { GRAPH <urn:sagebrain:cckp:2026-09-01> { ?s ?p ?o } }'
QUERY ?= SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY DESC(?g)

POLL_INTERVAL ?= 3
POLL_TIMEOUT ?= 90

.PHONY: help check-aws sparql-test \
        deploy-sql-dev deploy-sql-dev-lambda deploy-sql-dev-stack \
        deploy-sql-prod deploy-sql-prod-lambda deploy-sql-prod-stack \
        deploy-sparql-dev deploy-sparql-dev-lambda deploy-sparql-dev-stack \
        deploy-sparql-prod deploy-sparql-prod-lambda deploy-sparql-prod-stack

help:
	@echo "Local deploy targets for agents/cckp-copilot (mirrors .github/workflows/deploy-copilot-*.yml):"
	@echo ""
	@echo "  make check-aws                    confirm local AWS credentials/identity"
	@echo ""
	@echo "  make deploy-sql-dev               update dev Lambda code + deploy the dev SQL stack"
	@echo "  make deploy-sql-dev-lambda        update dev Lambda code only"
	@echo "  make deploy-sql-dev-stack         deploy the dev CloudFormation stack only"
	@echo "  make deploy-sql-prod              same, against PRODUCTION (asks for confirmation)"
	@echo "  make deploy-sql-prod-lambda"
	@echo "  make deploy-sql-prod-stack"
	@echo ""
	@echo "  make deploy-sparql-dev / -lambda / -stack   same, for the SPARQL variant"
	@echo "  make deploy-sparql-prod / -lambda / -stack  (not deployable yet — no hosted endpoint)"
	@echo ""
	@echo "  make sparql-test                  submit+poll a test SPARQL query against the live"
	@echo "                                     SageBrain endpoint (default: enumerate named graphs)"
	@echo "                                     override with QUERY=... , SPARQL_TEST_ENDPOINT=... ,"
	@echo "                                     POLL_INTERVAL=... (default 3), POLL_TIMEOUT=... (default 90)"
	@echo ""
	@echo "Set CCKP_SPARQL_ENDPOINT before a *-sparql-*-stack deploy (sql stacks need no token)."
	@echo "Optional: AWS_PROFILE, AWS_REGION, FOUNDATION_MODEL_ID, S3_BUCKET,"
	@echo "          SQL_STACK_NAME_DEV/PROD, SQL_AGENT_NAME_DEV/PROD, SQL_LAMBDA_FN_DEV/PROD,"
	@echo "          SPARQL_STACK_NAME_DEV/PROD, SPARQL_AGENT_NAME_DEV/PROD, SPARQL_LAMBDA_FN_DEV/PROD."

check-aws:
	$(AWS) sts get-caller-identity

sparql-test:
	@SPARQL_ENDPOINT="$(SPARQL_TEST_ENDPOINT)" \
	 SPARQL_QUERY="$(QUERY)" \
	 POLL_INTERVAL="$(POLL_INTERVAL)" \
	 POLL_TIMEOUT="$(POLL_TIMEOUT)" \
	 python3 scripts/sparql_test_query.py

# ---------------------------------------------------------------------------
# SQL variant
# ---------------------------------------------------------------------------

deploy-sql-dev: deploy-sql-dev-lambda deploy-sql-dev-stack

deploy-sql-dev-lambda:
	cd $(SQL_LAMBDA_DIR) && zip -q /tmp/cckpSqlRag.zip lambda_function.py
	$(AWS) s3 cp /tmp/cckpSqlRag.zip "s3://$(S3_BUCKET)/lambda/cckpSqlRag-dev.zip"
	$(AWS) lambda update-function-code \
		--function-name $(SQL_LAMBDA_FN_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/cckpSqlRag-dev.zip

deploy-sql-dev-stack:
	$(AWS) cloudformation deploy \
		--template-file $(SQL_TEMPLATE) \
		--stack-name $(SQL_STACK_NAME_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SQL_AGENT_NAME_DEV) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/cckpSqlRag-dev.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset

deploy-sql-prod: deploy-sql-prod-lambda deploy-sql-prod-stack

deploy-sql-prod-lambda:
	$(call CONFIRM_PROD,SQL Lambda)
	cd $(SQL_LAMBDA_DIR) && zip -q /tmp/cckpSqlRag.zip lambda_function.py
	$(AWS) s3 cp /tmp/cckpSqlRag.zip "s3://$(S3_BUCKET)/lambda/cckpSqlRag.zip"
	$(AWS) lambda update-function-code \
		--function-name $(SQL_LAMBDA_FN_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/cckpSqlRag.zip

deploy-sql-prod-stack:
	$(call CONFIRM_PROD,SQL stack)
	$(AWS) cloudformation deploy \
		--template-file $(SQL_TEMPLATE) \
		--stack-name $(SQL_STACK_NAME_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SQL_AGENT_NAME_PROD) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/cckpSqlRag.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset

# ---------------------------------------------------------------------------
# SPARQL variant (not deployable yet — no hosted CCKP SPARQL endpoint exists;
# targets provided for when one does, mirroring deploy-copilot-sparql.yml)
# ---------------------------------------------------------------------------

deploy-sparql-dev: deploy-sparql-dev-lambda deploy-sparql-dev-stack

deploy-sparql-dev-lambda:
	cd $(SPARQL_LAMBDA_DIR) && zip -q /tmp/cckpGraphRag.zip lambda_function.py
	$(AWS) s3 cp /tmp/cckpGraphRag.zip "s3://$(S3_BUCKET)/lambda/cckpGraphRag-dev.zip"
	$(AWS) lambda update-function-code \
		--function-name $(SPARQL_LAMBDA_FN_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/cckpGraphRag-dev.zip

deploy-sparql-dev-stack:
	@test -n "$(CCKP_SPARQL_ENDPOINT)" || (echo "CCKP_SPARQL_ENDPOINT is required" >&2; exit 1)
	$(AWS) cloudformation deploy \
		--template-file $(SPARQL_TEMPLATE) \
		--stack-name $(SPARQL_STACK_NAME_DEV) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SPARQL_AGENT_NAME_DEV) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			SparqlEndpoint=$(CCKP_SPARQL_ENDPOINT) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/cckpGraphRag-dev.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset

deploy-sparql-prod: deploy-sparql-prod-lambda deploy-sparql-prod-stack

deploy-sparql-prod-lambda:
	$(call CONFIRM_PROD,SPARQL Lambda)
	cd $(SPARQL_LAMBDA_DIR) && zip -q /tmp/cckpGraphRag.zip lambda_function.py
	$(AWS) s3 cp /tmp/cckpGraphRag.zip "s3://$(S3_BUCKET)/lambda/cckpGraphRag.zip"
	$(AWS) lambda update-function-code \
		--function-name $(SPARQL_LAMBDA_FN_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--s3-key lambda/cckpGraphRag.zip

deploy-sparql-prod-stack:
	$(call CONFIRM_PROD,SPARQL stack)
	@test -n "$(CCKP_SPARQL_ENDPOINT)" || (echo "CCKP_SPARQL_ENDPOINT is required" >&2; exit 1)
	$(AWS) cloudformation deploy \
		--template-file $(SPARQL_TEMPLATE) \
		--stack-name $(SPARQL_STACK_NAME_PROD) \
		--s3-bucket $(S3_BUCKET) \
		--parameter-overrides \
			AgentName=$(SPARQL_AGENT_NAME_PROD) \
			FoundationModelId=$(FOUNDATION_MODEL_ID) \
			SparqlEndpoint=$(CCKP_SPARQL_ENDPOINT) \
			LambdaS3Bucket=$(S3_BUCKET) \
			LambdaS3Key=lambda/cckpGraphRag.zip \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset
