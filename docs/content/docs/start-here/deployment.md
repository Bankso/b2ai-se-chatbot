---
title: Deployment
weight: 40
---

# Deployment

## Two stacks: dev and prod

We run the SQL backend template as two stacks: `b2ai-copilot-sql-dev` and `b2ai-copilot-sql-prod`. This gives you:

- A safe place to test instruction changes, model swaps, and Lambda updates before they reach users
- Isolated IAM roles, Lambda functions, and agent IDs per environment
- Separate Lambda packages so a bad dev deploy can't affect prod
- Dev uses the `TSTALIASID` test alias which always points to DRAFT — so you're always testing your latest changes

The workflow is: make changes on a branch → manually trigger the `deploy-copilot-sql` workflow to push to dev → test → merge to main → prod updates automatically.

> **No SPARQL backend.** The Bridge2AI Standards Explorer has no knowledge-graph endpoint — its data is served entirely from Synapse denormalized tables, queried via SQL — so there is only one backend variant, one template, and one CI workflow. See [Templates](/docs/start-here/templates/) for why the earlier CCKP-era SPARQL template was removed.

## CI/CD setup

`.github/workflows/deploy-copilot-sql.yml` handles deployments. It's smart about what changed:

- Lambda code only (`agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py`) → uploads zip and calls `update-function-code` directly (no stack update needed)
- Template/instructions/schema (`agents/b2ai-copilot/cloudformation.sql.yaml`) → runs `cloudformation deploy`

**CI/CD is optional to get started** — you can deploy manually with `aws cloudformation deploy` first and set this up later.

To set this up for your repo, you need a repo-specific IAM role for GitHub OIDC. **Ask an admin to create this**; currently, it can't be self-service because it requires IAM permissions. The role should:

- Trust `token.actions.githubusercontent.com` scoped to your repo and branch
- Have least-privilege permissions: S3 write on your Lambda bucket prefix, Lambda update on your function names, CloudFormation update on your two stack names, IAM manage on your copilot role names, Bedrock agent operations

No such role exists yet for this repo. Once created, store the role ARN as `AWS_OIDC_ROLE_ARN` in this repo's secrets.

The workflow also needs an S3 bucket for the Lambda deployment package — `deploy-copilot-sql.yml` and `Makefile` both default to the placeholder `REPLACE_ME_B2AI_S3_BUCKET`, which the user will replace with a real bucket name once one is provisioned.

## Local deploys with `make`

`Makefile` mirrors `deploy-copilot-sql.yml` for deploying from a developer machine (you need AWS credentials already configured locally — `aws configure` or `aws sso login`):

```bash
make check-aws              # confirm local AWS credentials/identity

make deploy-sql-dev         # update dev Lambda code + deploy the dev stack
make deploy-sql-dev-lambda  # update dev Lambda code only
make deploy-sql-dev-stack   # deploy the dev CloudFormation stack only

make deploy-sql-prod        # same, against PRODUCTION (asks for confirmation)
make deploy-sql-prod-lambda
make deploy-sql-prod-stack
```

Optional overrides: `AWS_PROFILE`, `AWS_REGION`, `FOUNDATION_MODEL_ID`, `S3_BUCKET`, `SQL_STACK_NAME_DEV`/`SQL_STACK_NAME_PROD`, `SQL_AGENT_NAME_DEV`/`SQL_AGENT_NAME_PROD`, `SQL_LAMBDA_FN_DEV`/`SQL_LAMBDA_FN_PROD`. Run `make help` for the full target list.

## Knowledge base

KBs are created and managed separately from the agent template — they have their own vector store, embedding model, data sources, and sync schedule. Create yours via the console or CLI, then pass the ID as the `KnowledgeBaseId` template parameter.

The Bridge2AI Standards Explorer Copilot's docs KB should be built from a crawl of both the [Bridge2AI Standards Registry docs](https://bridge2ai.github.io/b2ai-standards-registry/) and the [LinkML `standards-schemas` docs](https://bridge2ai.github.io/standards-schemas/) — see `benchmark/general-help/README.md` for the two spiders (`b2ai_registry_docs_spider.py`, `b2ai_schemas_docs_spider.py`) that crawl each source. No such KB has been built yet. The template ships with a `REPLACE_ME_B2AI_KB_ID` placeholder until one exists.

## Agent registration with Synapse

Once deployed, register the prod agent with Synapse so it appears in the chat interface. You'll need the agent ID and alias ID from the CloudFormation stack outputs. See [Registering Our New Agent](https://sagebionetworks.jira.com/wiki/spaces/PLFM/pages/3711303683/Adding+Custom+Agents+to+Synapse#Registering-Our-New-Agent) for a walkthrough and the [Synapse REST API](https://rest-docs.synapse.org/rest/PUT/agent/registration.html) for the registration endpoint.

Only register prod agents — dev agents are tested internally.

The b2ai.standards portal already has an existing registered agent (`agentRegistrationId: '236'`, "Bridge2AI Standards Portal Assistant") wired into `synapseChatConfig.ts` in the portal's frontend. The new prod agent is intended to **replace** that registration under the name **Bridge2AI Standards Explorer Copilot** — the `synapseChatConfig.ts` cutover is a separate change the user makes after this stack is deployed and registered; it isn't part of this repo.
