---
title: Templates
weight: 30
---

# Templates

The Bridge2AI Standards Explorer Copilot ships **one** CloudFormation template, for its one deployable backend — use it as-is, or as a reference for your own portal.

> **SPARQL variant removed.** An earlier configuration of this repo (for the Cancer Complexity Knowledge Portal) also shipped a SPARQL/knowledge-graph template and Lambda (`cckpGraphRag`). The Bridge2AI Standards Explorer has **no knowledge-graph backend** — its data lives in Synapse denormalized tables, queried via SQL — so that variant was removed rather than ported. It's recoverable from git history at commit `8684452` if a future portal needs the pattern.

### CloudFormation template

`agents/b2ai-copilot/cloudformation.sql.yaml` deploys the full stack: IAM roles, Lambda function, Bedrock Agent with action group, knowledge base attachment, and agent alias. See the usage comments at the top of the template for what to replace.

Key things to change for your own portal:
- **`Instruction`** — the system prompt, embedded inline. Replace with your agent's instructions.
- **`KnowledgeBaseId`** — a placeholder (`REPLACE_ME_B2AI_KB_ID`) until a real Bridge2AI Standards Explorer docs KB is provisioned (built from a crawl of the Bridge2AI Standards Registry docs and the LinkML `standards-schemas` docs — see [Deployment](/docs/start-here/deployment/)). Replace with your own or remove the block entirely.
- **`LambdaS3Bucket`** — a placeholder (`REPLACE_ME_B2AI_S3_BUCKET`) until an S3 bucket for the Lambda deployment package is provisioned.
- **`SynapseAuthToken`** — optional. Every table in `TABLES` is an open-access `TableEntity`, queryable anonymously, so this can be left blank; only set it if a future table requires authentication.
- **`FoundationModelId`** — defaults to a Claude Sonnet cross-region inference profile; change as needed.

Deploy with:

```bash
aws cloudformation deploy \
  --template-file agents/b2ai-copilot/cloudformation.sql.yaml \
  --stack-name b2ai-copilot-sql-dev \
  --parameter-overrides \
      AgentName=b2ai-copilot-sql-dev \
      LambdaS3Bucket=my-bucket \
      LambdaS3Key=lambda/b2aiSqlRag-dev.zip \
  --capabilities CAPABILITY_NAMED_IAM
```

Or use the Makefile targets, which wrap the same commands for a local deploy (see [Deployment](/docs/start-here/deployment/#local-deploys-with-make)):

```bash
make deploy-sql-dev      # dev Lambda + stack
make deploy-sql-prod     # prod Lambda + stack (asks for confirmation)
```

The stacks are named `b2ai-copilot-sql-dev` and `b2ai-copilot-sql-prod`.

### Lambda function

`agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py` exposes seven operations (see its `openapi.yaml` for the full action-group interface):

| Function | Purpose |
|---|---|
| `sqlQuery` | Run SQL against one named table (`standards`, `datasets`, `organizations`, `topics`, `substrates`, `manifest`, `d4d`), or a synId belonging to one of those tables |
| `getColumns` | List a table's exact deployed column names |
| `countByType` | Row counts across all 7 tables |
| `buildPortalUrl` | Build a portal path for a Standard/Organization/DataTopic Detail Page, or the Standards search tab (with optional `searchTerm`/`facets`) |
| `listD4Ds` | List the 4 Bridge2AI Grand Challenge orgs that have a D4D (Datasheet for Dataset) |
| `getD4D` | Get a Grand Challenge's D4D outline, or one section's text (paged) |
| `searchD4D` | Keyword search across one or all D4Ds |

#### The table allowlist and version pinning

`TABLES` maps each alias to a **pinned** synId (e.g. `"standards": "syn65676531.99"`), matching the exact versions the live portal queries (`Sage-Bionetworks/synapse-web-monorepo`'s `apps/portals/b2ai.standards/src/config/resources.ts`), so the copilot's answers match what a Detail Page currently shows. `sqlQuery`/`getColumns` accept only an alias or a bare synId that belongs to `TABLES`, always resolved to its pinned version — a query naming any other table or an unpinned version is rejected. This closed a real security hole found during the retarget (raw-synId passthrough; see `benchmark/redteam/redteam_config.json`'s `query-injection` item).

Run `scripts/check_table_pins.py` to diff `TABLES` against the live `resources.ts` on the monorepo's `main` and re-pin by hand if the portal has bumped a version:

```bash
python3 scripts/check_table_pins.py
```

To reuse this Lambda for your own portal: point `SYNAPSE_AUTH_TOKEN` at a valid Synapse Personal Access Token (if any of your tables need it) and update `TABLES` to your own table synIds (pinned or not, depending on whether your source data is versioned the same way). `openapi.yaml` defines the action group interface and can be used as-is if your operation names match, or adapted otherwise.
