# Resource Search Correctness Benchmark

Evaluates whether the CCKP Copilot's resource-backend tool calls are
**correct** for ordinary, good-faith resource-discovery questions — not just
whether the right source was consulted (see `benchmark/kb-routing`) or
whether the agent resists adversarial pressure (see `benchmark/redteam`).

## Relationship to the other three benchmarks

| Benchmark | Question it answers |
|---|---|
| `general-help` | Does the agent answer docs/policy/data-model questions correctly? |
| `kb-routing` | Did the agent consult the right **source** (docs KB vs. resource backend vs. redirect)? Never checks whether the query/answer itself was right. |
| `redteam` | Does the agent resist **adversarial** pressure (jailbreaks, social engineering, injection)? |
| **`resource-search`** (this one) | Given that the agent *did* call the resource backend, did it build the **right filter/join/status check** and report the **right answer** — under plain, good-faith questions? |

A concrete example of the gap this closes: an agent can score perfectly on
`kb-routing` (always calls the resource-backend action group for a data
question) while still building the wrong filter and silently returning the
wrong records — `kb-routing` has no way to catch that, by design. This
benchmark decodes the actual tool-call parameters from the Bedrock trace to
catch exactly that failure mode.

It also shares one underlying mechanism with `redteam`'s
`access-restriction-disclosure` item (both exercise
`checkRestriction`/`getDatasetFiles`/`getFileDetails` against the real
AR-gated dataset `syn64713343`) but under different conditions: `redteam`
tests it under social-engineering pressure; this benchmark's
`do-restricted-*` items test the same mechanism under a plain, direct ask.
Both should pass for the agent to be considered correct here.

## Why this exists

Two plan files in this repo (`plans/combined-filter-search-expressions.md`,
`plans/fix-searchexpressions-and-semantics.md`) document a real bug that
shipped in exactly this area: the agent built a redirect using two
`searchExpressions` entries assuming they'd AND together (as two
independent, required filters); they actually OR/union, and a query that
should have matched 6 real records instead showed 1,449. The bug was only
caught by a user manually cross-checking the count. This benchmark exists so
the next version of that bug fails a test run instead of shipping quietly —
see `mf-and-trap-glioma-fluorescence-microscopy` and
`mf-redirect-and-trap-glioma-fluorescence-microscopy`, which reproduce that
exact scenario with live, current data.

## Dataset

`resource_search_dataset.json` — 19 items across five categories, matching
the sections of the team's own demonstration question bank
(`Downloads/CCKP Copilot demonstration plan and question bank.pdf`):

| Category | Items | What it checks |
|---|---|---|
| `keyword-search` | 5 | Free-text topic search returns the right table/records |
| `multi-filter` | 4 | AND vs. OR filter-combination correctness (the bug class above) |
| `linked-resources` | 3 | Publication↔Dataset join, both directions, plus a negative (no-link) case |
| `dataset-operations` | 4 | Download/file-listing/access-check correctness across open, externally-hosted, and AR-restricted real examples |
| `redirect` | 3 | Same filter-correctness questions as `multi-filter`/`keyword-search`, but for `buildExploreUrl`'s decoded `qw0` shape instead of `sqlQuery`'s `sql` param |

**Every item is grounded in real, live-verified Synapse data — not
guesses.** Every `gold_query` and `known_facts` value in the dataset was
independently run against Synapse's public View tables
(`syn21897968`/`syn21868591`, CCKP's Datasets/Publications tables) and the
public `restrictionInformation` endpoint on 2026-09-22, the same way this
benchmark's own evaluator re-checks them at eval time (see below) — see each
item's `notes` field for the specific verification. Portal content
continues to grow and change; re-verify before trusting an old run's numbers
(the live-gold design below exists specifically so this doesn't require
manually re-deriving anything).

### Item schema

See `resource_search_schema.json`. Key fields:

- `expected_tool` — which action-group function a correct answer should
  invoke (`sqlQuery`, `buildExploreUrl`, `getDatasetFiles`,
  `getFileDetails`, `checkRestriction`, or `any` when either `sqlQuery` or
  `buildExploreUrl` reasonably serves the question).
- `expected_shape` — what a *correct* tool call looks like, decoded from the
  trace: `must_include_any` (free-text terms), `facets` (required AND
  columns), `or_group` (a column whose values should combine as an OR — see
  the open question below), `join` (acceptable Publication↔Dataset join
  directions), `id` (for dataset-operations items).
- `gold_query` — SQL the evaluator runs live against Synapse at eval time
  for an independent correctness signal, deliberately **not** a frozen
  count (CCKP is an actively growing portal; a snapshot number would go
  stale and start failing the benchmark for reasons that have nothing to do
  with the agent).
- `known_facts` — for `dataset-operations` items, the real resource's
  ground-truth hosting/restriction status, also re-checked live at eval
  time.
- `judge_check` — for items where correctness lives in what the agent
  *claims* in its answer text rather than in the decoded tool params alone
  (the AND/OR-trap items, the restricted-dataset items) — a specific
  yes/no question for the LLM judge, distinct from generic answer-quality
  judging.

### Open item flagged for Step 2 (human validation)

`mf-or-glioma-atac-or-rna-publications`,
`mf-and-human-glioma-atac-rna-datasets`, and
`rd-glioma-atac-rna-datasets-collection` all use an `or_group` shape that
accepts **either** two `searchExpressions` entries (confirmed to union) **or**
a single `facets` entry with multiple values, on the assumption that
multiple values within one facet column OR together — standard faceted-UI
convention, but **not independently live-tested against this portal in this
session** the way the AND-via-facets and OR-via-searchExpressions behaviors
already were (see `plans/fix-searchexpressions-and-semantics.md`). Confirm
this empirically — the same way the original `searchExpressions`-AND
assumption was disproven — before treating a disagreement on these three
items as a real agent failure rather than an unverified assumption in the
benchmark itself.

## Step 1: Expand the dataset

Add items directly to `resource_search_dataset.json` following
`resource_search_schema.json`. Every new item needs its `gold_query`/
`known_facts` actually run against live data before being added — see
"Dry-run gold queries" below for the exact mechanism; don't hand-write a
plausible-looking SQL string or status without running it.

## Step 2: Human validation

- Verify each `expected_shape` is actually the correct filter/join/status —
  an eval is only as good as its own ground truth.
- Resolve the open `or_group` question above via a real live test (mirror
  `plans/fix-searchexpressions-and-semantics.md`'s methodology: pick a
  genuinely independent value pair, compare individual counts to the
  combined count).
- Confirm `judge_check` items' questions are unambiguous yes/no checks — a
  judge_check the LLM itself might read either way defeats the point of
  making it a targeted claim check instead of generic answer-quality
  judging.

## Step 3: Dry-run gold queries (before running against a real agent)

```bash
cd benchmark/resource-search
python3 -c "
import json
from evaluate_resource_search import run_synapse_query, run_restriction_check
dataset = json.load(open('resource_search_dataset.json'))
for item in dataset:
    if 'gold_query' in item:
        print(item['id'], run_synapse_query(item['gold_query']))
    if 'known_facts' in item:
        print(item['id'], run_restriction_check(item['known_facts']['id']))
"
```

This hits Synapse's public REST API directly — **no credentials needed**
for CCKP's Dataset/Publication View tables specifically, since they're
public-readable. Confirms every `gold_query`/`known_facts` value in the
dataset still resolves the way the dataset claims before trusting an eval
run's diffs against them.

## Step 4: Evaluate

```bash
cd benchmark/resource-search
python evaluate_resource_search.py --agent-id ABC123                     # shape + gold checks only
python evaluate_resource_search.py --agent-id ABC123 --judge             # also run judge_check items
python evaluate_resource_search.py --agent-id ABC123 -n 5                # quick test: first 5 items
python evaluate_resource_search.py --agent-id ABC123 --item do-restricted-download-claim
python evaluate_resource_search.py --agent-id ABC123 --no-live-gold      # skip live Synapse calls entirely
```

`--agent-id` is required — no CCKP agent has been deployed yet. The default
alias `TSTALIASID` always points to the DRAFT version. If you've updated the
agent (instructions, model, action groups) without preparing it, run
`aws bedrock-agent prepare-agent --agent-id <ID>` first.

| Flag | Default | Description |
|---|---|---|
| `--agent-id` | _(required)_ | Bedrock Agent ID |
| `--alias-id` | `TSTALIASID` | Bedrock Agent alias ID (DRAFT) |
| `--profile` | env credentials | AWS profile |
| `--region` | `us-east-1` | AWS region |
| `--dataset` | `resource_search_dataset.json` | Item dataset |
| `--output` | `resource_search_eval_results.json` | Base output path (datestamp appended) |
| `--judge-model` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Model for the LLM judge |
| `-n` | all | Only run the first N items |
| `--item` | — | Run a single item by id |
| `--judge` | off | Enable LLM judge for `judge_check` items |
| `--no-live-gold` | off | Skip live Synapse `gold_query`/`known_facts` re-checks |

### Requirements

```bash
pip install boto3 pandas
```

AWS credentials with access to the Bedrock Agent and Bedrock Runtime (for
the judge model), same as the other two agent-invoking benchmarks. No
Synapse credentials are needed for the live-gold queries against CCKP's
public View tables — see "Live gold queries" below for what changes if a
future retarget's backend tables aren't public.

### How grading works

For each item, the script:

1. Invokes the agent with `enableTrace=True` (a fresh session per item).
2. Parses the orchestration trace's `actionGroupInvocationInput` /
   `actionGroupInvocationOutput` events to recover the **actual function
   name and parameters** the agent sent to the Lambda — not just "was an
   action group called" (that's all `kb-routing` checks).
3. Scores the decoded params against `expected_shape`
   (`shape_score`: 0 wrong / 1 partial / 2 fully correct).
4. If the item has a `gold_query`, runs it live against Synapse and reports
   it alongside the agent's implied answer (informational — scored
   separately since live counts drift, not folded into `shape_score`).
5. If the item has `known_facts`, re-checks them live and flags if the
   fixture itself is now stale (portal content can change) rather than
   silently trusting an old fixture.
6. If `--judge` is set and the item has a `judge_check`, asks an LLM judge
   the item's specific yes/no correctness question against the agent's full
   response text.

### Live gold queries — a note for retargeting this benchmark

`run_synapse_query`/`run_restriction_check` call Synapse's public REST API
directly with **no authentication**, because CCKP's Dataset/Publication View
tables happen to be public-readable — the same reason the demo bank's own
examples could be verified this way while building this dataset. This is
simpler than `plans/resource-search-correctness-benchmark.md` originally
anticipated (it flagged needing new Synapse credentials as a real added
dependency). A future portal (or a future CCKP backend change) whose tables
require auth will need to add a token to these two functions' requests —
they're isolated in one place specifically so that's a small, localized
change if/when it's needed.

### Output

Results are saved as `resource_search_eval_results_<timestamp>.json`:

- `timestamp`, `config` — run metadata (agent/alias ids, judge model,
  whether `--judge`/live-gold were enabled)
- `results` — per-item: decoded `tool_calls`, `shape_score`/`shape_notes`,
  `gold_result` (live query result, if applicable), `known_facts_live`/
  `known_facts_still_valid` (if applicable), `judge_score`, full
  `agent_response`
- `errors` — items that failed during invocation

### Metrics printed

| Metric | Description |
|---|---|
| Shape correctness | % of items scored 2 (fully correct) / 1 (partial) / 0 (wrong) |
| Per-category breakdown | Shape correctness broken down by the five categories |
| Stale-fixture warning | Flags any `dataset-operations` item whose `known_facts` no longer matches live Synapse data |
| Judge-assisted claim checks | % of `judge_check` items the judge scored as correct, with each failure named |

## Scope

Covers the SQL backend only (`agents/cckp-copilot/lambda/cckpSqlRag`) — the
one actually deployed today. The SPARQL variant
(`plans/implement-sparql-backend.md`) is still mid-implementation; add
`GRAPH`-scoped equivalents here once it's live and its own real query/
response shapes are confirmed, rather than building speculatively against an
unverified target (the same lesson `cloudformation.sparql.yaml` already
illustrates once).
