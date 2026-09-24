# Retarget: CCKP Copilot → Bridge2AI Standards Explorer Copilot

Portal: https://b2ai.standards.synapse.org/. Slug `b2ai`, stack/agent/Lambda prefix `b2ai-copilot`. The user-facing name is **Bridge2AI Standards Explorer Copilot**.
Driven by `.claude/skills/retarget-portal-chatbot/SKILL.md`. Branch `b2ai-conversion`.

## Intake (confirmed 2026-09-24)

| Item | Answer |
|---|---|
| Slug | `b2ai` |
| Docs sources | https://bridge2ai.github.io/b2ai-standards-registry/ (MkDocs, 189 sitemap URLs; github.io has no robots.txt) and https://bridge2ai.github.io/standards-schemas/ (LinkML class/slot/enum docs). The registry repo is https://github.com/bridge2ai/b2ai-standards-registry. |
| Source of truth | Synapse tables in public project `syn63096806` ("standards-data"). The portal renders mostly from the denormalized tables. |
| Backend | Synapse tables, SQL only. **No SPARQL/graph backend.** |
| Access restrictions | None. Everything is open access. |
| Deployment | Same as CCKP: AWS Bedrock Agents, Synapse registration, GitHub Actions. |
| Question bank | None yet; to be derived from the docs later. |

### Backend facts (verified live)

The denormalized tables below can be queried **anonymously**. `CurrentTableVersions` (`syn66330007`) needs authentication, and its rows are stale (last as-of date 2025-05), so it isn't a usable version source.

| Alias | Table | synId (portal pin) | Latest rows | Portal route |
|---|---|---|---|---|
| standards | DST_denormalized | syn65676531 (.99) | 1156 latest / 1029 pinned | `/Explore/Standard/DetailsPage?id=B2AI_STANDARD:N` |
| datasets | DataSet_denormalized | syn68258237 (.15) | 116 | none (by design) |
| organizations | Organization_denormalized | syn69693360 (.32) | 174 latest / 130 pinned | `/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:N` |
| topics | DataTopic_denormalized | syn75081383 (.8) | 82 | `/Explore/DataTopic/DetailsPage?id=B2AI_TOPIC:N` |
| substrates | DataSubstrate | syn63096834 (.32) | 81 | none (by design) |
| manifest | Manifest | syn72106735 (.21) | 70 | none (by design) |
| d4d *(new)* | D4D_content | syn68885644 (.13) | 5 (4 html + 1 css) | the "D4D" section of the Grand Challenge org detail pages |

Pins come from `Sage-Bionetworks/synapse-web-monorepo` `apps/portals/b2ai.standards/src/config/resources.ts` as of 2026-09-24.

Other tables in the project that the portal doesn't use through `resources.ts`: DataStandardOrTool `syn63096833`, DataSet `syn66330217`, DataTopic `syn63096835`, Organization `syn63096836`, UseCase `syn63096837`, Challenges `syn65913973`.

- **Version pinning.** DECISION: **pin** to the portal's versions so answers match what the pages show.
  - Implementation: versioned synIds in `TABLES`, plus a drift-check script that compares them against `resources.ts` on the monorepo's `main`.
  - Re-pin whenever the portal bumps its pins.
- **Search: RESOLVED 2026-09-24.** Verified via source trace, direct Synapse API replication and live browser counts.
  - **Request path.** `/Search/Standards` queries OpenSearch-backed search index `syn74909093` ("b2ai standards"). Its `definingSQL` is over `syn65676531.99` and gives 1029 docs, matching the pinned table's row count. The query goes to `POST /repo/v1/search/query/async/start` as `{multi_match:{query:<SEARCH_TERM>, fuzziness:"AUTO"}}`, with no `operator`. `standardsFtsConfig` (BOOLEAN) is dead code for this page.
  - **Multi-word `SEARCH_TERM` = OR/union.** `imaging` 107 + `genomics` 112 → `imaging genomics` 213, an exact ID-set union; the browser also shows 213. A second pair gave `FHIR` 69 + `ontology` 270 → 326. Quoted phrases and `synNNN` tokens switch to `simple_query_string`, and `+`/`-`/`AND`/`OR` aren't reliable. Don't tell users operators work.
  - **Matching** is case-insensitive, stemmed (`ontologies` ≡ `ontology` ≡ `ontolog`) and fuzzy (typos). It isn't prefix matching: `onto` returns 0. `standard` matches every doc.
  - **Facets can be deep-linked** via `qw0` = urlencode(base64(gzip(JSON diff `{"selectedFacets":[FacetColumnValuesRequest...]}`))), the same encoding as the CCKP builder, alongside plain `SEARCH_TERM`. Python-built links verified in the browser:
    - `topic=[Image]` → 67
    - `topic=[Image]` + `category=[Ontology or Vocabulary]` → 1 (**AND across columns**; SQL also gives 1)
    - `topic=[Image, Genome]` → 121 (**OR within a column**; SQL `HAS` gives 121)
    - `SEARCH_TERM=segmentation` (16) + `topic=[Image]` → 3 (**term AND facets**)
  - **Facet columns on the page:** `applicationNames`, `category`, `collections`, `dataTypes`, `hasAIApplication`, `isOpen`, `mature`, `registration`, `relevantOrgNames`, `topic`, `topicDescription`, `usedInBridge2AI`.
  - **Implication:** to require several concepts at once, use one search term plus facets (or several facets). Never rely on multiple words in the search term. Extend `buildPortalUrl` search with an optional `facets` param (qw0 plus the self-verify roundtrip).
- **Existing agent.** The portal ships `agentRegistrationId: '236'` ("Bridge2AI Standards Portal Assistant").
  - DECISION: the new prod agent **replaces** it, under the name **Bridge2AI Standards Explorer Copilot**.
  - The user will make the `synapseChatConfig.ts` change after deployment.
- **Grand Challenges and D4Ds.**
  - The 4 Grand Challenge orgs (`GC_ORG_IDS` = `B2AI_ORG:114`–`117`) get extra sections on their org detail pages (`OrganizationDetailsPage.tsx`: the `GC_ORG_IDS.includes(id)` branch, plus a `D4D` section whenever the org row's `d4d` column is set).
  - D4D (Datasheets for Datasets) content is full HTML in `D4D_content`, one row per GC org (`content_id` = org id, `content_type` = `html`, 38–60 KB each). That is **too large to return whole through a Bedrock action group** (~25 KB response limit).
  - DECISION: add a **D4D exploration sub-routine** (below).

## D4D exploration sub-routine (new scope, 2026-09-24)

Goal: a user can pick a Grand Challenge's D4D and explore it conversationally: get an overview, drill into sections, ask questions, and compare across GCs.

- **Lambda operations** (stdlib only; the deploy workflow zips just `lambda_function.py`):
  - `listD4Ds()`: the GC orgs that have a D4D (id, name, detail-page link).
  - `getD4D(orgId, section?, offset?)`
    - Without `section`: an outline, meaning title and section/subsection headings with ids and sizes.
    - With `section`: that section's text converted from HTML to readable markdown/text, paged with `offset` under a byte budget that stays safely below the action-group response limit.
  - `searchD4D(query, orgId?)`: case-insensitive keyword search across one or all D4Ds, returning section-labelled snippets. This supports cross-GC questions like "which challenges collected consent information?".
  - Parse sections from the real HTML heading structure. D4D follows the standard Datasheets-for-Datasets headings (Motivation, Composition, Collection, Preprocessing, Uses, Distribution, Maintenance), but verify against the actual HTML.
  - Ignore the CSS row.
- **Prompt sub-routine.**
  - Triggers: the user asks about a Grand Challenge's dataset datasheet, D4D, composition, collection process, intended uses, distribution or maintenance.
  - Flow:
    1. Resolve the GC (via `listD4Ds`/organizations).
    2. Fetch the outline and give a short overview.
    3. Offer the sections as `<guideprompt>` choices.
    4. Answer only from the retrieved section text, naming the section.
    5. Link to the org detail page.
  - For cross-GC comparisons, use `searchD4D` first.
  - Never paraphrase beyond the text, and say clearly when a D4D doesn't cover something.
- **Benchmarks (Phase 3):** add D4D sessions to kb-routing and resource-search, grounded in the real D4D text.

## Phases

1. **Agent scaffold and SQL backend**: done 2026-09-24, uncommitted. 70 tests pass, and they now run from the repo root via `tests/conftest.py`.
   - Rename to `agents/b2ai-copilot`, SQL variant only. New `TABLES`.
   - Dropped the Synapse-Dataset/file/restriction ops.
   - `buildPortalUrl` covers detail pages plus `/Search/Standards?SEARCH_TERM=`.
   - Rewrote the prompt. Fixed the `'Yes'`/`'No'` flag columns in review.
   - Placeholders `REPLACE_ME_B2AI_KB_ID` and `REPLACE_ME_B2AI_S3_BUCKET`.
   - **1b: done 2026-09-24.** 107 tests pass.
     - **Pins.** `TABLES` pinned to the portal's versions. `_bare_id()` puts the bare id in the URL and the versioned id in the SQL `FROM`.
       - Pinned row counts: standards 1029, datasets 116, organizations 130, topics 82, substrates 81, manifest 70, d4d 5.
       - `scripts/check_table_pins.py` reports 7/7 OK against monorepo `main`.
     - **Name.** Role and description now say "Bridge2AI Standards Explorer Copilot". `AgentName` stays `b2ai-copilot-sql`.
     - **D4D ops.** `listD4Ds`, `getD4D` and `searchD4D` added, with stdlib HTML parsing, a 15k-char paging budget and caching. Verified live: 4 GCs, 114 AI-READI, 115 CHoRUS, 116 CM4AI, 117 Voice.
       - The real D4D sections are flat: motivation, composition, collection-process, uses, distribution, maintenance, human-subjects. There's no "Preprocessing" section.
     - **Parser fix.** `_parse_response_body` now uses `json.loads(strict=False)`, because Synapse returns unescaped control characters in LARGETEXT.
     - **Prompt.** A "D4D Exploration" section was added. The Instruction is 13,314 chars.
   - **1c: done 2026-09-24.** 120 tests pass.
     - `buildPortalUrl` search takes optional `facets` (12-column allowlist; duplicate columns are merged) and encodes them as qw0 with the self-verify roundtrip.
     - The resolved semantics are in the docstring and prompt. The Instruction is 13,674 chars.
     - Links generated by the Lambda were browser-verified: Image + Ontology or Vocabulary → 1; segmentation + Image → 3; `SEARCH_TERM=imaging+genomics` → 213.
2. **Docs KB crawl**: spiders written and smoke-tested on 5 pages; the full crawl waits for confirmation.
   - Keeping **two sources**. The user's note suggested one, but I checked: the registry site has no schema docs. Its sitemap is only data-catalog pages (82 substrates, 53 topics, 44 use cases, plus DataTopic/Organization/DataSubstrate/UseCase listings, categories, curation, programmatic-access, ai-access, manifest). No page mentions slots, cardinality or LinkML classes, and the home page links out to the standards-schemas GitHub repo for the schema.
   - `b2ai_schemas_docs_spider.py` crawls from the index page, because the standards-schemas sitemap lists 404 URLs (`standards-schema/`, singular). That's worth reporting upstream.
3. **Benchmarks.**
   - Regenerate the general-help QA (`generate-help-qa-dataset`), then kb-routing (`qa-to-kb-routing-dataset`; labels `DOCS`/`RAG` still fit).
   - redteam: rename entities, and remove/replace `access-restriction-disclosure` (no restrictions here).
   - Build the resource-search correctness benchmark, including multi-word search semantics and D4D.
   - Delete the CCKP datasets as they're replaced.
4. **Docs site, README, CHANGELOG.**
   - Hugo branding and content, and the root README with lineage credit (CCKP → NF Portal Copilot).
   - `agents/README.md`: reset registrations and note that registration 236 will be replaced.
   - A fresh CHANGELOG section, and templates.md edited to drop SPARQL references.
5. **Validation.** pytest, dataset schema checks, a `cckp` sweep, and a placeholder audit.

## Open items

- [x] Deleted by the user on 2026-09-24: `agents/cckp-copilot/` (SPARQL template plus `cckpGraphRag/`), `deploy-copilot-sparql.yml`, the CCKP/MC2 spiders and `get.html`. They're recoverable from `8684452`.
  - `Makefile` retargeted to the B2AI SQL stack only.
  - Legacy spider rows removed from the general-help README.
  - Remaining references are left for later phases: agents/README.md, CHANGELOG, the docs site (templates.md, deployment.md, workflow page) → Phase 4; `redteam_config.json` → Phase 3; the retarget skill's spider-template pointers → Phase 4 (point at `git show 8684452:...` or at the B2AI spiders). The historical `plans/*.md` are left as-is.
- [ ] S3 artifact bucket name. The user will provide it.
- [ ] Bedrock KB ID. The user will provide it.
- [ ] Registration 236 cutover. The user will do it after deployment.
- [x] Search semantics investigation (resolved; see Backend facts).
- [x] `facets` on the `buildPortalUrl` search, with the resolved semantics documented.
- [x] D4D sub-routine and version pinning.
- [ ] Stray `get.html` (Synapse REST doc page) in the repo root, left by the search investigation. Untracked; the user should delete it (deletion is gated).
- [x] Datasets/substrates/manifest have no detail pages, by design.
- [x] Lambda tests runnable from the repo root (conftest added).
- [x] Pin versions: yes.
- [x] Single vs dual crawl source: dual (see Phase 2).
