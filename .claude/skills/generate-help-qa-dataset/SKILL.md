---
name: generate-help-qa-dataset
description: Generate (or extend) the general-help benchmark's synthetic multiple-choice QA dataset — Step 2 of benchmark/general-help/README.md — from crawled docs markdown, matching generate_dataset.py's schema/batching exactly. Supports a Claude-native mode that needs no OpenAI/Anthropic API key (Claude itself generates the questions inline) as well as the original scripted OpenAI/Anthropic API path. Use when the user asks to generate/regenerate the help QA dataset, run "step 2" of the general-help benchmark, add questions for a new/updated doc page, or create synthetic benchmark questions from the crawled portal docs — including when they have no API key and want Claude Code to do the generation itself.
---

You turn crawled documentation markdown into a synthetic multiple-choice QA dataset (`help_qa_dataset_<provider>.json`) matching `qa_schema.json` exactly. This is Step 2 of the general-help benchmark pipeline — it sits between the docs crawl (Step 1) and human review (Step 3), so treat it as one link in that chain, not an isolated task.

There are two ways to produce it, both yielding an identical output format:

| Mode | How questions are generated | Needs an API key? |
|---|---|---|
| **Claude-native** (default) | You (Claude, in this session) generate each batch's questions directly, using the exact same prompt/schema/batching logic `generate_dataset.py` would use | No |
| **Scripted** | `generate_dataset.py` calls the OpenAI or Anthropic API directly | Yes (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) |

Default to **Claude-native** unless the user specifically asks for the scripted OpenAI/Anthropic API path (e.g. to reproduce a prior provider comparison, or because they want the literal `gpt-5.4`/`claude-sonnet-4-6` API-billed call rather than the Claude Code session model).

## Background

Read `benchmark/general-help/README.md` in full before running anything — it is the source of truth for flags, defaults, and the dataset schema. In short: pages in `output_markdown/` are batched (`BATCH_SIZE = 5` pages/batch, `QUESTIONS_PER_BATCH = 6` questions/batch), a prompt instructing a mix of single-page and cross-page questions (≥70% `CONTRIBUTOR`/`REUSER` personas) is sent to a model with forced structured output, and each returned question is assigned a UUID (never by the model itself).

`generate_dataset.py`'s helper functions (`get_page_batches`, `load_batch`, `build_prompts`, `find_page_file`, `assign_ids`) are pure — no network call, no API key — and only `generate_with_openai`/`generate_with_anthropic` actually hit a paid API. Claude-native mode reuses all the pure helpers (by importing them) so batching, prompt wording, and ID assignment are byte-identical to the scripted path; it substitutes Claude itself for the one function that requires a key.

## Protocol

1. **Locate the working directory.** Find the repo root with `git rev-parse --show-toplevel` if you aren't already there, then `cd <root>/benchmark/general-help`. Don't hardcode an absolute path; this skill must work from any clone.

2. **Verify Step 1 is done.** Check that `output_markdown/` exists and contains `.md` files (`ls output_markdown/*.md | wc -l`). If it's empty or missing, stop and tell the user to run the crawl spiders listed in the README's Step 1 first (currently `scrapy runspider b2ai_registry_docs_spider.py` and `scrapy runspider b2ai_schemas_docs_spider.py`) — don't attempt to crawl yourself as part of this skill.

3. **Check dependencies.** `generate_dataset.py` imports `openai`, `anthropic`, and `tiktoken` at module level — even Claude-native mode needs these importable, since it imports the script's pure helper functions:
   ```bash
   python3 -c "import openai, anthropic, tiktoken" 2>&1 || python3 -m pip install openai anthropic tiktoken
   ```
   Use `python3 -m pip`, not bare `pip` — some environments don't have a `pip` binary on `PATH` even though the `python3` module works fine.

4. **Pick a mode**, and scope the run:
   - Full regeneration (all pages, optionally capped with `--max-batches N`-equivalent for a cheap test pass) vs. focused single-page generation (append-only, matching a URL/filename substring).
   - If the user hasn't said, and no `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` is set in the environment, proceed with **Claude-native mode** without asking for a key. If both a key is available and the user hasn't specified, ask which mode they want.

### Mode A: Claude-native (no API key)

You play the role of `generate_fn` yourself — everything else is the script's own code, invoked via short inline Python so batching/prompts/schema/ID-assignment can never drift from what the scripted path would produce.

a. **List batches** (mirrors `get_page_batches`):
   ```bash
   python3 -c "
   from generate_dataset import get_page_batches
   batches = get_page_batches('output_markdown')
   for i, b in enumerate(batches): print(i, b)
   "
   ```
   For a focused single-page run, use `find_page_file` instead to resolve the one matching filename and treat it as a one-file batch.

b. **For each batch**, print its exact prompt (mirrors `build_prompts` — do not paraphrase or reconstruct this yourself, always print it fresh so it can't drift from the script):
   ```bash
   python3 -c "
   import json
   from generate_dataset import load_batch, build_prompts, QUESTIONS_PER_BATCH
   schema = json.load(open('qa_schema.json'))
   batch_docs = load_batch('output_markdown', ['<file1>', '<file2>', ...])
   system_content, user_content = build_prompts(batch_docs, schema, QUESTIONS_PER_BATCH)
   print(system_content); print('---USER---'); print(user_content)
   "
   ```

c. **Generate that batch's questions yourself**, following the printed system/user content exactly as if you were the model receiving it: produce a JSON array of `QUESTIONS_PER_BATCH` question objects (`question`, `mc1_targets.choices`, `mc1_targets.labels`, `persona`, `page_urls`, `context` — no `id` yet, that's assigned next). Ground every `context` snippet in the actual batch text you just read; don't invent facts not present in the source pages. Write the array to a scratch file, e.g. `batch_<i>_raw.json`.

d. **Assign IDs per batch** (mirrors `assign_ids`), writing each batch's finished questions to its own scratch file so nothing is held only in conversation state:
   ```bash
   python3 -c "
   import json
   from generate_dataset import assign_ids
   raw = json.load(open('batch_<i>_raw.json'))
   json.dump(assign_ids(raw), open('batch_<i>_ids.json', 'w'))
   "
   ```

e. **Combine all batches and write the final file**, matching the script's own read-existing/append/write behavior (`help_qa_dataset_anthropic.json` — Claude-native output is filed under the `anthropic` provider name since Claude is an Anthropic model and this keeps it a drop-in default for `evaluate_bedrock_agent.py --dataset`):
   ```bash
   python3 -c "
   import json, glob, os
   new_questions = []
   for f in sorted(glob.glob('batch_*_ids.json'), key=lambda p: int(p.split('_')[1])):
       new_questions.extend(json.load(open(f)))

   out = 'help_qa_dataset_anthropic.json'
   append_run = False  # set True only for a focused/single-page run
   if append_run and os.path.exists(out):
       all_q = json.load(open(out)) + new_questions
   else:
       all_q = new_questions
   json.dump(all_q, open(out, 'w'), indent=2)
   print(len(all_q))
   "
   ```
   For a **full regeneration**, `append_run` stays `False` (overwrite, same as the scripted path's full-run behavior). For a **focused single-page run**, set it `True` so existing questions are preserved.

f. Clean up scratch files (`batch_*_raw.json`, `batch_*_ids.json`) once the final dataset file is written and verified.

### Mode B: Scripted (requires API key)

1. **Check the API key** for the chosen provider is set (`$OPENAI_API_KEY` or `$ANTHROPIC_API_KEY`). If missing, ask the user for it rather than guessing or running unauthenticated.

2. **Run generation** from `benchmark/general-help/`:
   ```bash
   # Full run (all pages)
   ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 generate_dataset.py --provider anthropic

   # Full run, capped for a test pass
   ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 generate_dataset.py --provider anthropic --max-batches 2

   # Focused single-page run (appends to existing dataset)
   ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 generate_dataset.py --provider anthropic --page "uploading-data"
   ```
   Swap in `OPENAI_API_KEY` / `--provider openai` for the OpenAI path. This is a real paid API call per batch — for a full run across all crawled pages that's one call per 5 pages, so confirm with the user before a full un-capped run if cost is a concern.

### Both modes: verify and hand off

5. **Verify the output matches spec** — don't just trust exit code 0 or your own generation:
   - `help_qa_dataset_<provider>.json` exists (created fresh, or grew in count for focused/appended runs).
   - It's a JSON array where every entry validates against `qa_schema.json`: has `question`, `mc1_targets.choices` (4–5 strings), `mc1_targets.labels` (ints, exactly one `1`), `persona` (one of `CONTRIBUTOR`/`REUSER`/`FUNDER`/`PATIENT`/`X`), `page_urls`, `context`, and a UUID `id`. A quick pass:
     ```bash
     python3 -c "
     import json
     qs = json.load(open('help_qa_dataset_anthropic.json'))
     for q in qs:
         assert q['mc1_targets']['labels'].count(1) == 1, q['id']
         assert len(q['mc1_targets']['choices']) == len(q['mc1_targets']['labels'])
         assert q['persona'] in ('CONTRIBUTOR','REUSER','FUNDER','PATIENT','X'), q['id']
     print('OK', len(qs), 'questions')
     "
     ```
   - Spot-check persona mix: CONTRIBUTOR + REUSER should be roughly ≥70% of new questions — flag it if a run looks badly skewed, but don't silently "fix" the dataset by hand.
   - For focused/appended runs, confirm the new questions were *appended* (existing question count preserved, not replaced).

6. **Report and hand off to Step 3.** Summarize: mode used, page/batch count, number of questions generated (and running total if appended), output file path, and any anomalies from step 5. Point the user at `benchmark/general-help/README.md`'s Step 3 (human validation via `reviewer_notes.yml`) as the natural next step — don't perform that review yourself.

7. **Do not commit** the generated dataset file unless the user asks — surface it for their review first, consistent with how this benchmark's other data artifacts are handled.

## Reference files

- `benchmark/general-help/generate_dataset.py` — batching, prompt-building, ID assignment, and (for Mode B) provider API clients
- `benchmark/general-help/qa_schema.json` — authoritative schema every generated question must satisfy
- `benchmark/general-help/README.md` — full pipeline doc (Steps 1–4); Step 2 section documents providers, batching strategy, and dataset field reference
- `benchmark/general-help/output_markdown/` — crawled doc pages consumed as input (git-ignored, produced by Step 1)
