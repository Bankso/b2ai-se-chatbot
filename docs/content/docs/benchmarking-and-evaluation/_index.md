---
title: Benchmarking and Evaluation
weight: 20
bookFlatSection: true
bookCollapseSection: false
bookIcon: bar-chart
---

# Benchmarking and evaluation

This is where the bulk of the effort goes. The core loop is:

{{% steps %}}
1. **Deploy** instruction or model changes to the dev stack, then prepare the agent so the DRAFT version picks them up:

   ```bash
   aws bedrock-agent prepare-agent --agent-id <ID>
   ```

2. **Run evals** to measure routing accuracy, answer quality, resource-search correctness, and adversarial resistance:

   ```bash
   cd benchmark/kb-routing
   python evaluate_kb_routing.py --agent-id <ID> --judge

   cd ../general-help
   python evaluate_bedrock_agent.py --agent-id <ID>

   cd ../resource-search
   python evaluate_resource_search.py --agent-id <ID>

   cd ../redteam
   python evaluate_redteam.py --agent-id <ID>
   ```

3. **Review** failures and over-queries to understand what went wrong — inspect the per-turn/per-item scores in the generated result JSON files.
4. **Revise** instructions, source selection rules, or the benchmark dataset.
5. **Repeat** until metrics stabilize.
{{% /steps %}}

## What to benchmark

Each knowledge source should have its own benchmark that tests whether the agent gives good answers from that source in isolation. If your agent only has a docs KB, you need a docs benchmark. If it also has a live resource backend (SQL, a knowledge graph, etc.), you need a benchmark for that too — and, crucially, one that checks the *correctness* of what it retrieved, not just whether it picked the right tool.

If you have multiple sources, you should also have a **source selection benchmark** — a dataset that tests whether the agent routes questions to the correct source. This is critical because the best instructions in the world won't help if the agent queries the wrong source. The source selection benchmark is how you validate and iterate on the routing rules in your system prompt.

You should also have an **adversarial benchmark** — the above benchmarks test whether the agent behaves well on ordinary questions; a [red teaming](/docs/benchmarking-and-evaluation/red-teaming/) benchmark tests whether it holds up against a user actively trying to make it misbehave (leak internal details, give unsafe guidance, exceed its intended scope).

The Bridge2AI Standards Explorer Copilot has (or will have, once deployed) four benchmarks:

- **Source selection eval** (`benchmark/kb-routing/`) — 33 multi-turn sessions (70 turns total), each labeled with the expected source per turn (docs, SQL resource backend, redirect, or none). Measures whether the agent consults the right source, and whether it over-queries by consulting unnecessary sources. See [Source routing](/docs/benchmarking-and-evaluation/source-routing/).
- **Docs KB eval** (`benchmark/general-help/`) — 390 multiple-choice questions generated from the Bridge2AI Standards Registry and `standards-schemas` documentation, scored by an LLM judge against known correct answers. See [Grounded retrieval](/docs/benchmarking-and-evaluation/grounded-retrieval/).
- **Resource-search correctness eval** (`benchmark/resource-search/`) — 42 items across 8 categories, grading whether the agent's *actual SQL/facet/D4D tool-call parameters* (decoded from the Bedrock trace) and reported answers are correct for good-faith questions — not just whether it picked the right source. See [Resource search correctness](/docs/benchmarking-and-evaluation/resource-search-correctness/).
- **Adversarial eval** (`benchmark/redteam/`) — 9 vulnerability items, each paired with one or more attack techniques; an attacker LLM probes the agent and a judge LLM scores whether each attack succeeded. See [Red teaming](/docs/benchmarking-and-evaluation/red-teaming/).

No B2AI agent has been deployed yet, so **none of the four datasets above has been run against a live agent**. Dataset generation/construction is done (all four are populated with B2AI-domain content, grounded in the real crawled docs and live Synapse queries), but Step 2/3 human validation is still pending for `general-help` and `kb-routing` — see each benchmark's README for status.

## Building your own benchmarks

For the **source selection dataset**, curate sessions that cover each source individually, mixed-source conversations where the user pivots between question types, compound questions that need multiple sources, and off-topic questions that should not trigger any lookup. Label each turn with the expected source. See `benchmark/kb-routing/kb_routing_dataset.json` for the format.

For **per-source datasets**, generate questions from your actual content. `benchmark/general-help/generate_dataset.py` can produce a synthetic multiple-choice dataset from crawled documentation. Human validation before use is strongly recommended — synthetic datasets always have quality issues that need manual review.

For a **resource-search correctness dataset**, hand-author items whose ground truth is computed live against your actual backend (see `benchmark/resource-search/build_ground_truth.py`), so a tool call can be graded on real SQL/facet/join correctness rather than just "a source was consulted." This catches bug classes source-routing alone can't — e.g. an AND/OR filter-combination mistake, or a string flag column (`'Yes'`/`'No'`) mistaken for a boolean.

For the **adversarial dataset**, define vulnerabilities (what the attacker is trying to make the agent do) and pair each with attack techniques (direct ask, roleplay, prompt injection, multi-turn escalation, etc.). See `benchmark/redteam/redteam_config.json` for the format and the [Red teaming](/docs/benchmarking-and-evaluation/red-teaming/) page for the full taxonomy.

## Running evals

See the READMEs in each `benchmark/` subdirectory for usage. All evals default to the `TSTALIASID` alias which points to the DRAFT version. If you've updated the agent without preparing it, run `aws bedrock-agent prepare-agent --agent-id <ID>` first.
