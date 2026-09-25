---
title: Source routing
weight: 30
---

# Source routing

> [!NOTE]
> This benchmark only applies if your agent has **more than one** knowledge source (e.g. a docs KB plus a resource-query backend). If you only have a single docs KB, you would only need [grounded retrieval](/docs/benchmarking-and-evaluation/grounded-retrieval/) and can skip this page.

## What this tests

Source routing tests whether the agent efficiently consults the **correct** knowledge source for a given question. The agent has to be a good orchestrator, recognizing which is the best source to utilize without unnecessary diversions that add time and money costs. Aside from the underlying model intelligence, the system may include routing rules in the system prompt (or other methods, depending on the framework) that influence overall performance; this benchmark is how to validate and iterate whether the overall system is working sufficiently well.

The Bridge2AI Standards Explorer Copilot has two sources — a documentation KB and the SQL resource backend (including its D4D exploration sub-routine) — so it needs this eval in addition to [grounded retrieval](/docs/benchmarking-and-evaluation/grounded-retrieval/) on each source individually. The **kb-routing benchmark** (`benchmark/kb-routing/`) covers this.

## How the dataset is built

`kb_routing_dataset.json` is a curated set of multi-turn sessions, each turn labeled with the source it should trigger:

| `expected` value | Meaning |
|---|---|
| `DOCS` | Documentation KB (process, policy, data-model how-tos) |
| `RAG` | The SQL resource-backend action group (counts, lists, specific records, D4D exploration, `buildPortalUrl`) |
| `BOTH` | Either source is acceptable, or both are needed for a compound question |
| `REDIRECT` | A navigation redirect, no KB lookup needed |
| `NONE` | No lookup expected — general knowledge or decline |

Sessions cover each source individually, mixed-source conversations where the user pivots between question types, compound questions needing multiple sources, and off-topic questions that shouldn't trigger any lookup.

## Current dataset

`kb_routing_dataset.json` has **33 sessions / 70 turns**: 6 DOCS sessions (13 turns), 12 RAG sessions (24 turns), 9 MIXED sessions (26 turns), 2 BOTH sessions (3 turns), and 4 NONE sessions (4 turns) — plus a 10-turn `e2e-full-flow` end-to-end session covering a portal overview, a live filtered standards query, docs follow-ups, Grand Challenge D4D exploration, a redirect, and an off-topic close. Sessions are grounded in real Bridge2AI Standards Registry documentation and live Synapse table/D4D facts (cross-referenced against the general-help QA dataset and live queries against the deployed Lambda's functions). Step 2 human validation is still pending — see `benchmark/kb-routing/README.md` for status.

## Example from a real run

No B2AI agent has been deployed yet, so there's no real routing run to draw an example from. Once a dev agent exists, run `evaluate_kb_routing.py` and replace this section with a real example — ideally one session that pivots between sources mid-conversation (e.g. a docs question followed by a live-data-count question), since that's the failure mode this benchmark exists to catch.

## Scoring

Unlike grounded retrieval, there's no gold answer text to compare against — the primary signal comes from Bedrock trace events (`KNOWLEDGE_BASE` vs `ACTION_GROUP` invocations), not the response text:

| Score | Meaning |
|---|---|
| 2 | Correct and efficient — used exactly the expected source(s) |
| 1 | Correct but over-queried — right source plus unnecessary extras |
| 0 | Wrong source, or any source used when `NONE` expected |
| -1 | No trace detected (excluded from accuracy) |

## Running it

```bash
cd benchmark/kb-routing
python evaluate_kb_routing.py --agent-id <ID>                     # routing only
python evaluate_kb_routing.py --agent-id <ID> --judge             # also score answer quality via LLM judge
```

See `benchmark/kb-routing/README.md` for the full dataset schema, detection method, and all CLI flags.
