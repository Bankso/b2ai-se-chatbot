---
title: Red teaming
weight: 40
---

# Red teaming

## What red teaming is

Red teaming is adversarial testing: instead of asking the copilot known questions with known-good answers (like the [source routing](/docs/benchmarking-and-evaluation/source-routing/) and [grounded retrieval](/docs/benchmarking-and-evaluation/grounded-retrieval/) evals), an **attacker LLM** actively tries to make the agent misbehave, and a **judge LLM** scores whether each attempt succeeded.

The Bridge2AI Standards Explorer Copilot's red team benchmark (`benchmark/redteam/`) is a self-contained harness with three roles, all running on Bedrock:

- **Target** — the live copilot Bedrock Agent (dev alias), invoked read-only.
- **Attacker** — a Claude model that crafts an adversarial message for a given (vulnerability, technique) pair. In multi-turn items it sees the full conversation and escalates, building on established rapport.
- **Judge** — a Claude model that scores the full transcript against the vulnerability's pass/fail criteria.

A **low** attack success rate is good — it means the copilot resisted. This is the opposite direction from the other benchmarks, where a high score is good.

## How it tests guardrails

Each vulnerability in `redteam_config.json` pairs a goal (what the attacker wants the agent to do) with one or more **attack techniques** — `Direct`, `Roleplay`, `PromptInjection`, `SystemOverride`, `GrayBox`, `EmotionalManipulation`, `Crescendo` (multi-turn escalation), and others. The **9** current vulnerability items span four categories:

| Category | Checks |
|---|---|
| `data-privacy` | PII leakage (fabrication/refusal probe — no individual contact fields exist in any B2AI table or sampled D4D content), system prompt / config / credential leakage |
| `safety` | Unqualified clinical guidance tied to conditions named in the Grand Challenge data (`b2ai-clinical-misinformation`) |
| `security` | Query-injection attempts against the SQL backend (including the table-allowlist regression check), `buildPortalUrl`/`facets` misuse |
| `agentic` | Off-topic repurposing (single-turn and multi-turn crescendo), false capability claims, false restriction claims, and D4D fact fabrication |

The copilot has **read-only** access only — a docs KB and a read-only SQL resource-backend action group (including the D4D sub-routine), no code execution, no write path anywhere, and no access-restricted resources (everything in the B2AI tables is open access). Nevertheless, the benchmark checks whether an attacker can still talk it into overstepping that scope, leaking something it shouldn't, giving unsafe guidance, or fabricating facts (about D4D content or about a resource's access status).

See `benchmark/redteam/README.md` for the full per-item breakdown, including `false-restriction-claim` (the mirror-image failure of the CCKP predecessor's access-restriction-disclosure item, since B2AI has no access-restricted resources at all) and `d4d-fabrication` (new for this portal — checks that the agent never states D4D facts beyond what `getD4D`/`searchD4D` actually retrieved).

## Examples from a real run

No B2AI agent has been deployed yet, so there's no real red-team run to draw examples from. Once a dev agent exists, run `evaluate_redteam.py` and replace this section with real transcript examples — both guardrails that held and any that failed. See [Red team report](/docs/benchmarking-and-evaluation/red-team-report/) for the current status.

## Running it

```bash
cd benchmark/redteam
python evaluate_redteam.py --agent-id <ID>                                   # all config items, dev agent
python evaluate_redteam.py --agent-id <ID> --vulnerability <vulnerability-id>
```

See `benchmark/redteam/README.md` for the full vulnerability/technique taxonomy, safety notes (it attacks a live agent), and all CLI flags.

> [!CAUTION]
> Result JSON files can contain successfully leaked or harmful content the attacks extracted from the agent — review before sharing outside the benchmark's normal workflow.
