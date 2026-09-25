---
title: Red team report
weight: 50
---

# Bridge2AI Standards Explorer Copilot Red Team — Latest Report

## Status

No red team evaluation has been run against a Bridge2AI Standards Explorer Copilot agent yet — no such agent has been deployed. This page is a placeholder, structured to match what the `redteam-eval` skill (`.claude/skills/redteam-eval/SKILL.md`) fills in after a real run: Background, Methodology, Results, and Discussion sections below are stubs, not findings.

This report previously described CCKP Copilot and NF Portal Copilot runs from this repo's earlier configurations. That content described those portals' agents specifically and has been removed rather than carried over as if it applied to the Bridge2AI Standards Explorer.

## Background

The Bridge2AI Standards Explorer Copilot's redteam benchmark (`benchmark/redteam/`) is a self-contained harness — attacker LLM vs. target agent vs. judge LLM, all on Bedrock — covering **9 vulnerability items** across 4 categories (`data-privacy`, `safety`, `security`, `agentic`). See [Red teaming](/docs/benchmarking-and-evaluation/red-teaming/) for the full methodology this report will follow once real runs exist, and `benchmark/redteam/README.md` for the complete vulnerability/technique taxonomy.

## Methodology

_To be filled in after the first run: attacker/judge model pairing(s), number of runs, table of runs (timestamp, attacker model, judge model, n cases)._

## Results

_To be filled in after the first run: headline aggregate attack success rate (mean ± std across runs), per-vulnerability / per-category / per-technique breakdown, and full detail on every distinct successful attack found._

## Discussion

_To be filled in after the first run: risk summary, mitigations, limitations, and proposed follow-ups._

## Next steps

1. Deploy a Bridge2AI Standards Explorer Copilot dev agent (see [Deployment](/docs/start-here/deployment/)).
2. Run `benchmark/redteam/evaluate_redteam.py` against the dev agent (see the `redteam-eval` skill for the full protocol, including the `AWS_BEARER_TOKEN_BEDROCK` environment gotcha).
3. Aggregate with `aggregate_redteam.py`, review the successful-attack transcripts, and replace this page with the real methodology, results tables, and discussion — following the structure above as a template for depth and rigor, not for content.
