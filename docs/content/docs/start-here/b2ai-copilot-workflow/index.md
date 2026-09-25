---
title: Reference workflow
weight: 20
---

# Reference workflow

The Bridge2AI Standards Explorer Copilot combines a documentation knowledge base (the Bridge2AI Standards Registry docs and the LinkML `standards-schemas` docs) and a resource-query backend (SQL over Synapse denormalized tables, including a D4D exploration sub-routine — see [Templates](/docs/start-here/templates/)) behind a Bedrock agent, then iterates through evaluation before promoting changes from dev to prod. There is **one backend variant** — SQL only, no SPARQL/knowledge-graph option — deployed and evaluated via CI (`deploy-copilot-sql.yml`).

![Diagram showing the Bridge2AI Standards Explorer Copilot workflow: knowledge sources (a docs KB and the SQL resource backend, including D4D) feed into a CloudFormation-managed agent stack (instructions, Lambda adapter, agent, alias); the agent alias is evaluated, results decide whether to revise or promote; ready changes flow through deploy-copilot-sql.yml's dev and prod stacks, ending in an update to the Synapse agent registration.](./diagrams/b2ai-copilot-workflow.svg)
