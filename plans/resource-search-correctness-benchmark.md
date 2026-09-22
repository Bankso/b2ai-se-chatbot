# Add a resource-backend search/retrieval correctness benchmark

## Context

Reviewing `Downloads/CCKP Copilot demonstration plan and question bank.pdf`
against the three existing benchmarks (`benchmark/general-help`,
`benchmark/kb-routing`, `benchmark/redteam`) surfaced a real coverage gap: the
demo bank's largest categories by far — **keyword search**, **multi-filter
search**, **linked resources**, **dataset operations**, and **redirect** — have
no benchmark checking whether the agent's answers are actually *correct*.

- `general-help` only grades docs-KB question answering (process/policy/data
  model reference content), not live resource data.
- `kb-routing` explicitly, by design, "measures source routing, not answer
  correctness" (its own README) — it checks whether the agent called `DOCS`
  vs. the resource-backend action group vs. `REDIRECT`, never whether the
  parameters it passed or the answer it gave were right.
- `redteam` only checks adversarial resistance (safety/security/privacy under
  attack), not correctness under ordinary, good-faith use.

So a real, demo-bank-documented failure mode has no regression coverage: the
agent could route to the right tool every time (scoring perfectly on
kb-routing) while still building the wrong filter, missing a linked resource,
or misreporting a dataset's access status — and nothing would catch it.

**This is not a hypothetical risk for this codebase.** Two of the plan files
already in this repo (`plans/combined-filter-search-expressions.md`,
`plans/fix-searchexpressions-and-semantics.md`) document a real, previously
-shipped bug in exactly this area: `searchExpressions` entries were assumed to
AND together and actually OR (union), confirmed by live-testing against
production (`agents/cckp-copilot/lambda/cckpSqlRag/lambda_function.py`'s
`build_explore_url` docstring has the full writeup, including the corrected
guidance: use `facets` for a true intersection, `searchExpressions` only
unions). That bug shipped and was only caught by manual live-testing. A
benchmark in this area would have caught it — and will catch the next one.

### What's actually being tested, grounded in the real Lambda

Reading `agents/cckp-copilot/lambda/cckpSqlRag/lambda_function.py` and the
Instruction text in `cloudformation.sql.yaml` directly (not guessing) turned
up the concrete mechanics this benchmark needs to grade against:

- **Tables**: `datasets` (syn21897968), `publications` (syn21868591), `tools`
  (syn26127427), `grants` (syn21918972), `education` (syn51497305).
- **AND vs. OR is already a confirmed, documented fact, not something this
  benchmark needs to re-discover**: `selectedFacets` (`facets` param) AND
  correctly; `additionalFilters`/`TextMatchesQueryFilter`
  (`searchExpressions` param) OR/union regardless of `searchMode` or
  `+`-operators. This benchmark's job is checking whether the **agent**
  reliably picks the right one for a given natural-language AND/OR intent —
  not re-verifying the backend's own behavior, which is already nailed down.
- **`build_explore_url` already self-verifies its own gzip/base64/JSON
  round-trip at runtime** (decodes its own `qw0` back and compares before
  returning). This benchmark does **not** need to re-test URL encoding
  mechanics — only whether the agent chose the right `facets`/
  `searchExpressions`/`table` going into that (already-correct) encoder. The
  qw0 param is trivial to decode from the outside (base64 → gzip → JSON),
  which is exactly how this benchmark should recover "what did the agent
  actually ask for," rather than trusting a raw trace field or the response
  text.
- **Publication↔Dataset join has two directions, and the Instruction
  explicitly requires checking both before concluding no link exists**:
  `publications.dataset` (alias field) ↔ `datasets.datasetAlias`, and
  `datasets.pubMedId` (multivalued) ↔ `publications.pubMedId`
  (`cloudformation.sql.yaml:328`). A linked-resources test item should
  include at least one pair only discoverable via each direction, plus one
  publication with a deliberately-absent link (to test the negative case
  isn't reported prematurely).
- **Dataset operations** (`getDatasetFiles`, `getFileDetails`,
  `checkRestriction`) key off a row's `downloadSynId`/`sourceRepository`,
  and an external `sourceRepository` (e.g. GEO) with no `downloadSynId` is
  documented as expected, not an error — a naive eval could wrongly flag
  correct "externally hosted" answers as failures if it doesn't know this.
  `checkRestriction`'s response is already the subject of the redteam
  benchmark's new `access-restriction-disclosure` item — but only under
  *adversarial pressure*. This benchmark's dataset-operations turns cover the
  same mechanism under **plain, good-faith** questions (does the agent
  correctly report open/GEO-hosted/AR-restricted status when just asked
  directly, not when being pushed to leak it).
- **Detail Page join keys** (needed to grade redirect/link correctness):
  `datasetId` (or alias — may be a GEO accession/DOI, not always a synId),
  `pubMedId`, `grantId` (`cloudformation.sql.yaml:308-311`).

## Approach

### 1. New benchmark directory: `benchmark/resource-search/`

Structured like the existing three (a dataset JSON + schema, an
`evaluate_*.py`, a `README.md`), reusing kb-routing's Bedrock-trace
inspection technique but going one level deeper than "which source was used."

- **`resource_search_schema.json`** — one test item per realistic question,
  categorized to match the demo bank's own sections so coverage is easy to
  audit against it directly:
  - `category`: `keyword-search` | `multi-filter` | `linked-resources` |
    `dataset-operations` | `redirect`
  - `question` — the natural-language ask (pull directly from the demo
    bank's examples where possible: "publications on chromosome
    instability," "human glioma ATAC-seq and RNA-seq datasets," etc. — these
    are already team-vetted, not invented from scratch)
  - `table` — expected primary table alias
  - `expected_shape` — what a *correct* tool call looks like, not a literal
    result: e.g. `{"facets": [{"columnName": "assay", "values": [...]}]}` for
    an AND-intent question, `{"searchExpressions": [...]}` for an OR/either
    -of intent, or for `linked-resources`, which join direction(s) are
    acceptable
    - This is the primary, objective thing to grade — it's fully recoverable
      by decoding `qw0` (for `buildExploreUrl` calls) or reading the `sql`
      param (for `sqlQuery` calls), so grading doesn't depend on a live
      count that can drift as the portal grows.
  - `gold_query` — for categories where a live correctness check is also
    wanted (`keyword-search`, `linked-resources`, `dataset-operations`), the
    equivalent hand-verified SQL the harness itself runs directly against
    Synapse at eval time (see §2) to get a live "correct" count/record set —
    deliberately **not** a frozen expected count, since CCKP is an actively
    growing portal and a snapshot number would go stale and start failing
    the benchmark for reasons that have nothing to do with the agent.
  - `known_facts` — for `dataset-operations` items only: the real resource ID
    plus its ground-truth status (`open` / `external:<repo-name>` /
    `restricted`), e.g. reusing `syn64713343` (AR-restricted, per the demo
    bank and the redteam benchmark's new item) alongside at least one
    Synapse-hosted-open example and one externally-hosted (GEO) example, so
    the eval doesn't wrongly penalize a correct "hosted on GEO, no
    `downloadSynId`" answer.
  - `persona`, `notes` — same as the other three benchmarks' schemas.

- **`resource_search_dataset.json`** — seed with the demo bank's own
  questions directly (they're already team-reviewed), organized by the
  categories above: keyword search (publications/datasets), multi-filter
  AND/OR (the glioma ATAC-seq/RNA-seq OR example, the human-glioma-AND
  example), linked resources (pub↔dataset joins both directions, plus a
  deliberate no-link negative case), dataset operations (download/files
  /file-metadata/access-check across open, external, and restricted
  examples), and redirect (the demo bank's explicit "generate a link to the
  collection of X" examples, graded on decoded filter shape).

- **`evaluate_resource_search.py`** — modeled directly on
  `benchmark/kb-routing/evaluate_kb_routing.py`'s structure (fresh session
  per item, `enableTrace=True`, same CLI flag conventions
  `--agent-id`/`--alias-id`/`--profile`/`--region`/`-n`/`--judge-model`), but
  its trace parsing goes further than detecting `ACTION_GROUP` fired:
  - Extract the actual `actionGroupInvocationInput` parameters Bedrock sent
    to the Lambda (tool name + input args) from the orchestration trace —
    this is already present in `invoke_agent`'s trace payload, just unused
    by the existing kb-routing script.
  - For a `buildExploreUrl`/`sqlQuery`/`countByType` call, decode the actual
    filter shape used (facets/searchExpressions/table, or SQL WHERE clause)
    and score it against `expected_shape`.
  - For categories with a `gold_query`, additionally run that query directly
    against the same Synapse table (via `synapseclient` or the table-query
    REST API — same mechanism the Lambda itself uses) at eval time, and
    report the agent's implied result set/count against this live gold
    answer, tolerant of natural drift (score by overlap/containment, not
    exact equality).
  - For `dataset-operations` items, compare the agent's reported status
    against `known_facts`, and separately check whether it disclosed
    file-level detail when the fact says `restricted` (same failure mode as
    the redteam item, graded here under non-adversarial conditions).
  - Reuses the LLM-judge pattern from the other two evals as a secondary,
    softer signal (is the final answer text coherent/on-topic given what the
    tool call returned) — primary grading stays objective/decodable.

- **`README.md`** — same four-step shape as kb-routing's (Dataset, Step 1
  expand, Step 2 human validation, Step 3 evaluate), plus an explicit
  "Relationship to other benchmarks" section spelling out the boundary with
  `kb-routing` (source selection vs. parameter/result correctness) and
  `redteam`'s `access-restriction-disclosure` (adversarial pressure vs.
  plain-ask correctness on the same underlying mechanism), so the four
  benchmarks' scopes stay legible as a set rather than drifting into overlap.

### 2. Live-gold vs. frozen-snapshot — decision needed, not made here

Flagging rather than deciding: computing "gold" answers live (harness calls
Synapse directly at eval time, same tables the Lambda queries) avoids the
staleness problem a frozen expected-count would hit as CCKP grows, but adds a
new operational requirement — the benchmark harness needs its own Synapse
read credentials (`synapseclient` login or a PAT), which none of the three
existing benchmarks need (they only need AWS/Bedrock credentials). This is a
real added dependency, not a free choice. Recommend: live gold for
`keyword-search`/`linked-resources`/`dataset-operations` (where a stale
number would produce false failures), but the primary grade for
`multi-filter`/`redirect` stays the decoded `expected_shape` match, which
needs no live query at all.

### 3. Scope to the SQL backend only for v1

The SPARQL backend variant (`plans/implement-sparql-backend.md`) is still
mid-implementation and not yet deployed. Building this benchmark
SQL-and-SPARQL-dual-backend from day one would repeat the SPARQL
template's own history in this repo — speculative code against an
unverified/nonexistent target. Scope v1 to the deployed SQL backend
(`table`/`sql`/`facets`/`searchExpressions` shapes above); add SPARQL
`GRAPH`-scoped equivalents as a follow-up once that variant is live and its
own real query/response shapes are confirmed, the same lesson already
learned and written up in the SPARQL plan.

## Verification

1. **Human validation pass** (same discipline as `kb-routing`'s Step 2):
   every `expected_shape` and `gold_query` needs a person to confirm it's
   actually the correct filter/join/status before it's used to grade the
   agent — an eval is only as good as its own ground truth.
2. **Dry-run the gold queries** against Synapse independently of the agent
   before wiring up the full evaluator, to confirm `gold_query` SQL is valid
   and the chosen linked-resource/dataset-operation examples still resolve
   the way `known_facts` claims (portal content can change).
3. **No live AWS/Bedrock run in this session** — same as the SPARQL plan,
   this needs a deployed agent ID and stays a manual step the user runs
   (`--agent-id` required, no default, matching the other three evals'
   convention).
4. **JSON schema validation** for both new JSON files before use, matching
   the pattern already applied to `redteam_config.json` and
   `kb_routing_dataset.json` in this session.
