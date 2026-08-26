---
name: retarget-portal-chatbot
description: Adapt this chatbot repo template — currently configured for the Cancer Complexity Knowledge Portal (CCKP), itself forked from nf-osi/portal-chatbot's NF Portal Copilot — for a completely different portal. Renames the agent, swaps in the new portal's resource entities and backend, re-crawls its docs, regenerates the benchmark datasets, and rewrites docs/README/CHANGELOG. Use when the user wants to fork, retarget, or reuse this repo template for a new/different portal, stand up a new portal's copilot from this codebase, or asks "how do I make this work for <other portal> instead of CCKP."
---

You retarget this entire repo — agent config, docs KB, benchmarks, docs site, top-level README/CHANGELOG — from the CCKP to a different portal, the same way this repo itself was retargeted from the NF Data Portal to the CCKP (commit `72ecf6f`, "Re-target repo from NF Portal to CCKP focus"). That commit touched 59 files and is the concrete precedent for what "done" looks like — read it (`git show 72ecf6f`) before starting if you want a full worked example, though don't assume this repo's *current* state matches that commit exactly: several things (a second docs-KB source, dataset/file discovery, agent registration) were added incrementally afterward, which is itself the lesson — retargeting isn't a single commit, it's a sequence of scoped changes you can pick up over multiple sessions.

**This is a large, multi-phase effort — do not attempt it end-to-end unattended.** Scope each phase with the user before starting it (which backend they actually have, whether they want a full retarget or just specific pieces), and treat live actions (crawling an external site, running paid LLM generation, deploying anything) as checkpoints to confirm, not steps to blaze through.

## Before starting: gather what you need from the user

You cannot retarget this repo from guesses. Get, at minimum:

1. **Portal name and a short slug** for it (e.g. "Cancer Complexity Knowledge Portal" / `cckp`) — used everywhere: directory names, Lambda function names, agent names, doc titles.
2. **Docs/help site URL(s)** to crawl for the KB — there may be more than one source, as CCKP has both a help site and a separate data-model reference site (`benchmark/general-help/cckpdocs_spider.py` + `mc2datamodelsdocs_spider.py`).
3. **The portal's resource entity model** — CCKP's is Dataset/Publication/Tool/Grant/EducationalResource, each a Synapse View table. A different portal will have different entity types (and may not use Synapse at all) — get the real list and, if available, the real schema/field reference docs for each.
4. **What resource backend actually exists**: a SQL-queryable table store (like CCKP's Synapse Views), a knowledge graph/SPARQL endpoint, both, or neither yet. This determines which CloudFormation template(s)/Lambda(s) are relevant — don't build both if only one exists.
5. **Deployment platform** — this repo assumes AWS Bedrock Agents + Synapse-hosted registration. If the new portal uses different infrastructure, the CloudFormation/Lambda layer needs more than a rename; say so explicitly rather than forcing the fit.

If any of these is unknown, ask (`AskUserQuestion` or plain conversation) before generating content — a plausible-sounding but wrong entity model or fabricated docs URL is worse than pausing to ask.

## Phase 1: Agent scaffold and backend

Reference `docs/content/docs/start-here/templates.md` — it's this repo's own living doc for reusing the CloudFormation/Lambda layer and is already written to be portal-agnostic; read and follow it rather than duplicating its instructions here. In short:

- Rename `agents/cckp-copilot/` → `agents/<slug>-copilot/` (directory, and every internal reference to `cckp-copilot` in CI/CD, README, docs).
- Rename the Lambda dirs/functions: `lambda/cckpSqlRag` → `lambda/<slug>SqlRag`, `lambda/cckpGraphRag` → `lambda/<slug>GraphRag` (only build the variant(s) matching the backend(s) that actually exist per the intake above).
- In each `cloudformation.*.yaml`: rewrite the `Instruction` parameter (the system prompt) for the new portal, replace the `KnowledgeBaseId` placeholder with the new portal's (or leave it clearly marked as a placeholder — never invent a real-looking ID), and update `SynapseAuthToken`/`SparqlEndpoint` and `TABLES`/schema references to the new entity model.
- In the SQL Lambda's `lambda_function.py`: update the `TABLES` alias map from CCKP's 5 entity types to the new portal's real ones, and check whether the dataset/file-discovery operations (`getDatasetFiles`, `getFileDetails`, `checkRestriction`) even apply — those were added specifically for Synapse-backed portals and may not transfer as-is to a different platform.
- Update each Lambda's `openapi.yaml` action-group interface and test fixtures (`tests/*.json`, `test_lambda_function.py`) to match.
- Update `.github/workflows/deploy-copilot-*.yml` for the renamed paths.

## Phase 2: Docs KB crawl

- Write a new Scrapy spider per docs source, following `benchmark/general-help/cckpdocs_spider.py` (and `mc2datamodelsdocs_spider.py` if there's a second source) as the template — same `output_markdown/`-with-source-prefix convention so multiple sources coexist without collisions.
- Confirm the target site's `robots.txt` / terms permit crawling before running it.
- Run the crawl and verify `output_markdown/*.md` is populated — this is the grounding source for everything in Phase 3.

## Phase 3: Benchmark datasets

Don't hand-roll these — this repo already has two skills purpose-built for exactly this, and they're already portal-agnostic in design (they read whatever docs/schema is actually present rather than hardcoding CCKP facts):

- **`generate-help-qa-dataset`** — generates the general-help QA dataset from whatever's in `output_markdown/`. Works as-is once Phase 2's crawl is done; no changes needed unless the new portal's directory layout differs from `benchmark/general-help/`.
- **`qa-to-kb-routing-dataset`** — converts that QA dataset into KB-routing sessions. Also works as-is, **but check `benchmark/kb-routing/kb_routing_schema.json`'s current source-label enum before assuming `DOCS`/`RAG` are still the right labels** — CCKP itself renamed `GRAPH`→`RAG` mid-project because its actual backend is SQL, not a graph; the new portal's backend shape may call for different labels again.

For `benchmark/redteam/`: this one is lighter-touch — `redteam_config.json`'s vulnerability taxonomy (off-topic repurposing, prompt injection, scope escalation, etc.) is largely portal-agnostic, so mostly find-and-replace the portal name and entity names in each `goal`/`criteria` field rather than regenerating from scratch. Do re-read each entry afterward — a vulnerability framed around a capability the new portal's agent doesn't have (e.g. controlled-access data, a specific redirect action) needs rewording, not just renaming.

For all three: never fabricate facts, field names, or policy details. Every claim needs to trace back to the new portal's actual crawled docs or real schema — the same rule the two dataset-generation skills already enforce.

## Phase 4: Docs site, README, CHANGELOG

- `docs/content/docs/**` — rewrite portal-specific content (start-here guides, benchmarking-and-evaluation pages, workflow diagrams) for the new portal; `docs/hugo.toml` branding (title, base URL).
- Root `README.md` — description, links to `agents/README.md`, PHD benchmarking section, acknowledgements (credit this repo's lineage: CCKP chatbot → NF Portal Copilot, if forking from here rather than from nf-osi directly).
- `agents/README.md` — Copilot Stacks description, Synapse Registrations table (reset to empty with a note, like this repo did — **never fabricate placeholder agent IDs**), Copilot Capabilities list, Open items.
- `agents/CHANGELOG.md` — start a fresh `## <new-agent-name>` section; don't carry over CCKP's changelog history into the new portal's entry.
- Sweep for leftover `cckp`/`CCKP`/`nf-osi` references with `grep -ril "cckp" .` (excluding `.git`) once the above is done, and confirm each hit is either intentionally-preserved lineage credit or something that still needs changing.

## Phase 5: Validate before handing off

- Run each Lambda's test suite (`pytest` in `lambda/*/tests/`) after updating `TABLES`/entity references.
- Re-run the schema validation steps documented in `qa-to-kb-routing-dataset` and `generate-help-qa-dataset` on the newly generated datasets.
- Confirm every unresolved piece of infrastructure (KB ID, agent ID, SPARQL endpoint, OIDC role) is left as a **clearly marked placeholder** — this repo's convention is literal strings like `REPLACE_ME_CCKP_KB_ID` and explicit "not yet deployed" notes in READMEs — never a plausible-looking fake value that could be mistaken for real.
- Summarize what's done vs. still open (mirroring `agents/README.md`'s "Open items" pattern) and let the user review before committing or deploying anything.

## Reference files

- `docs/content/docs/start-here/templates.md` — authoritative, already-portal-agnostic guide to the CloudFormation/Lambda layer (Phase 1)
- `git show 72ecf6f` — the original full NF→CCKP retarget commit; concrete precedent for scope and file list
- `.claude/skills/generate-help-qa-dataset/` — general-help QA dataset generation (Phase 3)
- `.claude/skills/qa-to-kb-routing-dataset/` — KB-routing dataset generation (Phase 3)
- `benchmark/general-help/cckpdocs_spider.py`, `mc2datamodelsdocs_spider.py` — spider templates (Phase 2)
- `agents/README.md`, `agents/CHANGELOG.md` — registration/changelog conventions to mirror (Phase 4)
