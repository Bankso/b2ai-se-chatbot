# Changelog

## cckp-copilot

### Unreleased

- Forked from `nf-osi/portal-chatbot`'s NF Portal Copilot and re-targeted at the Cancer Complexity Knowledge Portal (CCKP).
- Two backend variants: `cloudformation.sql.yaml` (SQL over Synapse View tables, deployable today) and `cloudformation.sparql.yaml` (SPARQL over a CCKP knowledge graph, pending a hosted endpoint).
- Instructions, redirect targets, and example queries rewritten for CCKP's Dataset/Publication/Tool/Grant/EducationalResource entities.
- Docs KB expanded to a second source: MC2 Center data model docs (mc2-center.github.io/data-models) alongside the CCKP help site, with a second crawl spider and instruction updates describing the two-source KB.
- SQL Lambda (`cckpSqlRag`) extended with dataset & file discovery (`getDatasetFiles`, `getFileDetails`, `checkRestriction`), `limit`/`nextPageToken` pagination, and more tolerant response parsing; per-id restriction info is now embedded directly in dataset/file responses so the agent can check public-accessibility before implying a resource is downloadable.
- Synapse registrations recorded for both SQL-variant stacks — `Cephy-sql-alpha-dev` (#336) and `Cephy-sql-alpha` (#335) — with manual AWS CLI deploy steps documented in `agents/README.md`. Still not deployed: no `AWS_OIDC_ROLE_ARN` role provisioned yet, so the `deploy-copilot-sql.yml`/`deploy-copilot-sparql.yml` CI/CD workflows remain inactive.
- SPARQL variant (`cloudformation.sparql.yaml`) is no longer speculative — rewritten against SageBrain, a real hosted async SPARQL/Neptune endpoint shared across multiple Sage-affiliated portals. `cckpGraphRag` rewritten for the actual wire protocol (JSON submit → poll job_id → parsed `{headers, rows, count}`, not the synchronous TSV form inherited unchanged from NF-OSI), with automatic named-graph resolution/scoping (SageBrain is append-only; every query is scoped to the latest resolved `urn:sagebrain:cckp:{date}` snapshot, not the shared default graph) and a code-level default `LIMIT` enforcement mirroring `cckpSqlRag`'s own `_clamp_limit()` pattern. New hybrid two-Lambda/two-action-group shape: a second, narrow `UrlBuilderFunction` (running the SQL Lambda's own code) exposes only `buildExploreUrl`, since a knowledge graph has no notion of the portal's filtered-Explore-page URL scheme. Reuses the SQL variant's existing `SYNAPSE_AUTH_TOKEN` secret rather than provisioning a new one — SageBrain's authorizer only requires Synapse team membership, not any particular PAT scope.
