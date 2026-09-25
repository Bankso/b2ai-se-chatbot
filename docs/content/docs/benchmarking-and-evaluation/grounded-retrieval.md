---
title: Grounded retrieval
weight: 20
---

# Grounded retrieval

## What this tests

Grounded retrieval evaluates a single knowledge source in isolation: given a question with a known correct answer, does the agent retrieve the right document and produce an answer consistent with it? This is the baseline eval every portal copilot needs — any agent with at least one docs knowledge base should have one of these, even before adding [source routing](/docs/benchmarking-and-evaluation/source-routing/) for multi-source setups.

The Bridge2AI Standards Explorer Copilot's version of this is the **general-help benchmark** (`benchmark/general-help/`), which evaluates the docs KB built from the [Bridge2AI Standards Registry docs](https://bridge2ai.github.io/b2ai-standards-registry/) and the [LinkML `standards-schemas` docs](https://bridge2ai.github.io/standards-schemas/).

## How the dataset is built

Questions are generated synthetically from the live docs, then validated by human reviewers before use:

1. **Crawl** — two Scrapy spiders (`b2ai_registry_docs_spider.py`, `b2ai_schemas_docs_spider.py`) convert each docs page into Markdown.
2. **Generate** — an LLM (OpenAI, Anthropic, or Claude-native via the `generate-help-qa-dataset` skill) reads the crawled pages and produces multiple-choice questions, each with 4-5 answer choices, one correct label, a target persona (`CONTRIBUTOR`, `REUSER`, `FUNDER`, `PATIENT`, or `X`), the source page URL(s), and a `context` snippet grounding the correct answer.
3. **Validate** — human reviewers check coherence, answer specificity, and whether the question is scoped appropriately for its persona.

A real dataset entry (`benchmark/general-help/help_qa_dataset_anthropic.json`):

```json
{
  "question": "Does using the Bridge2AI Standards Explorer MCP Server require an API key?",
  "mc1_targets": {
    "choices": [
      "No, no API key is required to use the server",
      "Yes, a Synapse Personal Access Token is always required",
      "Yes, an OpenAI API key is required",
      "An API key is only required when using the query_table tool",
      "Not covered in documentation"
    ],
    "labels": [1, 0, 0, 0, 0]
  },
  "persona": "REUSER",
  "page_urls": ["https://bridge2ai.github.io/b2ai-standards-registry/ai-access/"],
  "context": "The Bridge2AI Standards Explorer can be accessed by AI agents and Large Language Models (LLMs) through the Standards Explorer MCP... No API key is required."
}
```

Although the dataset is multiple-choice, evaluation runs in **free-response** format — the agent is asked the plain question, not shown the answer choices — to better reflect how a real user interacts with the copilot. An LLM judge then scores the free-text response against the known correct answer.

## Current dataset

`help_qa_dataset_anthropic.json` has **390 questions**, generated 2026-09-25 (Claude-native mode) from a 323-page crawl of both B2AI docs sources. Human review (Step 3) is still pending — see `benchmark/general-help/README.md` for known open items (e.g. one question that expands `ncit` to "National Cancer Institute Thesaurus," a fact not present on its source page, flagged for reviewer attention).

## Examples from a real run

No B2AI agent has been deployed yet, so there's no real eval run to draw examples from. Once a dev agent exists and the dataset has been through human review, run `evaluate_bedrock_agent.py` and replace this section with real results — including at least one example of a **grounded and correct** answer and one **honest gap** (the agent saying "I don't have this" rather than guessing), which is the failure mode worth normalizing rather than penalizing away.

## Running it

```bash
cd benchmark/general-help
python evaluate_bedrock_agent.py --agent-id <ID>
```

See `benchmark/general-help/README.md` for dataset generation (`generate_dataset.py`), the full scoring rubric, all CLI flags, and metrics reported (overall accuracy, per-persona accuracy, cross-page vs single-page accuracy, source attribution rate).
