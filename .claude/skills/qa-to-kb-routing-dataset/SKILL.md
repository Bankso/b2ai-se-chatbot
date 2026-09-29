---
name: qa-to-kb-routing-dataset
description: Convert facts from the general-help QA dataset (benchmark/general-help/help_qa_dataset_*.json) into new multi-turn sessions for the KB routing benchmark (benchmark/kb-routing/kb_routing_dataset.json), grounding DOCS-labeled routing questions in real QA entries and synthesizing companion resource-backend/redirect/off-topic turns to round out each session. Use when the user wants to expand, refresh, or backfill kb_routing_dataset.json — including migrating it to a new domain — using facts already validated in the QA dataset, or asks to "convert the QA dataset to KB routing sessions" / "generate more routing sessions from the help docs QA".
---

You turn grounded question/answer facts from the general-help benchmark's QA dataset into multi-turn **source-routing** sessions for the KB routing benchmark. The two benchmarks test different things — general-help scores answer correctness against a single-source docs agent; KB routing scores whether a multi-source agent picks the *right knowledge source* per turn — so this is a genuine transformation, not a reformat: each QA fact becomes the grounding for a DOCS-labeled turn, then gets a synthesized companion turn (usually resource-backend, sometimes redirect/off-topic/compound) that a static QA dataset can't supply on its own.

## Background

Read both benchmarks' READMEs in full before touching anything: `benchmark/general-help/README.md` (QA dataset field reference) and `benchmark/kb-routing/README.md` (session/turn schema, source labels, session_type semantics). Also read `benchmark/kb-routing/kb_routing_schema.json` directly — **do not hardcode source label names in your head**. This skill was built shortly after the KB routing labels were renamed (`GRAPH` → `RAG`) precisely because that vocabulary drifted once already; always pull the current `session_type`/`expected` enum values from the schema file at run time, not from memory or from this doc.

Key asymmetry to keep in mind throughout: the QA dataset only contains facts answerable from static documentation (by construction — it's generated from crawled docs pages). That means it can *only* ever ground `DOCS`-type turns directly. Every other `expected` value in a session — the resource-backend/RAG turn, a `REDIRECT`, a `NONE` off-topic turn, a `BOTH` compound question — has to be synthesized by you, grounded in the same entities/topic as the QA fact but requiring something a docs lookup can't supply (a live count, a specific record, an aggregate, a navigation action, general world knowledge). This is the core skill: pairing a real documented fact with a realistic "you'd need live data for this" follow-up on the same topic.

## Protocol

1. **Locate the working directory** via `git rev-parse --show-toplevel`, then work from `benchmark/kb-routing/` and read `benchmark/general-help/help_qa_dataset_*.json` (usually `help_qa_dataset_anthropic.json`) as your source. If it doesn't exist yet, tell the user to run the `generate-help-qa-dataset` skill first — don't fabricate QA facts to fill the gap.

2. **Read the current schema** (`kb_routing_schema.json`) to get the live `session_type` enum, `expected` enum, and required fields (`session_id`, `session_type`, `description`, `n_turns`, `turns[].id/question/expected/persona`, optional `notes`). Also skim the existing `kb_routing_dataset.json` for `session_id` naming conventions and how detailed `notes` fields are, so new sessions read as part of the same set, not a distinct batch.

3. **Decide append vs. full replacement** with the user — default to **append** new sessions to the existing array (this is meant to be a repeatable, incremental way to grow the routing benchmark alongside the QA dataset). Only replace/reset the whole file if the user explicitly says the existing sessions are wrong for the current domain (e.g. a domain migration, which is how this skill was first used — see reference below).

4. **For each new session**, pick one or more QA dataset entries covering a specific entity/policy/field and build a session around them:
   a. **DOCS turn(s)**: rephrase the QA entry's `question` into a natural chat question (it doesn't need to be multiple-choice) and set `expected: "DOCS"`. Put the QA fact's correct answer/context into the turn's `notes` field, and consider noting which QA entry it came from (e.g. by page URL) so the routing session can be revisited if that doc page changes.
   b. **Companion turn(s)**: synthesize 1+ additional turns that plausibly follow in the same conversation but need a different source — a live count/list/specific-record question about the *same entity* (`expected` = the current resource-backend label from the schema), a navigation request (`REDIRECT`), an out-of-scope or general-knowledge aside (`NONE`), or an intentionally ambiguous/compound question needing both (`BOTH`). Ground these in real entities/fields too (e.g. an actual resource-table/entity name from the data model), even though — by definition — they're not literally QA-dataset rows.
   c. Set `session_type` to match the actual pattern of `expected` values across the turns (all-DOCS → `DOCS`; all resource-backend → the RAG-equivalent label; a mix → `MIXED`; all `BOTH` → `BOTH`; all `NONE` → `NONE`), and set `n_turns` to the literal length of `turns`.
   d. Vary `persona` realistically (CONTRIBUTOR/REUSER/FUNDER/PATIENT/X) and vary single- vs. multi-turn sessions and single- vs. cross-source patterns across the batch — don't produce 10 near-identical MIXED-2-turn sessions.

5. **Never fabricate a fact.** Every DOCS-turn claim must trace to an actual QA dataset entry or something you directly grepped from `benchmark/general-help/output_markdown/*.md`. If you need a fact the QA dataset doesn't cover (e.g. a specific field name to build a realistic resource-backend companion question), look it up in the raw crawled docs rather than guessing plausible-sounding field names.

6. **Validate before writing anything final**:
   ```bash
   cd benchmark/kb-routing
   python3 -c "
   import json, jsonschema
   data = json.load(open('kb_routing_dataset.json'))
   schema = json.load(open('kb_routing_schema.json'))
   jsonschema.validate(data, schema)
   ids = [t['id'] for s in data for t in s['turns']]
   assert len(ids) == len(set(ids)), 'duplicate turn id'
   for s in data:
       assert s['n_turns'] == len(s['turns']), s['session_id']
   print('VALID —', len(data), 'sessions,', len(ids), 'turns')
   "
   ```
   Fix any schema violation or duplicate ID before moving on — don't hand-wave past a validation failure.

7. **Regenerate the dataset composition table** using the benchmark's own helper (don't hand-edit the table):
   ```bash
   python3 update_readme_stats.py
   ```

8. **Report and hand off to Step 2.** Summarize sessions added (or replaced), the new `session_type`/turn-count totals, and which QA entries grounded which sessions. Point the user at `benchmark/kb-routing/README.md`'s Step 2 (human validation — verify `expected` labels, check multi-turn flow, flag ambiguous questions as `BOTH`) as the next step; don't perform that review yourself.

9. **Do not commit** unless the user asks — surface the updated dataset/README for review first.

## Reference: prior full-dataset conversion

The first real use of this process was a full-dataset migration, not an incremental append: `kb_routing_dataset.json` had been reset to `[]` after a repo fork because its old sessions encoded a different domain's policy (NF Data Portal facts that didn't apply to CCKP). That conversion:
- Recovered the old sessions from git history (`git show <reset-commit>^:path/to/kb_routing_dataset.json`) to use as a structural template (session/turn counts, session_type distribution, persona mix, multi-turn patterns) — it did **not** reuse their content, since it was the wrong domain.
- Re-derived every DOCS-turn fact from the new domain's QA dataset (`help_qa_dataset_anthropic.json`) plus targeted greps of the raw crawled docs for facts the QA dataset didn't happen to cover (e.g. large-file-size handling, per-resource license fields, embargo field semantics).
- Discovered mid-task that the source-label vocabulary itself needed to change (`GRAPH` → `RAG`, since the new domain's deployable backend is SQL-based, not graph-based) — that rename had to be threaded through `kb_routing_schema.json`, `evaluate_kb_routing.py`, `update_readme_stats.py`, and `README.md`, not just the dataset file. If you ever find the schema's enum doesn't match what's in your source material's terminology, that's a signal to check whether the labels themselves are out of date, the same way this one was.

## Reference files

- `benchmark/general-help/help_qa_dataset_*.json` — source facts (question, correct answer, `page_urls`, `context`)
- `benchmark/general-help/output_markdown/*.md` — raw crawled docs, for facts not in the QA dataset
- `benchmark/kb-routing/kb_routing_dataset.json` — the file this skill adds sessions to
- `benchmark/kb-routing/kb_routing_schema.json` — authoritative schema and current source-label enum (check this every time, don't assume)
- `benchmark/kb-routing/update_readme_stats.py` — regenerates the README's dataset composition table from the dataset file
- `benchmark/kb-routing/README.md` — session/turn field reference, `session_type`/`expected` semantics, Step 2 human-validation checklist
