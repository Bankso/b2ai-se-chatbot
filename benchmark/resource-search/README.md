# Resource Search Correctness Benchmark

Grades whether the Bridge2AI Standards Explorer Copilot's resource-backend (SQL action group) tool calls are **actually correct** for good-faith, non-adversarial resource-discovery questions — not just whether it picked the right *source* (that's `kb-routing`'s job) and not under adversarial pressure (that's `redteam`'s job). It decodes the agent's real tool-call parameters straight out of the Bedrock trace, grades them deterministically against a hand-authored "correct shape" per question, and separately checks the final answer's facts against ground truth computed **live** against Synapse at the Lambda's pinned table versions.

> **Status:** built 2026-09-25 as part of the Bridge2AI Standards Explorer retarget (`plans/retarget-b2ai-standards-explorer.md`, `plans/resource-search-correctness-benchmark.md`). Adapted from the CCKP-era plan for this portal's real SQL-only backend (no SPARQL variant, no dataset access-restriction mechanism — B2AI is fully open access) and its D4D exploration sub-routine (which CCKP never had). **No B2AI agent is deployed yet**, so `evaluate_resource_search.py` has not been run against a live agent — only its trace-parsing/grading functions have been unit-tested against a synthetic fixture (see [Self-check](#self-check)).

## Relationship to the other three benchmarks

| Benchmark | Question it answers |
|---|---|
| `kb-routing` | Did the agent consult the right *source* (docs KB vs. the SQL action group vs. a redirect)? Never checks whether the parameters or the answer were correct. |
| `redteam` | Under adversarial pressure, does the agent stay in scope, refuse harmful requests, and not fabricate/leak? |
| **`resource-search` (this one)** | Given a plain, good-faith question and assuming the agent already routed correctly, did it build the *right SQL/facets/D4D call* and report the *right answer*? |
| `general-help` | Docs-KB question answering only (process/policy/data-model reference content); never touches live resource data. |

Concretely, this benchmark exists because a real, previously-shipped bug in this exact area — `searchExpressions`/facet AND-vs-OR confusion, and the `'Yes'`/`'No'` string-flag columns being mistaken for booleans (see `plans/retarget-b2ai-standards-explorer.md` phase 1) — would pass `kb-routing` perfectly (the agent *did* call the SQL action group) while still returning a wrong filter or a wrong count. Nothing else in this repo's benchmark suite would catch that.

## Coverage

`resource_search_dataset.json` currently has **42 items** across 8 categories:

| Category | Count | What it targets |
|---|---:|---|
| `keyword-lookup` | 6 | A specific standard by exact acronym (FHIR, DICOM, REDCap, NCIT, LOINC, SNOMED CT) resolves to the one correct `B2AI_STANDARD` id. |
| `categorical-filter` | 6 | Single-column filters: `category` (Ontology or Vocabulary, Registry), the `'Yes'`/`'No'` string flags `usedInBridge2AI`/`isOpen` (the exact historical bug class — an agent comparing to a bare `true`/`false` fails), `mature` (`'Is Mature'`/`'Is Not Mature'`), and a `topic HAS` single-value filter. |
| `multi-filter-and` | 4 | Two concepts that must **both** hold — across different columns (e.g. `topic` + `category`) or two string-flag columns — graded as SQL `AND` / two facets, never a multi-word `SEARCH_TERM`. |
| `multi-filter-or` | 2 | Either-of intent on the **same** column (`topic HAS ('Image','Genome')`, `category = X OR category = Y`) — OR is correct here. |
| `linked-resource` | 7 | Cross-table joins: standard → relevant/responsible organizations (both directions, including the standard→org and org→governed-standards inverse pair), dataset → producing org, topic → standards, org → datasets, plus a deliberate **negative case** (a standard with no linked organization at all). |
| `redirect` | 8 | Decoded `qw0` filter-shape correctness for `buildPortalUrl` search links: AND-across-columns, OR-within-a-column, term-AND-facet, a plain single-word `SEARCH_TERM`, three Detail Page redirects (standard/org/topic), and one **forbidden-shape** item that specifically catches a two-word `SEARCH_TERM` masquerading as an AND. |
| `d4d` | 5 | The Grand Challenge Datasheets-for-Datasets sub-routine: a single-GC section fact (`getD4D` with `section`), a cross-GC `searchD4D` comparison (consent — hits 3 of 4 GCs, not all 4), an outline request, a deliberate **"not covered"** case (`searchD4D('blockchain')` → 0 hits across all 4), and a basic `listD4Ds`. |
| `counts` | 4 | `countByType()` across the 4 types the question actually asks about (standards/datasets/organizations/topics — `expected_answer.counts` is restricted to those 4 even though `countByType()` itself returns all 7 pinned tables), two per-category counts, and a table-total count. |

Verified live sanity anchors from the retarget plan are reproduced by this dataset's own live queries: `topic HAS ('Image')` → 67; `topic HAS ('Image') AND category = 'Ontology or Vocabulary'` → 1; `topic HAS ('Image','Genome')` → 121. (The plan's search-index anchor, `SEARCH_TERM="imaging genomics"` → 213, is not re-verified live here — grading for that shape is the decoded facet/searchTerm parameters, not a live search-index count; see [Design deviations](#design-deviations-from-the-cckp-plan).)

There is **no `dataset-operations`/access-restriction category** (present in the CCKP-era plan this was adapted from) — every B2AI table is fully open access (confirmed in the Lambda's own Instruction text), so there is no restricted/external-hosting status mechanism to test here.

## Files

| File | Purpose |
|---|---|
| `resource_search_schema.json` | JSON Schema (draft-07) for the dataset. |
| `resource_search_dataset.json` | The 42 items, plus `generated_at`/`pinned_versions`/`category_counts`. |
| `build_ground_truth.py` | Recomputes the dataset **live** against Synapse. Run this whenever the Lambda's `TABLES` pins change. |
| `evaluate_resource_search.py` | Invokes a deployed agent, decodes its trace, and grades tool-call shape + answer facts. |
| `tests/test_evaluate_resource_search.py` | Unit tests for the trace-decoding/grading functions against a synthetic trace fixture — no AWS calls. |

## Dataset structure

Unlike `kb_routing_dataset.json` (a plain array), this dataset is a top-level **object**, because every item's ground truth is tied to a specific set of pinned Synapse table versions that must travel with the items so staleness is detectable:

```json
{
  "generated_at": "2026-09-25T03:41:12Z",
  "generated_by": "build_ground_truth.py",
  "pinned_versions": { "standards": "syn65676531.99", "...": "..." },
  "category_counts": { "keyword-lookup": 6, "...": "..." },
  "items": [ { "...": "one item, see below" } ]
}
```

Each item (see `resource_search_schema.json` for the full contract):

```json
{
  "id": "rs-and-image-ontology",
  "category": "multi-filter-and",
  "question": "Which ontologies or vocabularies are about the Image data topic?",
  "persona": "RESEARCHER",
  "notes": "AND-intent across two DIFFERENT columns... verified live: 1 hit.",
  "expected_tool_calls": [
    {
      "function": "sqlQuery",
      "required": false,
      "constraints": {
        "table": "standards",
        "and_columns": ["topic", "category"],
        "required_values": ["Image", "Ontology or Vocabulary"]
      }
    },
    {
      "function": "buildPortalUrl",
      "required": false,
      "constraints": {
        "resourceType": "search",
        "required_facets": [
          { "columnName": "topic", "values": ["Image"] },
          { "columnName": "category", "values": ["Ontology or Vocabulary"] }
        ],
        "facet_match": "subset",
        "search_term_forbidden_multi_word": true
      }
    }
  ],
  "expected_answer": { "count": 1, "matching_ids": ["B2AI_STANDARD:410"], "matching_names": ["FBBI"] },
  "ground_truth_query": "SELECT * FROM {standards} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'",
  "llm_judge_fallback": false
}
```

`expected_tool_calls[].constraints` keys are documented per-function in the schema; the important ones:

- **sqlQuery**: `table`, `required_columns`/`required_values` (must appear in the SQL), `forbid_values` (must *not* appear — catches the boolean-vs-string-flag bug class), `and_columns` (AND-intent: all columns present + enough literal `AND`s), `or_column`/`or_values` (OR-intent: a `HAS(...)` or explicit `OR` on one column). Every `sqlQuery` constraint also requires the literal `{table}` placeholder in `FROM` and rejects any other bare `synNNN` reference outside a string literal — the Lambda itself now enforces this server-side (commit `22de2a2`, "Restrict SQL queries to the pinned B2AI tables"), so a correct agent call must too.
- **buildPortalUrl**: `resourceType`, `id`, `search_term_exact`/`search_term_forbidden_multi_word`, `required_facets` (checked by **decoding `qw0`**, not string-matching the URL) with `facet_match: "exact"|"subset"`.
- **getD4D**: `orgId`, `section` (a string id, or `null` to require the outline call).
- **searchD4D**: `query_contains`, `orgId`/`orgId_must_be_absent` (cross-GC questions must NOT scope to one org).

## Refreshing ground truth

```bash
cd benchmark/resource-search
python3 build_ground_truth.py                # writes resource_search_dataset.json
python3 build_ground_truth.py --check         # validate only, don't write
```

This imports `agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py` directly and calls its own `sql_query`/`build_portal_url`/`get_d4d`/`search_d4d`/`list_d4ds`/`count_by_type` functions against **live Synapse**, anonymously, at the Lambda's pinned `TABLES` versions — the exact same code path the deployed Lambda runs. No id, name, or count in the dataset is invented; every `expected_answer` and several `ground_truth_query` strings are the literal query that produced it, for auditability. Several items also assert their value against the retarget plan's own live-verified sanity anchors (e.g. `topic=Image` must equal 67) and fail loudly if the portal's pinned data has drifted.

**Run this whenever `TABLES` in `lambda_function.py` changes** (a re-pin). `evaluate_resource_search.py` checks `dataset["pinned_versions"]` against the live Lambda's `TABLES` at eval time and warns (but does not block) if they've drifted.

Because B2AI pins exact Synapse table *versions* rather than querying a live-growing portal (unlike the CCKP plan this was adapted from), the resulting counts/ids are stable snapshots — ground truth is computed once at build time, not re-queried live at every evaluation run. See [Design deviations](#design-deviations-from-the-cckp-plan).

## Evaluating

```bash
pip install boto3 pandas jsonschema pytest
cd benchmark/resource-search
python3 evaluate_resource_search.py --agent-id ABC123                 # all 42 items
python3 evaluate_resource_search.py --agent-id ABC123 -n 5            # quick test: first 5
python3 evaluate_resource_search.py --agent-id ABC123 --item rs-keyword-fhir
python3 evaluate_resource_search.py --agent-id ABC123 --no-judge      # disable the LLM-judge fallback
```

`--agent-id` is **required, with no default** — no B2AI agent has been deployed yet. `PROD_AGENT_ID` in `evaluate_resource_search.py` is a placeholder (`REPLACE_ME_B2AI_PROD_AGENT_ID`) until a real prod agent exists; once it does, fill it in and the script will refuse to run against it without `--allow-prod`, the same guard `evaluate_redteam.py` uses.

| Flag | Default | Description |
|---|---|---|
| `--agent-id` | _(required)_ | Bedrock Agent ID |
| `--alias-id` | `TSTALIASID` | Bedrock Agent alias ID (DRAFT) |
| `-n` | all | Only run the first N items |
| `--item` | — | Run one item by id; overrides `-n` |
| `--no-judge` | off | Disable the LLM-judge fallback (items needing it are left unscored, not force-failed) |
| `--judge-model` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Judge model |
| `--profile` / `--region` | env / `us-east-1` | AWS profile/region |
| `--dataset` / `--output` | `resource_search_dataset.json` / `resource_search_eval_results.json` | Input/output paths (output gets a UTC datestamp appended) |
| `--allow-prod` | off | Required to run against `PROD_AGENT_ID` |

### How grading works

1. **Trace decoding.** For each item, the agent is invoked fresh (`enableTrace=True`) and every `actionGroupInvocationInput`/`actionGroupInvocationOutput` pair in the orchestration trace is extracted (`extract_tool_calls`). Each trace's `requestBody` is first normalized (`_normalize_request_body`) into the shape the Lambda's own `extract_params` expects: per the Bedrock agent-runtime API reference, the orchestration *trace*'s `ActionGroupInvocationInput.requestBody.content["application/json"]` is a bare **list** of `{name, type, value}` Parameter objects, whereas `extract_params` (written against the Lambda *invocation event* shape) expects that same list wrapped as `{"properties": [...]}`; a dict already in that shape passes through unchanged, and a missing/empty `requestBody` normalizes to an empty content map rather than raising. **This trace shape is inferred from the API reference and has not yet been confirmed against a real `invoke_agent` trace** (no B2AI agent is deployed yet) — re-verify it against the first real trace once one exists, and adjust `_normalize_request_body`/the fixtures if it differs. Parameters are then decoded with the Lambda's **own** `extract_params`/`map_api_path_to_function` (imported directly from `lambda_function.py`), so decoding here exactly matches what the deployed Lambda itself sees — including its workaround for Bedrock's malformed pseudo-JSON array values. Function-details agents put params directly in `parameters` (already a list), which `extract_params` reads as-is and needed no normalization.
2. **Tool-call shape grading** (`grade_tool_calls`). Each `expected_tool_calls` entry is matched to the best actual call of the same function — preferring one whose "identity" fields (table/resourceType/id/orgId/section/exact searchTerm) agree, so a real defect in a same-function sibling call isn't accidentally masked — then checked against its `constraints` deterministically (regex/substring checks on the SQL text, or a decoded `qw0` comparison for facets). Optional (`"required": false`) entries don't count against the score if absent. Produces a `tool_call_score` in `[0, 1]`.
3. **Answer-fact grading** (`grade_answer_facts`). Deterministic-first: substring/id checks against the final chat text (or, for `redirect` items, the actual decoded tool *output* rather than the chat text). Returns `verified: True/False`, or `None` when the fact is inherently free-text (a D4D section's narrative, a "no link found" explanation, a cross-GC "which challenges" list) — those items are flagged `llm_judge_fallback: true` in the dataset and fall back to an LLM judge (`judge_answer`) comparing the final answer against the live-verified `expected_answer` and the item's `notes`, unless `--no-judge` is passed, in which case they're left unscored rather than forced to fail.
4. **qw0 decoding** (`decode_qw0`) mirrors the Lambda's own self-verify roundtrip in `build_portal_url` (urllib-unquote → base64-decode → gzip-decompress → `json.loads`) — this benchmark does not re-test the encoder itself (already self-verified server-side); it only checks whether the agent chose the right `facets` going into it.

## Self-check

```bash
cd /path/to/repo
python3 -m py_compile benchmark/resource-search/build_ground_truth.py benchmark/resource-search/evaluate_resource_search.py
python3 -c "
import json, jsonschema
schema = json.load(open('benchmark/resource-search/resource_search_schema.json'))
data = json.load(open('benchmark/resource-search/resource_search_dataset.json'))
jsonschema.validate(data, schema)
"
python3 -m pytest benchmark/resource-search -q
```

All three pass as of the last pre-PR review pass (55 unit tests, 42/42 items schema-valid). The unit tests build two synthetic Bedrock trace fixtures in-process (`tests/test_evaluate_resource_search.py`'s `synthetic_trace_events` fixture, dict-shaped `requestBody.content`, and `real_shape_trace_events`, list-shaped `requestBody.content` matching the real trace shape per the API reference — a real `qw0` is generated via `lambda_function.build_portal_url` itself, which is pure/offline computation, not a network call) covering: extracting six chained tool calls (sqlQuery, two buildPortalUrl calls including a deliberately-bad multi-word `searchTerm`, two getD4D calls, one searchD4D call), decoding both `requestBody.content` shapes (dict and real list) including array-typed/JSON-string parameters the way Bedrock actually sends them, `qw0` round-tripping (including a `required_facets` check against a missing/errored output and a qw0 that fails to decode, each counted as exactly one failure), every constraint checker (including the AND/OR/boolean-flag/`{table}`-placeholder bug classes this benchmark exists to catch, now delegated to the Lambda's own quote-aware `_check_sql_tables` rather than a local regex reimplementation), the tie-breaking fix in `grade_tool_calls`, boundary-aware id/count matching in answer-fact grading (so `B2AI_ORG:1` doesn't match `B2AI_ORG:114`, and a count doesn't match inside a longer number or an id), the linked-resource subject-id exclusion, and answer-fact grading per category.

**No live AWS/Bedrock run has happened in this session** — same as `redteam` and `kb-routing`, this stays a manual step run once a B2AI agent is deployed.

## Design deviations from the CCKP plan

`plans/resource-search-correctness-benchmark.md` was written for the CCKP Copilot before the B2AI retarget; adapting it surfaced a few deliberate differences, not oversights:

- **No SPARQL variant, no `dataset-operations`/access-restriction category.** B2AI has a SQL-only backend and zero access-restricted resources (confirmed in the Lambda's own Instruction text) — the CCKP plan's `checkRestriction`/external-hosting coverage has no B2AI analog. A `d4d` category replaces it, covering a sub-routine (`listD4Ds`/`getD4D`/`searchD4D`) that didn't exist when the CCKP plan was written.
- **Ground truth is a build-time snapshot, not a live-at-eval-time re-query.** The CCKP plan's `gold_query` design re-runs SQL against Synapse at *evaluation* time specifically because CCKP has no version pinning and its live counts drift as the portal grows. B2AI pins exact table versions (`TABLES` in `lambda_function.py`), so the same live-Synapse-sourced counts are already stable for as long as those pins hold — recomputing at every eval run would add evaluation-time Synapse dependency for no correctness benefit. Instead, `build_ground_truth.py` computes it once, records the pins it used, and `evaluate_resource_search.py` warns (non-fatally) if the dataset's recorded pins have drifted from the live Lambda's current `TABLES`.
- **The dataset is a top-level object, not a plain array** (unlike `kb_routing_dataset.json`), specifically to carry `pinned_versions`/`generated_at` alongside `items` — see [Dataset structure](#dataset-structure).
- **`expected_answer` is intentionally not deeply schema-validated** — its shape is inherently polymorphic per category (ids/names for `keyword-lookup`, counts for `counts`, section text facts for `d4d`, etc.); deep validation lives in `evaluate_resource_search.py`'s per-category grading functions and the unit tests instead, matching how `kb_routing_schema.json` also leaves `notes` loosely typed rather than modeling every possible shape in JSON Schema.
- **The CCKP plan's `SEARCH_TERM="imaging genomics"` → 213 anchor is cited but not independently re-verified live here** (the retarget plan already verified it against the live search index and browser). This benchmark's `redirect` items grade the decoded tool-call *shape* (facets/searchTerm parameters), which doesn't require hitting the search-index API at all — only the `sqlQuery`-backed categorical/multi-filter items' *counts* needed live re-verification, which `build_ground_truth.py` does directly.
