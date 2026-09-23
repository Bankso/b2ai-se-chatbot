# Agent Registrations

This directory contains CCKP (Cancer Complexity Knowledge Portal) agent configurations.

## Copilot Stacks

The Copilot has two backend variants, deployed from separate CloudFormation templates under `cckp-copilot/`:

- **`cloudformation.sql.yaml`** — queries the CCKP's curated Synapse View tables (Dataset, Publication, Tool, Grant, EducationalResource) directly via SQL. No new infrastructure required; **recommended today**.
- **`cloudformation.sparql.yaml`** — a hybrid two-Lambda backend: a graph-RAG Lambda queries SageBrain (a hosted, async SPARQL/Neptune endpoint) for live data, and a second, narrow Lambda (reusing the SQL variant's own code) exposes only `buildExploreUrl` so redirect links can still be built. SageBrain is a shared triple store across multiple Sage-affiliated portals (NF-OSI, ALS Knowledge Portal, and others) and is append-only — every historical publication of every portal stays loaded side by side — so every query is type-anchored to a `cckp:` class and automatically scoped to the latest resolved CCKP snapshot.

Each template deploys two stacks from the same file:

- **`cckp-copilot-{sql,sparql}-prod`** — Production. Stable version that portal users interact with.
- **`cckp-copilot-{sql,sparql}-dev`** — Development/staging. Used to test instruction changes, model swaps, and Lambda updates before promoting to prod.

No agent has been deployed yet, so there are no real Agent IDs to record here — fill in the table below once a stack is first deployed.

## Synapse Registrations

| Agent | Registration | Registered by | Notes |
|---|---|---|---|
| Cephy-sql-alpha-dev | 336 | @Bankso | Dev/staging agent for the CCKP |
| Cephy-sql-alpha | 335 | @Bankso | Prod agent for the CCKP |

## Copilot Capabilities

- **Help Docs QA**: Answers process, policy, and how-to questions from the CCKP documentation (help.cancercomplexity.synapse.org) and data-model reference questions from the MC2 Center data model docs (mc2-center.github.io/data-models)
- **Resource Search** (SQL variant): SQL queries against the CCKP's Dataset, Publication, Tool, Grant, and EducationalResource View tables
- **Dataset & File Discovery** (SQL variant): lists a dataset's actual file contents, surfaces real file metadata (name/size/type), and checks public-accessibility before implying a resource is downloadable
- **Knowledge Graph Integration** (SPARQL variant): SPARQL queries against SageBrain, a hosted, shared knowledge graph — type-anchored and named-graph-scoped to CCKP's own latest snapshot
- **Portal Navigation**: Redirects users to filtered Explore pages (datasets, publications, tools, grants, educational resources)
- **Guided Prompts**: Interactive follow-up suggestions

See [CHANGELOG](CHANGELOG.md) for release history.

## CI/CD (Not active)

Changes under `agents/cckp-copilot/` trigger deploy workflows in `.github/workflows/`:

- `deploy-copilot-sql.yml` — triggered by changes to `cloudformation.sql.yaml` or `lambda/cckpSqlRag/**`
- `deploy-copilot-sparql.yml` — triggered by changes to `cloudformation.sparql.yaml`, `lambda/cckpGraphRag/**`, or `lambda/cckpSqlRag/lambda_function.py` (the last one keeps `UrlBuilderFunction` in sync with the SQL Lambda code it reuses, since it wouldn't otherwise be watched by this workflow's own paths)

Each supports:

- **Manual dispatch** (`workflow_dispatch`) — deploys to the dev stack for testing. Trigger from any branch via the Actions UI or `gh workflow run deploy-copilot-sql.yml --ref my-branch`.
- **Merge to main** — automatically deploys to the prod stack.

The workflow detects what changed and only runs the needed steps:

- Lambda code only → uploads zip to S3 and updates the function in-place (no stack update)
- Template/instructions/schema → runs `cloudformation deploy` on the stack

AWS credentials use GitHub OIDC via an IAM role, stored as the `AWS_OIDC_ROLE_ARN` repo secret. **No such role has been provisioned yet** — an AWS admin needs to create one scoped to this repo (e.g. named `GitHubActionsCCKPChatbot`, mirroring the NF Portal Copilot's role) before these workflows will succeed. The SPARQL variant's earlier blocker (no hosted endpoint) is resolved — it now has a real default `SparqlEndpoint` — but the `AWS_OIDC_ROLE_ARN` gap applies equally to both variants, so CI/CD stays inactive for both until that role exists.

## Manual deployment
Stacks and agents can be deployed from CLI, using the cloudformation templates and commands from GitHub workflows. 

Note: This requires access to the ADMIN role 

### Add new or updated Lambda (Dev SQL RAG example)
```
cd cckp-chatbot/agents/cckp-copilot/lambda/cckpSqlRag

mkdir tmp

zip tmp/cckpSqlRag.zip lambda_function.py

aws s3 cp tmp/cckpSqlRag.zip "s3://cckp-chatbot/lambda/cckpSqlRag.zip"

aws lambda update-function-code \
    --function-name Cephy-sql-alpha-dev-sqlrag \
    --s3-bucket cckp-chatbot \
    --s3-key lambda/cckpSqlRag.zip
```

### Create or deploy stack and agent (Dev SQL RAG example)
```
aws cloudformation deploy \
	--template-file cloudformation.sql.yaml \
	--stack-name obanks-cckp-search-agent-8-21-2026-dev \
	--capabilities CAPABILITY_NAMED_IAM
```

## Setup

To learn more about the Synapse Custom Agent framework, refer to [this internal Confluence doc](https://sagebionetworks.jira.com/wiki/spaces/PLFM/pages/3711303683/Adding+Custom+Agents+to+Synapse).

## Open items

- Provision the `GitHubActionsCCKPChatbot` IAM OIDC role and `AWS_OIDC_ROLE_ARN` repo secret.
- Confirm the Synapse identity behind the existing `SYNAPSE_AUTH_TOKEN` secret is a member of Sage Brain Team (Team:3605470) — SageBrain's authorizer requires team membership specifically, separate from anything already true for Synapse API access via the SQL variant. If that token is ever swapped for a dedicated, `view`-only PAT instead of the shared one, only the secret value changes — no template/workflow change needed.
- Kg-pipeline schema gap tracked in a separate repo: `getShape` only returns real SHACL constraint info for `Dataset`/`Grant` today — see `../data-models/plans/cckp_copilot_sparql_graph_followups.md` for the upstream fix (adding `PublicationShape`/`ToolShape`/`EducationalResourceShape` to `cckp_portal.shacl.ttl`).

### Troubleshooting

- **SPARQL/agentic endpoint connection errors**: the SageBrain endpoint URLs are CloudFormation outputs of SageBrain's own stacks and can change if those stacks are recreated. Re-resolve before assuming the Lambda code itself is broken:
  ```bash
  aws --profile sagebrain-prod cloudformation describe-stacks \
    --stack-name app-prod-neptune-api \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
  ```
