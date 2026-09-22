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

   **`Publication`'s property list confirmed live (2026-09-22, via `make
   sparql-test`)** — the same `+Term` pairing convention holds
   (`assay`/`assayTerm`, `tumorType`/`tumorTypeTerm`, `tissue`/`tissueTerm`,
   `accessibility`/`accessibilityTerm`), plus `theme`, `dataType`,
   `grantNumber`(+`Name`,+`Ref`), `consortium`(+`Ref`), `abstract`, `doi`
   (+`Iri`), `journal`, `pubMedId`(+`IdIri`,+`Link`,+`Url`),
   `publicationTitle`, `publicationYear`, `authors`, `keywords`. **One
   finding that changes the linked-resources design**: there are two
   distinct dataset-linking properties, not one —
   `cckp:dataset` (present on 4,771 of 4,773 Publications — almost
   certainly the same field the SQL variant's `publications.dataset` column
   exposes, which is a literal string frequently just `"Not Applicable"`,
   not a real link) versus **`cckp:datasetRef`** (present on only 394 of
   4,773 — far closer to the SQL variant's own real linked-dataset rate,
   and plausibly a proper object-property reference rather than a
   free-text field). **Resolved with certainty via finding #9 below** (went
   to `kg-pipeline`'s actual source rather than guessing from the counts
   alone): `cckp:datasetRef` is the real, SHACL-guaranteed link
   (`sh:class cckp:Dataset`); `cckp:dataset` is the raw literal mirror of
   the SQL column, same "looks like a link but usually isn't" shape the SQL
   variant's own Instruction already had to spell out for `dataset`/
   `datasetAlias` (`cloudformation.sql.yaml:328`). **Use `datasetRef`, never
   `dataset`, in the SPARQL variant's linked-resources example query.**

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

3. **Response shape decision:** since the real endpoint hands back structured
   SPARQL-JSON bindings (not TSV), switch the Lambda's return shape from the
   current `{"resultTsv": "..."}` to a parsed `{"headers": [...], "rows":
   [...], "count": N}` shape — mirroring the SQL Lambda's existing
   `QueryResultResponse` convention (`cloudformation.sql.yaml`'s
   `QueryResultResponse` schema) — instead of re-serializing JSON bindings
   into TSV text just to match the old contract. **No fallback to the old
   `resultTsv` shape**: this backend has never been deployed, so there is no
   real consumer of that contract to preserve — keeping it as an option
   would be a backwards-compatibility shim for a compatibility need that
   doesn't exist. Commit to the new shape outright.

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
   - **Confirmed live (2026-09-22, via `make sparql-test` run from the
     user's own machine — this session's own sandbox was network-blocked
     from reaching the endpoint, see git history for that dead end).** The
     enumerate-graphs query returned exactly six named graphs:
     ```
     urn:sagebrain:reactome:2026-09-22
     urn:sagebrain:nf:2026-09-20
     urn:sagebrain:nf:2026-09-14
     urn:sagebrain:cckp:2026-09-15
     urn:sagebrain:als:2026-09-15
     http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph
     ```
     This resolves everything finding #4 needed:
     - **The portal token is confirmed `cckp`** — `_resolve_cckp_graph()`
       should filter for the literal prefix `urn:sagebrain:cckp:`.
     - **Exactly one CCKP snapshot exists today**: `urn:sagebrain:cckp:2026-09-15`.
       So `_resolve_cckp_graph()` has nothing to disambiguate yet — but see
       the next point for why the resolver still needs to be built as if it
       did.
     - **NF already has two snapshots live right now** (`2026-09-20` and
       `2026-09-14`) — this is no longer a hypothetical "if CCKP is ever
       re-published" risk description; the exact failure mode finding #4
       warns about is observably already happening for a sibling portal on
       this same shared store, today. An unscoped `?x a cckp:Dataset` query
       would currently still be correct for CCKP specifically (only one
       snapshot exists), but the equivalent unscoped query for NF's own
       classes would already be double-counting across its two live
       snapshots — strong, concrete confirmation that this finding was
       never speculative and that CCKP will hit the identical problem the
       moment it re-publishes.
     - **`http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph` is a
       real entry that must NOT match the CCKP filter** — a plain
       Neptune-internal default graph (matches the `sagebrain-infra` issue
       found via research about deprecating/superseding this default graph
       with named snapshot graphs). A simple string-prefix filter on
       `urn:sagebrain:cckp:` naturally excludes it; a looser substring/regex
       filter might not — worth a unit test case specifically for this.
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

8. **Plan review, no-workaround lens: two mitigations below were written as
   Instruction-text/hope-based nudges where the sibling SQL Lambda already
   has a real, code-level canonical fix for the same class of risk.**
   - **Result-size overflow (finding #7's `LIMIT` note) needs server-side
     enforcement, not just an Instruction telling the agent to remember
     it.** `agents/cckp-copilot/lambda/cckpSqlRag/lambda_function.py`
     doesn't rely on the agent remembering a `limit` param either — it
     hard-clamps every query server-side via `_clamp_limit()`/`MAX_LIMIT =
     200` (`lambda_function.py:50-51,290-295`), regardless of what the agent
     requests. The current plan's only mitigation for the SPARQL side's
     equivalent risk (an unbounded `SELECT` over `cckp:Dataset`/
     `cckp:Publication` failing past ~400KB) is prompt-engineering — asking
     the agent nicely to add `LIMIT` itself. That's a compliance-dependent
     workaround for a risk the sibling backend already solves in code. Fix:
     add a shared `_ensure_limit(query: str, default_limit: int) -> str`
     helper — regex-check for an existing `LIMIT` clause (SPARQL's `LIMIT n`
     is a simple trailing clause, unlike SQL's `partMask`-based limit, so
     this means light query-text handling, not a request param) and append
     a default if absent — and run **every** query this Lambda ever submits
     to SageBrain through it: the agent-supplied `sparqlQuery` query text
     *and* the internally-built queries in `get_schema()`/`count_by_type()`.
     The Instruction-text guidance can still exist on top of this (teaching
     the agent to pick a *sensible* `LIMIT` for relevance, not just avoid a
     crash) but must not be the only thing standing between the agent and
     the failure mode.
   - **The Lambda timeout ceiling should match the documented dependency
     SLA, not be worked around by asking for narrower queries.** Finding
     #6 correctly identifies that this Lambda's poll budget (`SPARQL_TIMEOUT`,
     ~25s, derived from `LAMBDA_TIMEOUT=30`) is shorter than the vendor
     doc's documented worst-case execution ceiling (60s in the SageBrain
     worker) — but the plan's response to that is "keep example/
     agent-generated queries narrow," i.e. hope the mismatch is never hit,
     rather than closing it. `cloudformation.sparql.yaml:196,204`
     (`Timeout: 30` / `LAMBDA_TIMEOUT: "30"`) simply copies the SQL
     variant's value verbatim — reasonable for the SQL Lambda (a
     synchronous Synapse table-query call with a much shorter real-world
     latency), but never re-derived for this backend's actual dependency,
     whose vendor doc explicitly documents a 60s worst case specifically
     *because* it built an async job-poll design so real work isn't bound by
     a synchronous API's short limit (the doc's own stated reason for the
     29-second constraint that forced the async design is API Gateway's own
     hard ceiling on *SageBrain's* synchronous endpoint — a constraint this
     Lambda's own Bedrock-invoked, async-polling design was never subject to
     in the first place). Canonical fix: raise `Timeout`/`LAMBDA_TIMEOUT` to
     comfortably exceed 60s (e.g. 75–90s, giving `SPARQL_TIMEOUT` ~70–85s)
     so the full documented protocol space is actually supported, rather
     than leaving the ceiling at 30s and mitigating with instruction text.
     Confirm during implementation whether Bedrock's own action-group
     invocation wait has any shorter ceiling of its own that would cap this
     regardless — nothing in the current template suggests one, but this
     should be verified against AWS's current Bedrock Agents documentation
     rather than assumed, before picking a final number.

9. **Went to the actual source of the KG (`mc2-center/data-models`'s
   `kg-pipeline`, PR #264 — currently `OPEN`, not merged, branch
   `aws-upload-pipeline`; this is live source-of-truth research, but note
   it's an in-flight branch, not a stable committed reference — re-check
   after the PR merges or moves) instead of only inferring structure from
   live property-count queries.** This both closes an open question from
   finding #2's addendum with certainty and surfaces a real functional gap
   in `get_shape()` as currently written.
   - **`cckp:datasetRef` (and the equivalent `Ref` property for every other
     join) is now definitively confirmed, not just probable.**
     `kg-pipeline/scripts/build_triples.py` emits a `cckp:{field}Ref`
     object-property triple "per resolved join" specifically for fields
     with a `cckp_join` annotation in `kg-pipeline/schema/cckp_portal.linkml.yaml`,
     separate from the plain `cckp:{field}` literal mirror of the raw
     SQL-column value. `kg-pipeline/schema/cckp_portal.shacl.ttl` formally
     asserts the target type of every one of these `Ref` properties (e.g.
     `shape:DatasetRefShape`: `sh:targetSubjectsOf cckp:datasetRef ;
     sh:property [ sh:class cckp:Dataset ; ... ]`) — this is a hand-authored,
     validated (`make validate` runs `pyshacl`-style checks against it)
     contract, not an inference from sampled data. **Use `{field}Ref`
     properties for every cross-class join in the Instruction's
     linked-resources examples, never the plain `{field}` literal.**
   - **The full join map, confirmed from `cckp_portal.linkml.yaml`'s
     `cckp_join` annotations (far more complete than what this plan had
     before — it only knew about Publication↔Dataset):**
     | Source `Class.field` | → Target | Emits |
     |---|---|---|
     | `Dataset.pubMedId` | `Publication.pubMedId` | `cckp:pubMedIdRef` → `cckp:Publication` |
     | `Dataset.grantNumber` | `Grant.grantNumber` | `cckp:grantNumberRef` → `cckp:Grant` |
     | `Publication.dataset` | `Dataset.datasetAlias` | `cckp:datasetRef` → `cckp:Dataset` |
     | `Publication.grantNumber` | `Grant.grantNumber` | `cckp:grantNumberRef` → `cckp:Grant` |
     | `Tool.pubMedId` | `Publication.pubMedId` | `cckp:pubMedIdRef` → `cckp:Publication` |
     | `Tool.datasets` | `Dataset.datasetAlias` | `cckp:datasetRef` → `cckp:Dataset` |
     | `Tool.grantNumber` | `Grant.grantNumber` | `cckp:grantNumberRef` → `cckp:Grant` |
     | `EducationalResource.publicationId` | `Publication.pubMedId` | `cckp:publicationIdRef` → `cckp:Publication` |
     | `EducationalResource.grantNumber` | `Grant.grantNumber` | `cckp:grantNumberRef` → `cckp:Grant` |

     Every class joins to `Grant` via `grantNumber`/`grantNumberRef` — the
     Instruction's linked-resources section should cover all four of these,
     not just the Publication↔Dataset pair this plan previously knew about.
   - **`get_shape()` will most likely return empty for `Publication`,
     `Tool`, and `EducationalResource` as currently designed — this is no
     longer a "spot-check, probably fine" item, it's a concrete predicted
     gap with a source-level reason.** `get_shape()` queries
     `?shape a sh:NodeShape ; sh:targetClass cckp:{class}`, but
     `cckp_portal.shacl.ttl` only has **two** `sh:targetClass`-based shapes
     total — `shape:DatasetShape` (`sh:targetClass cckp:Dataset`) and
     `shape:GrantShape` (`sh:targetClass cckp:Grant`). Every other shape in
     that file targets via `sh:targetSubjectsOf cckp:{property}` (a
     property-keyed target, not a class-keyed one) — there is no
     `sh:targetClass cckp:Publication`/`cckp:Tool`/`cckp:EducationalResource`
     shape anywhere in CCKP's own hand-authored shapes file, and
     `kg-pipeline/Makefile` only ever validates against this one file (no
     second, auto-generated SHACL file exists to fill the gap). **This
     needs a real design decision, not just a spot-check**: either (a)
     confirm live whether some other mechanism still produces a
     `sh:targetClass`-based shape for these three classes elsewhere in the
     merged graph (test directly — don't assume from the repo alone, since
     the live graph could differ from what's in this branch), or (b) if
     confirmed empty, have `get_shape()` fall back to also querying
     `sh:targetSubjectsOf` shapes for that class's own known join
     properties, or (c) accept and clearly document that `getShape` only
     gives real constraint info for `Dataset`/`Grant` today, and say so in
     the Instruction rather than let the agent treat a silently-empty
     result as "this class has no documented shape constraints."

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
  itself. **Confirmed live (2026-09-22, see finding #4)**: CCKP is published
  as a dated snapshot, `urn:sagebrain:cckp:2026-09-15` today — filter on the
  literal prefix `urn:sagebrain:cckp:` (this also naturally excludes the
  real `http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph` entry
  confirmed to exist alongside it; a looser substring match might not, so
  test that case explicitly). Only one snapshot exists right now, but NF
  already has two live (`urn:sagebrain:nf:2026-09-20` and `-09-14`) — build
  the lexicographically-latest-wins resolution logic for real, not as a
  single-graph special case, since CCKP will follow the same pattern the
  moment it re-publishes.
- Rewrite `count_by_type()` to explicitly enumerate the 5 `cckp:` classes
  (`Dataset`, `Publication`, `Tool`, `Grant`, `EducationalResource`) rather
  than an unscoped `?s a ?type`, and to run inside the resolved `GRAPH` block
  above.
- Add a `cckp:`-namespace `FILTER` to `get_schema()`, and scope it to the
  resolved graph too — otherwise it still walks every snapshot's schema
  triples even once type-filtered.
- **`get_shape()` needs a real decision, not just a spot-check — see finding
  #9.** Source inspection of `kg-pipeline/schema/cckp_portal.shacl.ttl`
  found only two `sh:targetClass`-based shapes (`Dataset`, `Grant`); every
  other shape in that file targets via `sh:targetSubjectsOf
  cckp:{property}` instead, and no second/generated SHACL file exists to
  cover `Publication`/`Tool`/`EducationalResource`. Confirm live first
  (`make sparql-test QUERY='SELECT ?shape WHERE { GRAPH
  <urn:sagebrain:cckp:...> { ?shape a sh:NodeShape ; sh:targetClass
  cckp:Publication } }'` — the live graph could differ from this
  unmerged branch) whether `get_shape("Publication")` really does come back
  empty; if so, pick one of finding #9's three options (fall back to
  `sh:targetSubjectsOf` shapes for that class's join properties, or
  document the `Dataset`/`Grant`-only limitation plainly in the
  Instruction) rather than shipping a tool that silently returns nothing
  for 3 of 5 classes. Also confirm it gets the `GRAPH` wrapper regardless
  of which option is chosen.
- **New: add a shared `_ensure_limit(query: str, default_limit: int = 200) ->
  str` helper and run every submitted query through it (see finding #8) —
  code-level enforcement, not just an Instruction-text reminder.** Regex-check
  for an existing `LIMIT` clause (case-insensitive, e.g.
  `re.search(r"\bLIMIT\s+\d+\b", query, re.IGNORECASE)`) and append
  `f"LIMIT {default_limit}"` if absent. Apply it to `sparql_query()`'s
  agent-supplied query text *and* to the internally-built queries in
  `get_schema()`/`count_by_type()` — one shared enforcement point for every
  query this Lambda ever submits to SageBrain, mirroring
  `cckpSqlRag/lambda_function.py`'s existing `_clamp_limit()`/`MAX_LIMIT`
  pattern (`lambda_function.py:50-51,290-295`), which already hard-caps the
  SQL side's equivalent risk in code rather than relying on the agent
  remembering a param. The Instruction-text `LIMIT` guidance from finding #7
  is still worth keeping on top of this (teaches the agent to pick a
  *sensible* limit for relevance, not just avoid a crash), but this code-level
  cap is what actually prevents the >400KB failure mode regardless of what
  the agent does or forgets to do.
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
- **`GraphRagFunction`'s `Timeout`/`LAMBDA_TIMEOUT` (see finding #8): raise
  from `30` to comfortably exceed SageBrain's documented 60s worst case** —
  e.g. `Timeout: 90` and `LAMBDA_TIMEOUT: "90"` (giving `SPARQL_TIMEOUT`
  ~85s). The template currently copies the SQL variant's `Timeout: 30`
  verbatim (`cloudformation.sparql.yaml:196,204`), which was never re-derived
  for this backend's actual dependency latency. **Resolved via research**:
  there is no Bedrock-service-side ceiling shorter than Lambda's own 15-minute
  maximum that would cap this — Lambda invoked as a Bedrock action-group
  executor is bound by its own configured `Timeout` only. The "InvokeAgent
  has a 60-second timeout" claim that shows up when researching this is
  **boto3's own generic client-side `read_timeout` default** (applies to
  every boto3 service client, not something Bedrock Agents specifically
  imposes) — fully overridable per-caller via
  `Config(read_timeout=...)`. `UrlBuilderFunction` (pure local computation,
  no network call) keeps the existing `Timeout: 30` — this only applies to
  the graph-querying Lambda.
  - **This makes raising the Lambda timeout a two-sided fix, not a one-line
    CFN change**: it only helps if every caller of `invoke_agent` against
    this backend *also* raises its own `read_timeout` past 60s, or the
    client-side socket read simply drops the connection at 60s regardless of
    whether the Lambda is still legitimately working. Concretely:
    - This repo's own `benchmark/kb-routing/evaluate_kb_routing.py`,
      `benchmark/redteam/evaluate_redteam.py`, and
      `benchmark/resource-search/evaluate_resource_search.py` all construct
      `boto3.Session(...).client("bedrock-agent-runtime")` with no `Config`
      override today — meaning a legitimately-slow SPARQL query (up to the
      new ~85s budget) would surface as a client-side `ReadTimeoutError` in
      any of these evals once the SPARQL variant is actually tested, not as
      a real agent failure. Add `--read-timeout` (default matching the
      client's normal 60s, overridable) to each script's `boto3.Session(...)`
      → `Config(read_timeout=...)` construction before evaluating the SPARQL
      variant with these tools — a small, mechanical change once identified,
      but a real gap if missed (a false "the SPARQL backend is broken/slow"
      reading that's actually just the eval harness's own client timing out
      first).
    - **The production CCKP chat frontend's own timeout configuration is
      outside this repo and this plan's control** — it's a separate system
      (the portal's own chat UI, not part of `cckp-chatbot`) whose HTTP/SDK
      client timeout this plan has no visibility into. Flag this explicitly
      to whoever owns that frontend before this backend ships: a raised
      Lambda timeout accomplishes nothing in production if the frontend's
      own request timeout is still ~60s or shorter. This is a real
      cross-system dependency for the timeout fix to actually work
      end-to-end, not something this plan can verify or fix by itself.
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
    `cckp:Dataset`/`cckp:Publication` property lists above; pull the
    equivalent lists for `Tool`/`Grant`/`EducationalResource` (Verification
    §1) before finalizing this section. Since the Lambda injects the `GRAPH`
    wrapper server-side, example queries shown here should stay in the plain
    `?x a cckp:Dataset ; ...` form (no `GRAPH` clause in the agent-facing
    examples) — don't teach the agent to write graph URIs itself, that's the
    Lambda's job now, not the LLM's.
    **Include a linked-resources example per finding #9's confirmed join
    map, using `Ref` properties exclusively** — `?pub cckp:datasetRef
    ?dataset` (Publication→Dataset), `?dataset cckp:pubMedIdRef ?pub`
    (Dataset→Publication), `?tool cckp:datasetRef ?dataset` /
    `?tool cckp:pubMedIdRef ?pub` (Tool→Dataset/Publication),
    `?edu cckp:publicationIdRef ?pub` (EducationalResource→Publication), and
    `?x cckp:grantNumberRef ?grant` for every class's Grant back-reference —
    never the bare `dataset`/`pubMedId`/`publicationId`/`grantNumber`
    literal fields for a join, only for displaying the raw value.
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

### 6. Benchmark evaluator scripts — required companion to the timeout raise
Per finding #8's `read_timeout` discovery: add a `--read-timeout` CLI flag
(default 60, matching today's implicit boto3 default) to
`benchmark/kb-routing/evaluate_kb_routing.py`,
`benchmark/redteam/evaluate_redteam.py`, and
`benchmark/resource-search/evaluate_resource_search.py`, threaded into each
script's `boto3.Session(...).client("bedrock-agent-runtime")` construction
via `config=Config(read_timeout=args.read_timeout)`. Without this, running
any of these evals against a SPARQL-backed agent will misreport a
legitimately-slow-but-successful query as a client-side timeout error once
the Lambda's own timeout is raised past 60s. Low priority relative to
sections 1–5 (only matters once the SPARQL variant is actually evaluated),
but cheap and mechanical — worth doing in the same pass rather than
rediscovering it later as a confusing false failure.

## Verification

1. **Schema completion** (next step, before finalizing Instruction text):
   pull real property lists for `Publication`/`Tool`/`Grant`/
   `EducationalResource` the same way Dataset's was confirmed. The
   graph-enumeration query confirmed the live CCKP graph is
   `urn:sagebrain:cckp:2026-09-15` (2026-09-22) — will need re-checking for
   drift if a second CCKP snapshot lands before this step finishes. Use
   `make sparql-test` (confirmed working from the user's machine now — see
   finding #4), one property-count query per remaining class:
   ```bash
   make sparql-test QUERY='SELECT ?p (COUNT(*) AS ?n) WHERE { GRAPH <urn:sagebrain:cckp:2026-09-15> { ?s a cckp:Tool ; ?p ?o } } GROUP BY ?p ORDER BY DESC(?n)'
   ```
   substituting `Grant`/`EducationalResource` for `Tool` on repeat runs (and
   the current graph URI, if it's changed by then). No `PREFIX cckp: ...`
   needed — `make sparql-test` auto-prepends the real deployed Lambda's own
   `DEFAULT_PREFIXES`.
   - **`Publication` — done** (2026-09-22, see finding #2's addendum above):
     confirmed real property list, and a genuinely important finding —
     `cckp:dataset` vs. `cckp:datasetRef` are two different properties with
     very different fill rates (4,771/4,773 vs. 394/4,773), and the smaller
     one is the more likely real link. Needs a spot-check (does a
     `datasetRef` value actually resolve to a real `cckp:Dataset` node?)
     before finalizing the linked-resources example query.
   - **`Tool`/`Grant`/`EducationalResource` — still open.**
2. **Unit tests**: `cd agents/cckp-copilot/lambda/cckpGraphRag && pytest` —
   the suite needs real rewriting (see above), not just a pass/fail check,
   since the request/response contract changed.
3. **One end-to-end authenticated smoke call** after the Lambda rewrite: run
   the new `sparql_request()`/`count_by_type()` locally (not deployed) against
   the live endpoint with the user's PAT to confirm the rewritten poll-and-parse
   logic actually works before it's wrapped in CloudFormation. `make
   sparql-test` (with `QUERY=` overridden to the real rewritten call's
   query shape) is the fastest way to check this by hand before wiring it
   into the Lambda proper.
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
   lag, not a sign the rotation didn't take. Separately: this session also
   read the PAT from `~/.synapseConfig` (extracted into a shell variable,
   never printed or echoed into the visible transcript) to attempt the
   live queries above — every attempt was rejected by AWS before reaching
   SageBrain's own auth logic (see the blockers above), so this specific
   attempt never actually authenticated, but the token did leave the
   sandbox as an `Authorization` header on each rejected request, which is
   worth knowing about even though it's the token's normal intended use.
