# Changelog

## cckp-copilot

The CCKP Copilot's full release history (forked from `nf-osi/portal-chatbot`, two backend variants, dataset/file discovery, registrations #335/#336, etc.) predates this repo's retarget to the Bridge2AI Standards Explorer and is not carried over here. See `git log 8684452^` and earlier for that history, or `git show 8684452:agents/CHANGELOG.md` for the CCKP-era changelog file as it stood at the point of the retarget.

## b2ai-copilot

### Unreleased

- Retargeted this repo from the Cancer Complexity Knowledge Portal (CCKP) to the **Bridge2AI Standards Explorer** (https://b2ai.standards.synapse.org/), per `plans/retarget-b2ai-standards-explorer.md`. Agent renamed `agents/cckp-copilot/` → `agents/b2ai-copilot/`; user-facing name is **Bridge2AI Standards Explorer Copilot**.
- **Backend: SQL-only.** Removed the SPARQL/knowledge-graph variant entirely (`cloudformation.sparql.yaml`, `lambda/cckpGraphRag/`) — the Bridge2AI Standards Explorer has no knowledge-graph backend, only Synapse tables. Also dropped the CCKP-era Synapse Dataset/file/access-restriction operations (`getDatasetFiles`, `getFileDetails`, `checkRestriction`) — every B2AI table is fully open access, so there's nothing to check.
- **New `TABLES`**, pinned to the exact synId.version the live portal itself queries: `standards` (`DST_denormalized`), `datasets`, `organizations`, `topics`, `substrates`, `manifest`, and a new `d4d` table (`D4D_content`). Added `scripts/check_table_pins.py` to drift-check these pins against the portal frontend's `resources.ts` on demand.
- **Security fix:** found and closed a real hole where Synapse executes whatever table a query's `FROM` clause names, regardless of the request path's entity id — SQL text is now checked against a table allowlist in addition to the `table` argument (`_check_sql_tables`, commit `22de2a2`). `query-injection` in `benchmark/redteam/redteam_config.json` now regression-tests this.
- **D4D (Datasheets for Datasets) exploration sub-routine** (new capability, not present in the CCKP Copilot): `listD4Ds`, `getD4D` (outline or a paged section), and `searchD4D` (keyword search across one or all D4Ds) over the 4 Bridge2AI Grand Challenges' Datasheets-for-Datasets HTML content, so a user can explore a 38–60 KB document conversationally instead of receiving it whole.
- **`buildPortalUrl` rewritten** for the B2AI portal's actual routes (Standard/Organization/DataTopic Detail Pages, `/Search/Standards`) and extended with an optional `facets` param (12-column allowlist), encoded as the portal's `qw0` gzip+base64 scheme with a self-verify decode-and-compare roundtrip before returning. Facet/search-term combination semantics (facets AND across columns, OR within one column, a search term ANDs against combined facets, but a multi-word `SEARCH_TERM` alone ORs/unions) were verified independently via source trace, direct Synapse API replication, and live browser counts, and are documented in the function's docstring and the agent's Instruction. A later fix (`e0947fa`) made the `qw0` encoding deterministic by pinning gzip's `mtime`.
- Prompt (Instruction) rewritten for the Bridge2AI Standards Explorer's entities and the D4D sub-routine.
- Docs KB re-crawled from two B2AI sources (Bridge2AI Standards Registry docs and standards-schemas LinkML docs) with new spiders, replacing the CCKP help site + MC2 Center data model spiders.
- All four PHD benchmarks (`general-help`, `kb-routing`, `redteam`, and the new `resource-search`) retargeted or rebuilt for the Bridge2AI Standards Explorer's real entities, backend, and D4D sub-routine; see each benchmark's own README for status and coverage.
- Synapse registrations reset to empty (see `agents/README.md`) — no B2AI stack has been deployed yet. Placeholders introduced: `REPLACE_ME_B2AI_KB_ID`, `REPLACE_ME_B2AI_S3_BUCKET`, `REPLACE_ME_B2AI_EVAL_RESULTS_PROJECT`, `REPLACE_ME_B2AI_PROD_AGENT_ID`.
- Open: KB ID, S3 bucket, first deploy, and the registration-236 cutover — see `agents/README.md`'s "Open items".
