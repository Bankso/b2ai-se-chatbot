# Implement the SPARQL backend as a real, deployable CCKP Copilot variant

## Context

`cloudformation.sparql.yaml` has existed since the NF-OSI fork as a speculative
"not deployable yet" template — no hosted CCKP SPARQL endpoint existed, so its
Instruction, example queries, ontology assumptions, and the `cckpGraphRag`
Lambda's wire protocol were never verified against real data, and it never
received the hardening passes `cloudformation.sql.yaml` (the active backend)
has accumulated since (guide framing, scope-lock, disclosure rules, the
corrected redirect/`qw0` mechanism, two-source docs KB, etc.).

The user has a live endpoint:
`https://vyar2xyj0k.execute-api.us-east-1.amazonaws.com/prod/query`, and
authorizes against it with their own Synapse Personal Access Token
(`Authorization: Bearer <PAT>`) — the endpoint is configured to accept Synapse
PATs directly.

**A round of authenticated, read-only smoke testing (SELECT-only SPARQL, no
mutations) turned up two findings that invalidate load-bearing assumptions in
the old template and in `cckpGraphRag/lambda_function.py`:**

1. **The wire protocol is not what `cckpGraphRag` (inherited unchanged from
   NF-OSI's `nfGraphRag`) implements.** NF's endpoint is a synchronous
   Oxigraph/Fuseki-style POST (form-urlencoded `query`+`action=tsv_export` →
   raw TSV body). This endpoint is **async and JSON-based**, fronted by API
   Gateway + a job-queue Lambda:
   - `POST /prod/query` with a **JSON** body `{"query": "<sparql>"}` and
     `Authorization: Bearer <token>` returns `202` with
     `{"job_id": "...", "status": "pending"}`.
   - `GET /prod/query/{job_id}` (same auth header) polls the job; observed
     jobs completed within ~1-2s for modest queries. A finished job returns
     `{"job_id", "status": "complete", "results": "<JSON string>"}` where
     `results` is itself JSON-encoded **standard SPARQL 1.1 Query Results
     JSON** (`{"head": {"vars": [...]}, "results": {"bindings": [{"var":
     {"type", "value", ...}}]}}`) — not TSV, and double-encoded (a JSON string
     containing JSON) so it needs two `json.loads` calls.
   - No `failed`/`error` status was observed in testing; the Lambda rewrite
     must still handle an unknown/non-`complete` status defensively after its
     poll deadline is reached.
   - This means **`cckpGraphRag/lambda_function.py`'s `sparql_request` needs a
     full rewrite** (POST → poll → parse), not just an added auth header. The
     `x-api-key` header support planned earlier turned out to be unnecessary
     — `Authorization: Bearer <PAT>` alone works end-to-end; dropping that
     part of the original plan.

2. **This is not a CCKP-only graph — it's a shared triple store across
   multiple Sage-affiliated portals.** An unscoped `countByType`-style query
   (`SELECT ?type (COUNT(?s) AS ?count) WHERE { ?s a ?type }`) returned NF-OSI
   terms (`http://nf-osi.github.com/terms#File`: 550,344 instances,
   `#Specimen`, `#Individual`, `#Donor`, etc.), ALS Knowledge Portal terms
   (`https://alskp.synapse.org/terms#...`), generic `biolink`/`schema.org`
   vocab, and — mixed in among all of that — the real CCKP classes under
   `https://w3id.org/mc2-center/cckp-portal/`: `Publication` (4,773),
   `Dataset` (1,130), `Tool` (331), `Grant` (159), `EducationalResource` (10).
   A follow-up query (`?s a cckp:Dataset ; ?p ?o`) confirmed rich, real
   `cckp:` properties that line up well with the SQL variant's known columns:
   `datasetId`, `datasetName`, `tumorType`(+`Term`), `assay`(+`Term`),
   `tissue`(+`Term`), `species`(+`Term`), `grantNumber`, `pubMedId`, `doi`,
   `downloadSynId`, `sourceRepository`, `accessType`, `conditionsOfAccess`,
   `individualCount`, `specimenCount`, plus several `schema.org` predicates
   layered on top (`creator`, `measurementTechnique`, `license`, `funder`,
   `keywords`, `citation`).

   **Consequence:** every query this backend runs must type-anchor to a
   `cckp:` class (`?x a cckp:Dataset`, etc.) — the existing speculative
   example queries already did this by luck/convention, but it turns out to
   be a hard correctness requirement, not a style choice, since an untyped or
   wrongly-scoped query silently pulls in NF-OSI/ALSKP/other-portal data.
   Two of the four graph Lambda functions as currently written are actively
   broken against this real graph and must be rewritten, not just relabeled:
   - `count_by_type()` currently runs the unscoped `?s a ?type` query above —
     must become 5 explicit `cckp:`-scoped counts (one per known class, via
     `UNION` or 5 `COUNT`s), matching what "CCKP inventory" actually means.
   - `get_schema()` currently returns every `owl:Class`/`ObjectProperty`/
     `DatatypeProperty` graph-wide (5,798 classes, 817 datatype properties
     just in the top-100 sample) — must add a
     `FILTER(STRSTARTS(STR(?term), "https://w3id.org/mc2-center/cckp-portal/"))`
     to scope it to CCKP's own terms.
   - `get_shape()` already scopes via `sh:targetClass cckp:{class}`, so it's
     probably fine as-is, but should be spot-checked (`sh:NodeShape` count 32
     did appear in the type inventory, so SHACL shapes do exist in this
     graph) rather than assumed.
   - The Instruction needs an explicit, prominent rule (not just implicit
     convention in example queries): every `sparqlQuery` call must type-anchor
     to a `cckp:` class, because this is a shared multi-portal graph.

3. **Response shape decision (my recommendation, flagged for review rather
   than decided silently):** since the real endpoint hands back structured
   SPARQL-JSON bindings (not TSV), switch the Lambda's return shape from the
   current `{"resultTsv": "..."}` to a parsed `{"headers": [...], "rows":
   [...], "count": N}` shape — mirroring the SQL Lambda's existing
   `QueryResultResponse` convention (`cloudformation.sql.yaml`'s
   `QueryResultResponse` schema) — instead of re-serializing JSON bindings
   into TSV text just to match the old contract. Cleaner and more consistent
   with the rest of the repo; can keep `resultTsv` instead if preferred.

**A vendor doc ("Querying SageBrain", the team's official reference) surfaced
two more findings after the above was drafted — one is a correctness gap in
the plan as written, the other simplifies a design decision already made:**

4. **`cckp:` type-anchoring alone is not sufficient — every query also needs
   named-graph version scoping, or it silently double-counts/goes stale.**
   SageBrain is append-only: every portal publication loads into its own
   dated named graph (`urn:sagebrain:{program}:{YYYY-MM-DD}`), and **nothing
   is ever removed automatically**. A query against the default graph (no
   `GRAPH`/`FROM`) merges *every* snapshot ever loaded for *every* portal at
   once. Finding #2 above (type-anchor to `cckp:`) fixes the
   NF-OSI/ALSKP-bleed problem, but does nothing about **CCKP's own history**:
   if CCKP is ever re-published as a second dated snapshot (exactly the
   pattern NF and ALS already follow), an unscoped-but-type-anchored query
   like `?x a cckp:Dataset` will start returning duplicate/stale rows from
   both snapshots merged together, and any `count_by_type` or aggregate
   result becomes silently wrong in the same way the doc warns about for the
   other portals. There is no query-layer auto-resolution yet (the doc
   describes that as a proposed, unimplemented future feature) — callers are
   currently responsible for scoping by hand.
   - **Unresolved unknown, must confirm during implementation**: whether CCKP
     is currently loaded as a single unversioned graph or already follows the
     `urn:sagebrain:cckp:{date}` dated-snapshot convention. Check with
     `SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY DESC(?g)`
     and look for `cckp` entries before finalizing the scoping approach.
   - **Design implication for `lambda_function.py`**: add a small
     graph-resolution step — enumerate named graphs, pick the
     lexicographically-latest one matching the CCKP prefix — and wrap every
     query (`sparqlQuery`, `count_by_type`, `get_schema`, `get_shape`) in a
     `GRAPH <resolved-uri> { ... }` clause instead of relying on `cckp:`
     type-anchoring alone. This costs an extra async submit/poll round-trip
     per agent tool call unless the resolved graph URI is cached (e.g., per
     Lambda cold start, with a short TTL) — worth deciding explicitly rather
     than adding it ad hoc.
   - **Residual risk, document rather than solve**: the doc's official
     protocol also checks a DynamoDB ingestion-tracking table
     (`app-dev-neptune-pipeline-loads`, in the `sagebrain-prod` AWS account)
     to confirm a snapshot's load actually reached `status: complete` before
     trusting it — a partially-failed load can leave a graph with partial
     triples but no in-graph signal of that. `LambdaExecutionRole` in
     `cloudformation.sparql.yaml` only has `AWSLambdaBasicExecutionRole` and
     no cross-account access to that table (it lives in SageBrain's account,
     not CCKP's), so this Lambda cannot replicate that check. Accept this as
     a known limitation and say so plainly in the Instruction/README rather
     than silently pretending the graph-enumeration step is equivalent to
     the doc's full protocol.

5. **Auth: reuse the existing Synapse-token wiring instead of minting a new
   secret.** The doc confirms SageBrain's authorizer takes a Synapse PAT (or
   OAuth token) as `Authorization: Bearer <token>`, resolves it to a Synapse
   user ID, and requires that user be a member of **Sage Brain Team
   (Team:3605470)** — team membership is the actual gate, not anything about
   the PAT's own scopes. The CCKP Copilot is deployed through the CCKP and
   already has exactly this kind of token available in its environment: the
   SQL variant's `cckpSqlRag` Lambda already takes a `SynapseAuthToken`
   CFN parameter (`NoEcho`) wired to a `SYNAPSE_AUTH_TOKEN` env var and sends
   it as `Authorization: Bearer {SYNAPSE_AUTH_TOKEN}` for its own Synapse API
   calls (`cloudformation.sql.yaml:123-196`,
   `lambda/cckpSqlRag/lambda_function.py:16,234`) — functionally the same
   credential shape SageBrain's authorizer expects.
   - **Changes §2/§3 below**: drop the plan's earlier framing of
     `SparqlAuthToken`/`CCKP_SPARQL_AUTH_TOKEN` as a *brand-new* secret
     requiring its own provisioning pipeline. Instead, wire the *same*
     Synapse-token parameter/secret the SQL stack already uses into the
     GraphRag Lambda's `SPARQL_AUTH_TOKEN` env var (same value, same
     `NoEcho` parameter pattern — can even be the literal same CFN parameter
     if the two Lambdas are deployed together, since the hybrid design
     already shares one stack).
   - **Still needs a one-time human step, not a code change**: whichever
     Synapse identity that token belongs to must actually be a member of
     Sage Brain Team (Team:3605470), or every SageBrain call 401s regardless
     of how the token is wired in. Worth a line in `agents/README.md`'s open
     items rather than assuming it's already true.
   - The plan's existing recommendation to use a dedicated, minimally-scoped
     PAT for the deployed agent (rather than the personal PAT used for this
     session's manual testing) still stands as good practice — this finding
     only changes *how the token reaches the Lambda* (reuse existing
     plumbing), not whether it should be a separate, purpose-specific PAT.

6. **Minor, low-cost additions the doc calls out:**
   - Send `X-Source: cckp-copilot` on every request — the doc says this
     labels the caller in SageBrain's audit log (which records full query
     text, caller IP, duration, and status per request); free to add and
     makes this Lambda's traffic distinguishable from other SageBrain
     callers after the fact.
   - The doc's execution ceiling (60s in the SageBrain worker) is *longer*
     than this Lambda's own poll budget (`SPARQL_TIMEOUT`, derived from
     `LAMBDA_TIMEOUT - 5`, i.e. ~25s by default) — a legitimately slow query
     can finish server-side after this Lambda has already given up and
     returned a timeout to the agent. This isn't a bug to fix (the existing
     `TimeoutError` path already handles it defensively), just worth a note
     in the Instruction to keep example/agent-generated queries narrow
     (`LIMIT`, explicit type-anchoring) rather than something to engineer
     around.
   - Max query length is 8,000 characters (rejected with `400` above that) —
     worth keeping in mind once the scoping `GRAPH` wrapper and multi-class
     `UNION`s in `count_by_type()` are added, though current query sizes are
     nowhere near the limit.

7. **A second pass over the doc turned up more gaps — most are small fixes,
   one is a real open question flagged for review rather than decided here:**
   - **`status: "error"` is a documented, distinct response shape, not just
     "any other status."** The doc shows `{"job_id", "status": "error",
     "error": "..."}` explicitly. Testing never observed it, but the Lambda
     rewrite should check for `status == "error"` specifically and raise with
     the `error` field's text, rather than lumping it into the same generic
     "any other terminal/unknown status" branch as truly-unexpected statuses
     — the two cases produce genuinely different debugging info.
   - **Poll cadence: use 2–3s, not 1s.** The doc's own guidance for a
     "reasonable client" is polling every 2–3 seconds; the request-rate limit
     (50/s sustained, 100 burst) is shared across **all** SageBrain callers,
     not just this Lambda. Polling at 1s multiplies this Lambda's share of a
     shared budget for no benefit (jobs were observed completing in 1–2s
     regardless of poll granularity). Changing the plan's earlier "e.g. 1s"
     to 2–3s.
   - **Result-size limit (>400KB JSON can fail) applies to `sparqlQuery`
     itself, not just `get_schema()`/`count_by_type()`.** The plan already
     scopes those two, but the Instruction should also tell the agent to
     default to a `LIMIT` on `sparqlQuery` calls it writes itself — nothing
     currently stops the agent from writing an unbounded `SELECT` over
     `cckp:Dataset` (1,130 rows) or `cckp:Publication` (4,773 rows) and
     hitting this failure mode after the query itself already succeeded.
   - **Endpoint URLs are CloudFormation outputs and can change if the
     SageBrain stack is recreated.** The plan hardcodes today's URL as the
     `SparqlEndpoint` parameter default (matching how the SQL variant hardcodes
     its own endpoint) — reasonable as a default, but add the doc's `aws
     cloudformation describe-stacks` lookup commands to `agents/README.md` as
     a runbook note, so a future 401/404 from a stale endpoint has a
     documented first thing to check rather than being a mystery.
   - **Token-validation caching (5 min) affects revocation timing, not just
     rotation.** The existing credential-hygiene note (Verification §6)
     already recommends rotating the testing PAT; worth adding that if the
     *deployed* agent's token is ever rotated/revoked, up to 5 minutes of
     cached validations can still succeed against the old token — expected
     behavior per the doc, not a bug if a live-during-rotation test briefly
     still works.
   - **Resolved with the user**: the doc documents a second SageBrain
     endpoint, `POST /ask` / `GET /ask/{job_id}` — SageBrain's own
     natural-language-to-SPARQL "agentic endpoint," which internally writes
     the SPARQL and calls `/query` itself, returning `{answer, steps}`. This
     was flagged as an alternative to the plan's hybrid custom-action-group
     design; **decision: keep the custom action-group design, do not use
     `/ask`.** This backend only ever calls `POST /query` / `GET
     /query/{job_id}` — `/ask` is out of scope for this plan entirely, not
     just deprioritized.

Decisions already made with the user:
1. **Hybrid agent.** The graph Lambda has no equivalent of the SQL Lambda's
   `buildExploreUrl` (the portal only accepts filters via a gzip+base64 `qw0`
   param that tool computes — an LLM can't hand-produce it). Attach a
   **second, narrow action group**, backed by the *same* `cckpSqlRag` Lambda
   code, exposing **only** `buildExploreUrl` — not `sqlQuery` or anything
   else — so the graph stays the sole data source and "Source Selection"
   stays unambiguous.
2. **Full parity pass.** Update `agents/README.md`, `agents/CHANGELOG.md`, and
   `.github/workflows/deploy-copilot-sparql.yml` alongside the template and
   Lambda, not just the CFN file in isolation.
3. **Fully independent dev/prod stack lineage, kept separate from the
   SQL-only agent's stacks — not a variant sharing infrastructure with it.**
   This backend is a major enough departure (async job-poll protocol, shared
   multi-portal graph requiring type+version scoping, hybrid two-Lambda
   action-group shape) that it should go through its own dev→prod promotion
   independently, not be coupled to the SQL agent's release cadence. In
   practice this is already how the two live workflow files are structured —
   `cckp-copilot-sql-{dev,prod}` and `cckp-copilot-sparql-{dev,prod}` are
   four distinct stacks, distinct `AgentName`s, distinct Lambda function
   names — so this decision mostly means: preserve that separation
   deliberately while making the changes below, rather than let any of the
   "reuse existing X" recommendations in this plan (the shared
   `SYNAPSE_AUTH_TOKEN` secret, the shared `cckpSqlRag.zip` artifact for
   `UrlBuilderFunction`) quietly cross-wire dev and prod *across* the two
   variants. See the `SqlLambdaS3Key` fix in Approach §2 below — the plan
   previously suggested a single hardcoded default for that parameter, which
   would have let a `sparql-dev` deploy pull in the SQL agent's *prod*
   Lambda code. Fixed there.
4. **Exception: the Knowledge Base is shared, not per-stack.** Unlike the
   Lambdas/agent/stack, the docs KB (crawl of
   help.cancercomplexity.synapse.org + the MC2 data-model docs) is reference
   material independent of which backend answers live-data queries, and is
   already external to both templates — each just takes a `KnowledgeBaseId`
   parameter pointing at a KB provisioned outside CloudFormation. The SQL
   template's default (`cloudformation.sql.yaml:134`) is the real, live KB
   ID (`KTM8BHLEXL`); the SPARQL template's default
   (`cloudformation.sparql.yaml:143`) is still the placeholder
   `REPLACE_ME_CCKP_KB_ID` left over from before any CCKP KB existed. Fixed
   in Approach §2 below — point both variants (all four stacks) at the same
   `KTM8BHLEXL` KB.

## Approach

### 1. `agents/cckp-copilot/lambda/cckpGraphRag/lambda_function.py` — core rewrite
- Replace `sparql_request()`'s body: JSON POST (`{"query": full_query}`,
  `Content-Type: application/json`, `Authorization: Bearer {SPARQL_AUTH_TOKEN}`)
  instead of form-urlencoded `action=tsv_export`. Drop the `SPARQL_API_KEY`/
  `x-api-key` path — testing showed it's unneeded.
- Add a poll loop: after the `202`, `GET {SPARQL_ENDPOINT}/{job_id}` (same
  auth header) on a 2–3s interval — not 1s; the doc's own "reasonable client"
  guidance is 2–3s, and the 50 req/s rate limit is shared across every
  SageBrain caller, not just this Lambda — until `status == "complete"`,
  raising `TimeoutError` once the existing `SPARQL_TIMEOUT` budget is
  exhausted (reuse that env var/pattern — don't invent a second timeout
  concept). Handle `status == "error"` as its own branch, raising with the
  response's `error` field text (this is a documented terminal status, not
  an unknown one); raise a separate generic `Exception` only for a truly
  unrecognized status value.
- Parse `results` (a JSON string) into SPARQL-JSON bindings, and convert to
  `{"headers": [...], "rows": [{...}], "count": N}` (see response-shape
  decision above) instead of returning raw TSV.
- **New: add a `_resolve_cckp_graph()` helper and scope every query to it —
  `cckp:` type-anchoring alone is not enough (see finding #4).** SageBrain is
  append-only and never merges/dedupes across snapshots on its own; an
  unscoped query answers from every named graph ever loaded, merged. Add a
  helper that submits `SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }`,
  filters to the CCKP naming prefix, and picks the lexicographically-latest
  match (ISO dates sort correctly as strings, same convention NF/ALS use).
  Cache the resolved URI for the Lambda's lifetime (module-level global,
  refreshed on cold start) rather than re-resolving on every tool call — that
  would double the async job count per agent turn. Wrap every subsequent
  query — `sparqlQuery`, `count_by_type()`, `get_schema()`, `get_shape()` —
  in `GRAPH <resolved-uri> { ... }` (or `FROM <resolved-uri>` where a single
  compartment is being read) instead of relying on `cckp:` filtering by
  itself. **Before writing this**, confirm with the enumerate-graphs query
  whether CCKP is actually published as dated snapshots yet
  (`urn:sagebrain:cckp:{date}`) or still lives in one undated graph — the
  helper's filter/sort logic depends on which is true.
- Rewrite `count_by_type()` to explicitly enumerate the 5 `cckp:` classes
  (`Dataset`, `Publication`, `Tool`, `Grant`, `EducationalResource`) rather
  than an unscoped `?s a ?type`, and to run inside the resolved `GRAPH` block
  above.
- Add a `cckp:`-namespace `FILTER` to `get_schema()`, and scope it to the
  resolved graph too — otherwise it still walks every snapshot's schema
  triples even once type-filtered.
- Spot-check `get_shape()` against a real class during implementation (should
  already be correctly scoped via `sh:targetClass`; confirm it also gets the
  `GRAPH` wrapper, don't assume).
- Send `X-Source: cckp-copilot` on every request (submit and poll) — costs
  nothing and makes this Lambda's traffic identifiable in SageBrain's audit
  log.
- Update `tests/*.json` fixtures and `tests/test_lambda_function.py` to match
  the new async-poll flow and the new response shape (they currently assume
  the old synchronous TSV contract) — this is the biggest test-suite change
  in the whole plan, since the request/response contract changed, not just a
  header.

### 2. `agents/cckp-copilot/cloudformation.sparql.yaml` (the bulk of the work)
Rewrite to reach parity with `cloudformation.sql.yaml`'s structure:

- **Header comment**: drop the "NOT deployable yet" warning; describe the
  async job-poll protocol, the shared multi-portal graph, and the hybrid
  two-Lambda/two-action-group shape.
- **Parameters**: `SparqlEndpoint` default → the real URL above (matching how
  NF-OSI's template hardcodes its own real endpoint as the default). Keep
  `SparqlAuthToken` (`NoEcho`) — drop the `SparqlApiKey` parameter, no longer
  needed. Add `SqlLambdaS3Key` with **no hardcoded environment-specific
  default** (or default it to the prod key and require the workflow to
  override it for dev — see the fix in Approach §3 below): the earlier draft
  of this plan defaulted it to `lambda/cckpSqlRag.zip` (the SQL agent's
  *prod* artifact) unconditionally, which would have let a `sparql-dev`
  deploy silently wire its `UrlBuilderFunction` to prod SQL code — exactly
  the dev/prod cross-wiring the separate-stack decision (#3 above) is meant
  to prevent. Never put the actual token value in the template — stays a
  deploy-time `NoEcho` override.
  **`KnowledgeBaseId` default: fix the stale placeholder.** Currently
  `REPLACE_ME_CCKP_KB_ID` with a description saying "no CCKP KB has been
  provisioned yet" (`cloudformation.sparql.yaml:141-147`) — that's no longer
  true. Per decision #4 above, change the default to `KTM8BHLEXL`, the same
  real KB ID the SQL template already defaults to
  (`cloudformation.sql.yaml:133-138`), and update the description to drop
  the "not provisioned yet" line. This is the one piece of config that
  *should* be identical across all four stacks (sql-dev/sql-prod/sparql-dev/
  sparql-prod) — it's shared reference material, not backend-specific state.
  **Auth wiring (revised per finding #5): reuse the SQL stack's existing
  `SynapseAuthToken` parameter/value rather than treating `SparqlAuthToken`
  as a wholly separate secret to provision.** SageBrain's authorizer only
  cares that the underlying Synapse identity is a member of Sage Brain Team
  (Team:3605470) — not the PAT's own scopes — so the same Synapse token the
  `cckpSqlRag` Lambda already uses for Synapse API calls
  (`cloudformation.sql.yaml:123-196`) satisfies SageBrain's auth requirement
  too, as long as that identity has (or is added to) that team membership.
  Feed the same parameter value into both `SYNAPSE_AUTH_TOKEN` (SQL/URL
  Lambda) and `SPARQL_AUTH_TOKEN` (Graph Lambda) env vars — one secret, two
  env vars — instead of standing up a second `CCKP_SPARQL_AUTH_TOKEN`
  provisioning path. The plan's original suggestion of a dedicated,
  minimally-scoped PAT for the deployed agent (vs. the personal
  `view`/`download`/`modify`-scoped PAT used for this session's manual
  testing) still stands as good practice — this only changes how the token
  reaches the Lambda, not whether it should be purpose-specific. Note in
  `agents/README.md`'s open items that team membership for that identity
  needs to be confirmed/added as a one-time manual step; it isn't implied by
  the token existing.
- **Resources**: add a second `AWS::Lambda::Function` (`UrlBuilderFunction`,
  `FunctionName: !Sub "${AgentName}-urlbuilder"`) running the *same*
  `cckpSqlRag.zip` package via `SqlLambdaS3Key`. `build_explore_url` is pure
  local computation (gzip/base64 of a JSON blob, confirmed by reading
  `lambda_function.py:317-446`) — no Synapse API call, no `SYNAPSE_AUTH_TOKEN`
  needed for this second Lambda. Add its own `LambdaBedrockPermission`, widen
  `BedrockAgentRole`'s `InvokeLambda` policy `Resource` to both Lambda ARNs.
- **Instruction**: rewrite, carrying over from `cloudformation.sql.yaml:266-353`:
  - Same Rules of Engagement style: guide-framing, curation, the scope-lock
    rule (`:276`), navigation-is-user-controlled, no-code, cast-a-wide-net.
  - **New rule, specific to this backend — two-part, not one**: (1) always
    type-anchor SPARQL queries to a `cckp:` class — this is a shared graph
    across multiple Sage portals (NF-OSI, ALS Knowledge Portal, others), and
    an untyped/mis-scoped query can silently return non-CCKP data; **and**
    (2) never query the default graph — SageBrain is append-only and keeps
    every historical snapshot loaded side by side, so an unscoped query can
    also silently merge duplicate/stale CCKP data across multiple
    publications of CCKP itself. Since the Lambda now resolves and injects
    the current graph automatically (see `_resolve_cckp_graph()` above), the
    agent doesn't need to reason about graph URIs itself — but the
    Instruction should still say plainly that results are always scoped to
    the latest resolved CCKP snapshot, so the agent doesn't claim otherwise
    if asked how current the data is.
  - **Disclosure/restriction rules can't carry over as-is** — they reference
    `getDatasetFiles`/`getFileDetails`/`checkRestriction`, which don't exist
    in this backend's toolset. Replace with a capability-matched rule: this
    backend has no restriction-checking tool, so never state or imply a
    resource's downloadability/openness — always point to its Detail Page for
    the portal's live accessibility info.
  - **Response Format / redirect mechanics**: adopt the corrected version
    from `cloudformation.sql.yaml:284-333` verbatim (well-formed
    `<actions><redirect><target>` block — the old CDATA `<query>` block here
    is the same broken mechanism already fixed in the SQL variant). Reuse the
    same Collection/Detail Page target table (`cloudformation.sql.yaml:306-312`).
  - **Source Selection**: same two-source docs KB description as
    `cloudformation.sql.yaml:318-323`; Graph KB (SPARQL action group) instead
    of Table KB for live data.
  - **Toolset section**: `sparqlQuery`/`getSchema`/`getShape`/`countByType`
    plus `buildExploreUrl` from the new action group, documented the way
    `cloudformation.sql.yaml:333` documents it (facets vs. searchExpressions
    AND/OR semantics, copied verbatim — never retype the `url`). Add an
    explicit instruction to default to a `LIMIT` on any `sparqlQuery` the
    agent writes itself — results are stored inline and an unbounded query
    over a class with 1,000+ rows (`cckp:Dataset`, `cckp:Publication`) can
    fail past a ~400KB response even after the query itself succeeded.
  - **Ontology/example-query section**: rewrite using the confirmed real
    `cckp:Dataset` property list above; pull the equivalent lists for
    `Publication`/`Tool`/`Grant`/`EducationalResource` via one more `getShape`
    or property-count query per class during implementation before finalizing
    this section (cheap, same pattern already used for Dataset). Since the
    Lambda injects the `GRAPH` wrapper server-side, example queries shown
    here should stay in the plain `?x a cckp:Dataset ; ...` form (no `GRAPH`
    clause in the agent-facing examples) — don't teach the agent to write
    graph URIs itself, that's the Lambda's job now, not the LLM's.
- **ActionGroups**: keep `cckp-graph-rag-actions` (schema updated for the new
  `{headers, rows, count}` response shape). Add `cckp-url-builder-actions`
  with an OpenAPI schema trimmed to **only** `/explore-url` — copy
  `BuildExploreUrlRequest`/`BuildExploreUrlResponse` verbatim from
  `cloudformation.sql.yaml:407-454` / `:626-651`, dropping `sqlQuery`,
  `getColumns`, `countByType`, and the discovery endpoints so this action
  group can't double as a second data source.
- **KnowledgeBases block**: update `Description` to match the two-source
  wording already used in `cloudformation.sql.yaml:608`.
- **Outputs**: `GraphRagFunctionArn` and `UrlBuilderFunctionArn`.

### 3. `.github/workflows/deploy-copilot-sparql.yml`
- Drop the "not deployable yet" comment banner.
- Pass `SparqlAuthToken="${{ secrets.SYNAPSE_AUTH_TOKEN }}"` — the **same**
  repo secret `deploy-copilot-sql.yml:81` already uses for `SynapseAuthToken`
  (per finding #5 — don't provision a separate `CCKP_SPARQL_AUTH_TOKEN`
  secret) — into `--parameter-overrides`, alongside `SparqlEndpoint` from
  `CCKP_SPARQL_ENDPOINT`.
- **`SqlLambdaS3Key` must follow the same dev/prod split the SQL workflow
  already uses for its own `S3_KEY`** (`lambda/cckpSqlRag.zip` for prod,
  `lambda/cckpSqlRag-dev.zip` for dev — `deploy-copilot-sql.yml:37-38,43`).
  In the "Set environment" step, set a matching `SQL_S3_KEY` env var per
  branch (prod: `lambda/cckpSqlRag.zip`, dev: `lambda/cckpSqlRag-dev.zip`)
  and pass it as `SqlLambdaS3Key="$SQL_S3_KEY"` in `--parameter-overrides` —
  do **not** hardcode a single value. This is what keeps `sparql-dev` from
  ever pointing its `UrlBuilderFunction` at the SQL agent's prod code (see
  decision #3).
- Add a second "Update Lambda code" step for `UrlBuilderFunction`, zipping/
  uploading `lambda/cckpSqlRag/lambda_function.py` under `$SQL_S3_KEY` when
  it changes — reuse the SQL workflow's existing source file, don't fork a
  copy. Note this means the SPARQL workflow's `paths:` trigger (currently
  only watching `cloudformation.sparql.yaml` and `lambda/cckpGraphRag/**`)
  should also watch `lambda/cckpSqlRag/lambda_function.py`, or
  `UrlBuilderFunction` can silently drift out of sync with the SQL Lambda's
  source whenever only the SQL workflow's own trigger paths fire.
- Pass `KnowledgeBaseId` through unchanged from the template's default
  (`KTM8BHLEXL`, per decision #4) — this is the one parameter that should
  **not** vary between dev and prod or between the SQL and SPARQL variants;
  don't add a dev/prod override for it the way `STACK_NAME`/`AGENT_NAME`/
  `LAMBDA_FN` already do.

### 4. `agents/README.md`
- Update the backend-variant bullets (`:9-10`) — SPARQL is no longer
  speculative; describe the hybrid two-Lambda shape and that the graph is
  shared across multiple Sage portals (CCKP data is type-scoped and
  graph-scoped within it).
- Update "Knowledge Graph Integration ... not yet deployable" (`:31`).
- Update the CI/CD section (`:41-49`) — the SPARQL-specific blocker
  (no endpoint) is resolved; the `AWS_OIDC_ROLE_ARN` gap still applies to
  both variants equally, so CI stays inactive either way.
- Update "Open items" (`:90-92`) — remove the "stand up a hosted SPARQL
  endpoint" line. Per the revised finding #5, this does **not** need a new
  `CCKP_SPARQL_AUTH_TOKEN` secret — replace with: (a) "confirm the Synapse
  identity behind the existing `SYNAPSE_AUTH_TOKEN` secret is a member of
  Sage Brain Team (Team:3605470) — required for SageBrain auth, separate
  from anything already true for Synapse API access," and (b) "if it's ever
  swapped for a dedicated, `view`-only PAT instead of the shared one, update
  only the secret value — no template/workflow change needed."
- **New**: add a short runbook note under Open items or a new "Troubleshooting"
  aside — the SPARQL/agentic endpoint URLs are CloudFormation outputs of
  SageBrain's own stacks and can change if those stacks are recreated; if
  the deployed agent starts getting connection errors, re-resolve the URL
  with:
  ```
  aws --profile sagebrain-prod cloudformation describe-stacks \
    --stack-name app-prod-neptune-api \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
  ```
  before assuming the Lambda code itself is broken.

### 5. `agents/CHANGELOG.md`
Add an `Unreleased` bullet under `cckp-copilot` documenting the new hybrid
SPARQL+SQL-URL-builder backend and the async job-poll protocol, matching the
file's existing terse style.

## Verification

1. **Schema completion** (next step, before finalizing Instruction text):
   pull real property lists for `Publication`/`Tool`/`Grant`/
   `EducationalResource` the same way Dataset's was confirmed, using the
   user's PAT one more time, transiently (never written to a repo file).
2. **Unit tests**: `cd agents/cckp-copilot/lambda/cckpGraphRag && pytest` —
   the suite needs real rewriting (see above), not just a pass/fail check,
   since the request/response contract changed.
3. **One end-to-end authenticated smoke call** after the Lambda rewrite: run
   the new `sparql_request()`/`count_by_type()` locally (not deployed) against
   the live endpoint with the user's PAT to confirm the rewritten poll-and-parse
   logic actually works before it's wrapped in CloudFormation.
4. **YAML sanity**: parse `cloudformation.sparql.yaml` with PyYAML (same
   throwaway check used for the last CFN edit).
5. **No live AWS deploy in this session** — `aws cloudformation deploy`
   requires the ADMIN role documented in `agents/README.md` and would
   create/modify real infrastructure; that stays a manual step the user runs
   themselves (or asks for explicitly) once the template/Lambda changes are
   reviewed.
6. **Credential hygiene reminder**: the PAT shared in this session for
   testing has `view`/`download`/`modify` scope and is now in the session
   transcript — recommend the user rotate/revoke it after testing if this
   transcript isn't treated as sensitive, and use a separate, `view`-only PAT
   for the actual deployed agent's `SparqlAuthToken`. Note per the doc:
   token validations are cached for 5 minutes per token, so a revoked/rotated
   token can still succeed for up to 5 minutes after the change — expected
   lag, not a sign the rotation didn't take.
