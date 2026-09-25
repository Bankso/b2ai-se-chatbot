# Agent Registrations

This directory contains configuration for the **Bridge2AI Standards Explorer Copilot** (portal: https://b2ai.standards.synapse.org/).

## Copilot Stacks

The copilot has **one** backend variant — SQL only — deployed from `agents/b2ai-copilot/cloudformation.sql.yaml`. Unlike the CCKP Copilot this repo was retargeted from, there is no SPARQL/knowledge-graph variant: the Bridge2AI Standards Explorer has no knowledge-graph backend, so `cloudformation.sparql.yaml` and the `cckpGraphRag` Lambda were removed rather than ported (see `agents/CHANGELOG.md`).

The template deploys two stacks from the same file:

- **`b2ai-copilot-sql-prod`** — Production. Stable version that portal users interact with.
- **`b2ai-copilot-sql-dev`** — Development/staging. Used to test instruction changes, model swaps, and Lambda updates before promoting to prod.

No stack has been deployed yet, so there are no real Agent IDs to record here — fill in the table below once a stack is first deployed.

## Synapse Registrations

*(reset — no Bridge2AI Standards Explorer Copilot agent has been registered in Synapse yet. Fill in this table once a stack is deployed and an agent is registered; never invent a placeholder registration number or agent id.)*

| Agent | Registration | Registered by | Notes |
|---|---|---|---|

The portal currently ships a *different*, pre-existing agent registration — **236, "Bridge2AI Standards Portal Assistant"** — wired into the live frontend's `synapseChatConfig.ts`. Per the retarget plan, the new prod agent built from this repo will **replace** that registration once deployed, but that cutover is an **open item** for the user to perform after deployment (see below) — it has not happened yet, and this table does not represent it as done.

## Copilot Capabilities

- **Help Docs QA**: Answers process, policy, and how-to questions from the Bridge2AI Standards Registry docs (bridge2ai.github.io/b2ai-standards-registry) and LinkML data-model reference questions from the standards-schemas docs (bridge2ai.github.io/standards-schemas), via the Bedrock Knowledge Base.
- **Resource Search (SQL)**: `sqlQuery`, `getColumns`, and `countByType` against 7 pinned Synapse tables — `standards`, `datasets`, `organizations`, `topics`, `substrates`, `manifest`, and `d4d` (see `TABLES` in `agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py`). Every table reference is pinned to the exact synId.version the live portal itself queries, and every query is checked against a **table allowlist** applied to both the `table` argument *and* the raw SQL text — Synapse executes whatever table a query's `FROM` clause actually names regardless of the request path's entity id, so restricting the argument alone isn't sufficient (see `_check_sql_tables`, added after this was found to be a real security hole during the retarget — commit `22de2a2`).
- **D4D (Datasheets for Datasets) Exploration**: `listD4Ds`, `getD4D` (outline or a specific section, paged), and `searchD4D` (keyword search across one or all D4Ds) over the 4 Bridge2AI Grand Challenges' Datasheets-for-Datasets content (`D4D_content` table), letting a user get an overview, drill into a section, or compare across Grand Challenges conversationally instead of receiving the full 38–60 KB HTML document at once.
- **Portal Navigation (`buildPortalUrl`)**: builds working relative portal paths — Detail Pages for a standard/organization/data topic (`/Explore/{Standard,Organization,DataTopic}/…DetailsPage?id=…`), and filtered `/Search/Standards` links with a free-text `SEARCH_TERM` and/or `facets`. Facets are gzip+base64 encoded as the portal's `qw0` query param and **self-verified** (decoded back and compared) before the URL is returned. Verified live semantics: facets **AND across columns**, **OR within one column's values**, and a search term **ANDs against the combined facets** — but a multi-word `SEARCH_TERM` alone is an **OR/union of the words**, never an AND, so it can't be used to require multiple concepts at once (use one search term plus facets instead). `facets` is restricted to a 12-column allowlist (`FACET_COLUMNS`) matching the columns actually shown on the page.
- **Guided Prompts**: Interactive follow-up suggestions.

See [CHANGELOG](CHANGELOG.md) for release history.

## CI/CD

Changes under `agents/b2ai-copilot/` trigger the deploy workflow in `.github/workflows/`:

- `deploy-copilot-sql.yml` — triggered by changes to `cloudformation.sql.yaml` or `lambda/b2aiSqlRag/**`

It supports:

- **Manual dispatch** (`workflow_dispatch`) — deploys to the dev stack for testing. Trigger from any branch via the Actions UI or `gh workflow run deploy-copilot-sql.yml --ref my-branch`.
- **Merge to main** — automatically deploys to the prod stack.

The workflow detects what changed and only runs the needed steps:

- Lambda code only → uploads zip to S3 and updates the function in-place (no stack update)
- Template/instructions/schema → runs `cloudformation deploy` on the stack

AWS credentials use GitHub OIDC via an IAM role, stored as the `AWS_OIDC_ROLE_ARN` repo secret. **No such role has been provisioned yet for this repo** — an AWS admin needs to create one scoped to `b2ai-se-chatbot` before this workflow will succeed. The workflow's `S3_BUCKET` env var is currently the literal placeholder `REPLACE_ME_B2AI_S3_BUCKET` — replace it (and `LambdaS3Bucket`) with a real bucket name once one is provisioned.

## Local deploys (`Makefile`)

The repo root `Makefile` is a local equivalent of `deploy-copilot-sql.yml`, for deploying from a developer machine instead of only through CI (no OIDC assumption — use your own configured AWS credentials).

```sh
make check-aws                    # confirm local AWS credentials/identity
make deploy-sql-dev               # update dev Lambda code + deploy the dev SQL stack
make deploy-sql-dev-lambda        # update dev Lambda code only
make deploy-sql-dev-stack         # deploy the dev CloudFormation stack only
make deploy-sql-prod              # same, against PRODUCTION (asks for confirmation)
make deploy-sql-prod-lambda
make deploy-sql-prod-stack
```

Optional overrides: `AWS_PROFILE`, `AWS_REGION` (default `us-east-1`), `FOUNDATION_MODEL_ID` (default `anthropic.claude-sonnet-4-6`), `S3_BUCKET` (default `REPLACE_ME_B2AI_S3_BUCKET` — **set this before deploying**), `SQL_STACK_NAME_DEV`/`SQL_STACK_NAME_PROD`, `SQL_AGENT_NAME_DEV`/`SQL_AGENT_NAME_PROD`, `SQL_LAMBDA_FN_DEV`/`SQL_LAMBDA_FN_PROD`.

## Manual deployment

Stacks and agents can also be deployed straight from the CLI, using the CloudFormation template and the same commands the GitHub workflow and Makefile run.

Note: This requires access to the ADMIN role.

### Add new or updated Lambda (Dev SQL RAG example)
```sh
cd b2ai-se-chatbot/agents/b2ai-copilot/lambda/b2aiSqlRag

mkdir tmp

zip tmp/b2aiSqlRag.zip lambda_function.py

aws s3 cp tmp/b2aiSqlRag.zip "s3://REPLACE_ME_B2AI_S3_BUCKET/lambda/b2aiSqlRag-dev.zip"

aws lambda update-function-code \
    --function-name b2ai-copilot-sql-dev-sqlrag \
    --s3-bucket REPLACE_ME_B2AI_S3_BUCKET \
    --s3-key lambda/b2aiSqlRag-dev.zip
```

### Create or deploy stack and agent (Dev SQL RAG example)
```sh
aws cloudformation deploy \
	--template-file cloudformation.sql.yaml \
	--stack-name b2ai-copilot-sql-dev \
	--parameter-overrides \
		AgentName=b2ai-copilot-sql-dev \
		LambdaS3Bucket=REPLACE_ME_B2AI_S3_BUCKET \
		LambdaS3Key=lambda/b2aiSqlRag-dev.zip \
	--capabilities CAPABILITY_NAMED_IAM
```

`REPLACE_ME_B2AI_S3_BUCKET` above is a placeholder — swap in the real artifact bucket name once one is provisioned.

## Setup

To learn more about the Synapse Custom Agent framework, refer to [this internal Confluence doc](https://sagebionetworks.jira.com/wiki/spaces/PLFM/pages/3711303683/Adding+Custom+Agents+to+Synapse).

## Open items

- **KB ID.** `KnowledgeBaseId` in `cloudformation.sql.yaml` is still the placeholder `REPLACE_ME_B2AI_KB_ID` — no Bedrock Knowledge Base has been built yet from the crawled docs.
- **S3 artifact bucket.** `REPLACE_ME_B2AI_S3_BUCKET` (this file, the `Makefile`, and `deploy-copilot-sql.yml`) is not yet a real bucket. The user will provide a name.
- **First deploy.** No `b2ai-copilot-sql-{dev,prod}` stack has ever been deployed; the `AWS_OIDC_ROLE_ARN` repo secret also needs to be provisioned before CI can deploy.
- **Registration 236 cutover.** The live portal's `synapseChatConfig.ts` currently points at agent registration 236 ("Bridge2AI Standards Portal Assistant"). The new prod agent from this repo is intended to *replace* it, but that cutover has **not** happened — the user will make the `synapseChatConfig.ts` change themselves after this repo's agent is deployed and verified.
- **QA dataset human review.** `benchmark/general-help/help_qa_dataset_anthropic.json` (390 Claude-generated questions) has not yet been through Step 3 human review — see that benchmark's README.
- **`datasets.isPublic` meaning.** 16 of 116 rows in the `datasets` table have `isPublic = false`, and its meaning isn't documented anywhere crawled so far. Ask the Bridge2AI team before making any claim about what an `isPublic=false` dataset means.
- **Registry-specific contact.** The redteam benchmark's PII-leakage allowlisted contact is currently the program-level `admin@bridge2ai.org`; confirm whether there's a registry-specific contact that should be used instead.
- **Runbook note:** when the portal bumps its Synapse table pins, re-run `scripts/check_table_pins.py` (drift-checks `TABLES` in the Lambda against `Sage-Bionetworks/synapse-web-monorepo`'s `resources.ts` on `main`) and update `TABLES` accordingly before the Lambda's answers silently go stale.
