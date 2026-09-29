# Bridge2AI Standards Explorer Copilot

Development and configuration for the **Bridge2AI Standards Explorer Copilot**, a chatbot for the [Bridge2AI Standards Explorer](https://b2ai.standards.synapse.org/).

Forked from [mc2-center/cckp-chatbot](https://github.com/mc2-center/cckp-chatbot) (the CCKP Copilot, for the Cancer Complexity Knowledge Portal), which was itself forked from [nf-osi/portal-chatbot](https://github.com/nf-osi/portal-chatbot) (the NF Portal Copilot). This repo was retargeted from the CCKP to the Bridge2AI Standards Explorer on branch `b2ai-conversion`; see [`plans/retarget-b2ai-standards-explorer.md`](plans/retarget-b2ai-standards-explorer.md) for the full retarget plan and rationale.

## Description

This repository contains configuration, test datasets, and other resources for a Synapse-hosted chatbot tailored to the Bridge2AI Standards Explorer — an AWS Bedrock Agent that answers questions from the portal's documentation and queries its live Synapse-backed data (standards, datasets, organizations, data topics/substrates, and the Grand Challenge D4Ds).

## Repository layout

- **[`agents/`](agents/)** — the copilot's agent configuration: CloudFormation template(s), Lambda source and tests, Synapse agent registrations, deployment docs, and a changelog. See [`agents/README.md`](agents/README.md) for the copilot stack, capabilities, deploy instructions, and open items.
- **[`benchmark/`](benchmark/)** — four benchmarking/evaluation datasets (the PHD suite; see below) that exercise the deployed copilot: `general-help`, `kb-routing`, `redteam`, and `resource-search`.
- **[`docs/`](docs/)** — guides and architecture notes, built with [Hugo](https://gohugo.io) and the [hugo-book](https://themes.gohugo.io/themes/hugo-book/) theme; see [`docs/README.md`](docs/README.md) for local dev setup. Deploys to GitHub Pages automatically via [`.github/workflows/deploy-docs.yml`](.github/workflows/deploy-docs.yml) on changes to `docs/`. The docs site's URL is `https://bankso.github.io/b2ai-se-chatbot/` (derived from the repo's `origin` remote, `github.com/Bankso/b2ai-se-chatbot`); as of this writing it has not been confirmed live, so treat that URL as not-yet-deployed until verified.

## Agent Registrations

For details on the deployed copilot stack (SQL over pinned Synapse tables — the Bridge2AI Standards Explorer has no knowledge-graph/SPARQL backend), see [agents/README.md](agents/README.md).
(Note: Creating agents for Synapse is not generally available to all Synapse users.
Nevertheless, if you came across this project and have interest and funding for Synapse agents and portals, feel free to reach out to us.)

No Bridge2AI Standards Explorer Copilot has been deployed yet — see [agents/README.md](agents/README.md)'s "Open items" for what's needed.

## PHD Benchmarking and Evaluation

The Portal Help & Discovery (PHD) suite is a set of benchmarking and evaluation datasets to ensure quality standards and quantify improvements in our chatbot agents.
Within our framework, these resources also help identify documentation gaps and inconsistencies.
Not all datasets are stored here in this repo; references to relevant datasets will be kept up to date.

### General Help Component

The General Help component tests the agent's ability to answer questions about Bridge2AI Standards Explorer navigation, features, and general usage.
The agent answers questions based on the Bridge2AI Standards Registry docs and the standards-schemas (LinkML data-model) docs.

- [Test questions about the Bridge2AI Standards Explorer](benchmark/general-help/) — synthetically generated from a crawl of `bridge2ai.github.io/b2ai-standards-registry` and `bridge2ai.github.io/standards-schemas`; see the benchmark's README for crawl/generation status and pending human review.

### Discovery Component

There will eventually be multiple implementations falling under the Discovery component.
Some of the below may arguably focus more on search; the distinction sometimes depends on user phrasing.
(Note: Search is more goal-oriented with targeted results. Discovery is for browsing, recommendations, and understanding what's available. Discovery is harder to evaluate.)

- [KB routing](benchmark/kb-routing/) — whether the agent selects the correct knowledge source (docs KB vs. the SQL resource backend) per query.
- [Resource search correctness](benchmark/resource-search/) — whether the agent's actual SQL/facet/D4D tool calls and answers are correct for good-faith resource-discovery questions, not just whether it picked the right source.
- [Redteam](benchmark/redteam/) — adversarial security/safety testing against a live dev agent.

## Contributing

We welcome contributions to improve the chatbot. To contribute:

1. Fork the repository.
2. Create a new branch for your feature or bugfix.
    ```sh
    git checkout -b feature-name
    ```
3. Commit your changes.
    ```sh
    git commit -m 'Describe your feature or fix'
    ```
4. Push to the branch.
    ```sh
    git push origin feature-name
    ```
5. Create a pull request.

## Acknowledgements

This repository was forked from the [CCKP Copilot](https://github.com/mc2-center/cckp-chatbot), maintained by Sage Bionetworks' MC2 Center, and retargeted from the Cancer Complexity Knowledge Portal to the Bridge2AI Standards Explorer. The CCKP Copilot was itself forked from the NF Portal Copilot, built by NF-OSI with funding from the [Gilbert Family Foundation](https://gilbertfamilyfoundation.org/).

## See Also

- https://rest-docs.synapse.org/rest/index.html#org.sagebionetworks.repo.web.controller.AgentController
- [Synapse Custom Agents framework](https://sagebionetworks.jira.com/wiki/spaces/PLFM/pages/3711303683/Adding+Custom+Agents+to+Synapse) (Internal Confluence page)
- [Bridge2AI Standards Registry](https://github.com/bridge2ai/b2ai-standards-registry) — the source repo behind the Standards Registry docs site
- [Bridge2AI standards-schemas](https://github.com/bridge2ai/standards-schemas) — the LinkML schema behind the standards-schemas docs site

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
