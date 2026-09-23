import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


SPARQL_ENDPOINT = os.environ.get(
    "SPARQL_ENDPOINT", "https://vyar2xyj0k.execute-api.us-east-1.amazonaws.com/prod/query"
)
SPARQL_AUTH_TOKEN = os.environ.get("SPARQL_AUTH_TOKEN", "")

# LAMBDA_TIMEOUT should match the Lambda function's configured timeout (seconds).
# This backend's dependency (SageBrain) has a documented 60s worst-case query
# time in its own worker, so the CloudFormation template configures this
# Lambda's Timeout well above that (90s) rather than the SQL variant's 30s —
# see plans/implement-sparql-backend.md finding #8. SPARQL_TIMEOUT is kept
# shorter than LAMBDA_TIMEOUT so the Lambda always has time to return a clean
# error response before AWS forcibly kills the invocation (which causes a 424).
_LAMBDA_TIMEOUT = int(os.environ.get("LAMBDA_TIMEOUT", "90"))
SPARQL_TIMEOUT = int(os.environ.get("SPARQL_TIMEOUT", str(max(_LAMBDA_TIMEOUT - 5, 5))))

# Vendor doc's own "reasonable client" guidance is a 2-3s poll interval; the
# 50 req/s rate limit on this endpoint is shared across every SageBrain
# caller, not just this Lambda, so polling faster buys nothing (jobs were
# observed completing in 1-2s regardless of poll granularity) and only
# multiplies this Lambda's share of a shared budget.
POLL_INTERVAL_SECONDS = 3

# Default row cap for any query this Lambda submits that doesn't already
# specify one — mirrors cckpSqlRag/lambda_function.py's MAX_LIMIT pattern:
# a code-level enforcement, not just an Instruction-text reminder, since
# results are stored inline and a query over a class with 1,000+ rows
# (cckp:Dataset, cckp:Publication) can fail past ~400KB even after the
# query itself succeeded.
DEFAULT_QUERY_LIMIT = 200
# Hard ceiling on an agent-supplied LIMIT — same value and role as
# cckpSqlRag's MAX_LIMIT, so `LIMIT 100000` can't bypass the cap above.
MAX_QUERY_LIMIT = 200

# Poll-time HTTP statuses worth retrying within the remaining budget: the
# 50 req/s rate limit is shared across every SageBrain caller, so a 429 is
# often someone else's burst, and a GET /{job_id} poll is idempotent.
RETRYABLE_POLL_STATUSES = {429, 500, 502, 503, 504}

# The 5 real CCKP classes under https://w3id.org/mc2-center/cckp-portal/,
# confirmed live 2026-09-22 against urn:sagebrain:cckp:2026-09-15 (see
# plans/implement-sparql-backend.md findings #2, #9). SageBrain is a shared
# triple store across multiple Sage-affiliated portals (NF-OSI, ALS
# Knowledge Portal, Reactome, etc.) — every query must type-anchor to one
# of these classes or it silently pulls in non-CCKP data.
CCKP_CLASSES = ["Dataset", "Publication", "Tool", "Grant", "EducationalResource"]

# SageBrain is append-only: every portal publication loads into its own
# dated named graph (urn:sagebrain:{portal}:{YYYY-MM-DD}), and nothing is
# ever removed automatically. A query against the default graph (no GRAPH/
# FROM) merges every snapshot ever loaded for every portal at once — this
# is not hypothetical: NF-OSI already has two live snapshots on the shared
# store as of 2026-09-22. This prefix is used to find CCKP's own current
# snapshot among all of them.
CCKP_GRAPH_PREFIX = "urn:sagebrain:cckp:"

DEFAULT_PREFIXES = """\
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX cckp: <https://w3id.org/mc2-center/cckp-portal/>
PREFIX efo: <http://www.ebi.ac.uk/efo/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX prov: <http://www.w3.org/ns/prov#>
"""

# Cached across warm invocations within the same Lambda execution
# environment so resolving the current CCKP graph doesn't cost an extra
# async submit/poll round-trip on every single tool call. Re-resolved after
# GRAPH_CACHE_TTL_SECONDS so a long-warm container picks up a newly
# published snapshot instead of pinning the one it saw at cold start.
GRAPH_CACHE_TTL_SECONDS = 3600
_resolved_graph_uri: Optional[str] = None
_resolved_graph_at: float = 0.0

# One time budget per Lambda invocation, shared by every submit/poll it
# makes (graph resolution + the actual query), so their sum can never run
# past the Lambda's own Timeout. Set by lambda_handler; None means "no
# invocation budget set" (direct calls, tests) and falls back to
# SPARQL_TIMEOUT per job.
_invocation_deadline: Optional[float] = None


def _make_response(action_group, api_path, http_method, http_status, body):
    """Build a Bedrock action-group response that is always JSON-serializable."""
    try:
        body_str = json.dumps(body)
    except (TypeError, ValueError):
        body_str = json.dumps({"error": "Response could not be serialized"})
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": http_status,
            "responseBody": {
                "application/json": {"body": body_str}
            },
        },
    }


def lambda_handler(event, context):
    """
    Lambda handler for exposing the cckp_rag SPARQL helper functions to an agent.

    The entire body is wrapped in a top-level try/except so that a well-formed
    response is *always* returned.  An unhandled exception would cause an AWS
    invocation failure, which Bedrock surfaces as a 424 error.
    """
    global _invocation_deadline
    action_group = api_path = http_method = None
    budget = SPARQL_TIMEOUT
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        # Leave 5s to build and return the error response ourselves rather
        # than letting AWS kill the invocation (which Bedrock shows as 424).
        budget = min(budget, context.get_remaining_time_in_millis() / 1000 - 5)
    _invocation_deadline = time.monotonic() + max(budget, 1)
    try:
        print(f"Received event: {json.dumps(event)}")

        action_group = event.get("actionGroup")
        api_path = event.get("apiPath", "")
        http_method = event.get("httpMethod", "POST")
        function = map_api_path_to_function(api_path) or event.get("function")
        params = extract_params(event)

        print(f"API path: {api_path}, function: {function}, params: {params}")

        if function == "sparqlQuery":
            response_body = sparql_query(params)
        elif function == "getSchema":
            response_body = get_schema(params)
        elif function == "getShape":
            response_body = get_shape(params)
        elif function == "countByType":
            response_body = count_by_type(params)
        else:
            response_body = {
                "error": f"Unknown function: {function}",
                "apiPath": api_path,
                "eventKeys": list(event.keys()),
            }

        response = _make_response(
            action_group, api_path, http_method, 200, response_body
        )
    except TimeoutError as e:
        print(f"Timeout: {e}")
        response = _make_response(
            action_group, api_path, http_method, 200, {"error": str(e)}
        )
    except Exception as e:
        print(f"Error processing request: {e}")
        response = _make_response(
            action_group, api_path, http_method, 200,
            {"error": f"Failed to process request: {e}"},
        )
    finally:
        _invocation_deadline = None

    print(f"Returning response: {json.dumps(response)}")
    return response


def map_api_path_to_function(api_path: str) -> str | None:
    mapping = {
        "/sparql-query": "sparqlQuery",
        "/schema": "getSchema",
        "/shape": "getShape",
        "/count-by-type": "countByType",
    }
    return mapping.get(api_path)


def extract_params(event: Dict[str, Any]) -> Dict[str, Any]:
    request_body = event.get("requestBody", {})
    content = request_body.get("content", {})
    application_json = content.get("application/json", {})
    properties = application_json.get("properties", [])

    params: Dict[str, Any] = {}
    for prop in properties:
        params[prop["name"]] = prop["value"]

    for item in event.get("parameters", []):
        if "name" in item and "value" in item:
            params[item["name"]] = item["value"]

    return params


# Masks string literals, IRIs, and comments (same-length, so indices still
# line up with the original text) before any structural scan below — a `{`,
# `LIMIT 5`, or `GRAPH` inside "a literal", <an/iri#frag>, or a # comment
# must not be mistaken for query structure. Alternation order doesn't matter
# (each alternative starts with a distinct character); re.sub's left-to-right
# scan means whichever construct opens first consumes the rest.
_MASKABLE = re.compile(
    r'"""[\s\S]*?"""'
    r"|'''[\s\S]*?'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r'|<[^<>"{}|^`\\\s]*>'
    r"|#[^\n]*"
)


def _mask(query: str) -> str:
    return _MASKABLE.sub(lambda m: " " * len(m.group(0)), query)


def _find_where_body(masked: str) -> Optional[tuple]:
    """(start, end) indices of the first top-level `{ ... }` pair — the
    WHERE-clause body — in already-masked query text, or None."""
    start = masked.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _query_form(masked: str) -> Optional[str]:
    """SELECT/ASK/CONSTRUCT/DESCRIBE — the first query-form keyword after
    any PREFIX/BASE declarations (whose IRIs are already masked out)."""
    match = re.search(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", masked, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _ensure_limit(query: str, default_limit: int = DEFAULT_QUERY_LIMIT) -> str:
    """Guarantee the outer query has a LIMIT no larger than MAX_QUERY_LIMIT.

    Code-level enforcement, not just an Instruction-text reminder — mirrors
    cckpSqlRag/lambda_function.py's _clamp_limit()/MAX_LIMIT pattern. Only
    the solution modifiers *after* the WHERE body count, so a subquery's own
    `LIMIT 5` doesn't satisfy the outer cap. An oversized LIMIT is clamped.
    A missing one is inserted before a trailing VALUES block (LIMIT must
    precede it) or else appended.
    """
    masked = _mask(query)
    body = _find_where_body(masked)
    if body is None:
        return f"{query}\nLIMIT {default_limit}"

    tail_start = body[1] + 1
    masked_tail = masked[tail_start:]

    existing = re.search(r"\bLIMIT\s+(\d+)\b", masked_tail, re.IGNORECASE)
    if existing:
        if int(existing.group(1)) <= MAX_QUERY_LIMIT:
            return query
        num_start = tail_start + existing.start(1)
        num_end = tail_start + existing.end(1)
        return f"{query[:num_start]}{MAX_QUERY_LIMIT}{query[num_end:]}"

    values = re.search(r"\bVALUES\b", masked_tail, re.IGNORECASE)
    if values:
        at = tail_start + values.start()
        return f"{query[:at]}LIMIT {default_limit}\n{query[at:]}"
    return f"{query}\nLIMIT {default_limit}"


def _remaining_budget() -> float:
    """Seconds left in this invocation's shared budget (see
    _invocation_deadline). Falls back to SPARQL_TIMEOUT when no handler
    set one."""
    if _invocation_deadline is None:
        return float(SPARQL_TIMEOUT)
    return _invocation_deadline - time.monotonic()


def _auth_headers() -> Dict[str, str]:
    # X-Source labels this Lambda's traffic in SageBrain's audit log (which
    # records full query text, caller IP, duration, and status per request)
    # — distinguishes it from other SageBrain callers.
    headers = {"X-Source": "cckp-copilot"}
    if SPARQL_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {SPARQL_AUTH_TOKEN}"
    return headers


def _submit_query(query: str, include_default_prefixes: bool = True) -> str:
    """Submit a SPARQL query to SageBrain's async job-queue endpoint.

    Returns the job_id. A 429 (not accepted, so safe to resend) is retried
    within the remaining budget; any other non-202 raises. 5xx is not
    retried here — the job may already have been queued.
    """
    full_query = f"{DEFAULT_PREFIXES}\n{query}" if include_default_prefixes else query
    body = json.dumps({"query": full_query}).encode("utf-8")
    headers = {"Content-Type": "application/json", **_auth_headers()}

    while True:
        remaining = _remaining_budget()
        if remaining <= 0:
            raise TimeoutError("SPARQL time budget exhausted before submit")
        req = urllib.request.Request(
            SPARQL_ENDPOINT, data=body, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=remaining) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and _remaining_budget() > POLL_INTERVAL_SECONDS:
                print(f"SPARQL submit rate-limited (429), retrying: {detail}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            raise Exception(f"SPARQL submit error {e.code}: {detail}")
        except (urllib.error.URLError, socket.timeout) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(f"SPARQL submit timed out after {remaining:.0f}s")
            raise Exception(f"Submit request failed: {e}")

    job_id = payload.get("job_id")
    if not job_id:
        raise Exception(f"Submit response had no job_id: {payload}")
    return job_id


def _poll_job(job_id: str) -> str:
    """Poll a submitted job until it completes, errors, or the invocation's
    remaining budget is exhausted. Returns the raw `results` string (SPARQL
    1.1 Query Results JSON, itself encoded as a JSON string — double-encoded,
    needs a second json.loads by the caller) on success.
    """
    poll_url = f"{SPARQL_ENDPOINT.rstrip('/')}/{job_id}"
    headers = _auth_headers()
    deadline = time.monotonic() + _remaining_budget()

    def _timed_out():
        return TimeoutError(
            f"SPARQL query timed out (job_id={job_id}, may still complete server-side)"
        )

    while True:
        if deadline - time.monotonic() <= POLL_INTERVAL_SECONDS:
            raise _timed_out()
        time.sleep(POLL_INTERVAL_SECONDS)
        req = urllib.request.Request(poll_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(deadline - time.monotonic(), 1)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in RETRYABLE_POLL_STATUSES:
                print(f"SPARQL poll got {e.code}, retrying: {detail}")
                continue
            raise Exception(f"SPARQL poll error {e.code}: {detail}")
        except (urllib.error.URLError, socket.timeout) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise _timed_out()
            raise Exception(f"Poll request failed: {e}")

        status = payload.get("status")

        if status == "complete":
            return payload.get("results", "")

        # Documented, distinct terminal status — not an unknown one. Raise
        # with the response's own error text rather than lumping this into
        # the generic unrecognized-status branch below.
        if status == "error":
            raise Exception(f"SPARQL job {job_id} failed: {payload.get('error', payload)}")

        if status not in ("pending", "running"):
            raise Exception(f"SPARQL job {job_id} returned unrecognized status: {status!r}")


def _run(query: str, include_default_prefixes: bool = True) -> Dict[str, Any]:
    """Submit + poll + decode the double-encoded results into SPARQL 1.1
    Query Results JSON."""
    job_id = _submit_query(query, include_default_prefixes=include_default_prefixes)
    results_raw = _poll_job(job_id)
    try:
        parsed = json.loads(results_raw) if isinstance(results_raw, str) else results_raw
    except (json.JSONDecodeError, TypeError) as e:
        raise Exception(f"Failed to parse SPARQL results JSON: {e}")
    return parsed or {}


def sparql_request(query: str, include_default_prefixes: bool = True) -> Dict[str, Any]:
    """Submit a query, poll for its result, and return it as {"headers":
    [...], "rows": [{...}], "count": N} for SELECT, or {"boolean": bool}
    for ASK.

    Committed fully to this shape — no TSV/resultTsv fallback. This backend
    has never been deployed, so there is no real consumer of the old
    contract to preserve.
    """
    parsed = _run(query, include_default_prefixes=include_default_prefixes)

    if "boolean" in parsed:
        return {"boolean": bool(parsed["boolean"])}

    variables = (parsed.get("head") or {}).get("vars", [])
    bindings = (parsed.get("results") or {}).get("bindings", [])

    rows = []
    for binding in bindings:
        row = {var: binding[var].get("value") for var in variables if var in binding}
        rows.append(row)

    return {"headers": variables, "rows": rows, "count": len(rows)}


def _resolve_cckp_graph() -> str:
    """Find the current, completely-loaded CCKP named graph among every
    snapshot SageBrain has ever loaded, across every portal on this shared
    store.

    Type-anchoring to a cckp: class alone is not sufficient — it fixes the
    NF-OSI/ALSKP-bleed problem but not CCKP's own history: once CCKP is
    re-published as a second dated snapshot (NF already has two live as of
    2026-09-22), an unscoped-but-type-anchored query would merge both.

    Discovery is anchored on `?s a cckp:Dataset` (an indexed type lookup
    touching only CCKP graphs) rather than a `GRAPH ?g { ?s ?p ?o }` scan of
    every quad in the shared store. Each candidate is paired with whether it
    carries kg-pipeline's _provenance.ttl completion triple (prov:Activity +
    cckp:portal "cckp"); the lexicographically-latest graph *with*
    provenance wins, so a snapshot caught mid-bulk-load is skipped in favor
    of the last complete one. The provenance mechanism is inferred from
    kg-pipeline's source and not yet confirmed live, so if *no* candidate
    has it, fall back to the latest graph with a warning rather than
    refusing to answer at all.

    Cached for GRAPH_CACHE_TTL_SECONDS per execution environment.
    """
    global _resolved_graph_uri, _resolved_graph_at
    if (
        _resolved_graph_uri is not None
        and time.monotonic() - _resolved_graph_at < GRAPH_CACHE_TTL_SECONDS
    ):
        return _resolved_graph_uri

    query = (
        "SELECT ?g (COUNT(?b) AS ?prov) WHERE {\n"
        "  { SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s a cckp:Dataset } } }\n"
        '  OPTIONAL { GRAPH ?g { ?b a prov:Activity ; cckp:portal "cckp" } }\n'
        "} GROUP BY ?g"
    )
    parsed = _run(query, include_default_prefixes=True)
    bindings = (parsed.get("results") or {}).get("bindings", [])

    candidates = {}
    for b in bindings:
        g = (b.get("g") or {}).get("value", "")
        if g.startswith(CCKP_GRAPH_PREFIX):
            candidates[g] = int((b.get("prov") or {}).get("value", "0") or 0) > 0
    if not candidates:
        raise Exception(
            f"No named graph found matching prefix {CCKP_GRAPH_PREFIX!r} — "
            "cannot scope any query without a resolved CCKP graph."
        )

    complete = [g for g, has_prov in candidates.items() if has_prov]
    latest = max(candidates)  # lexicographically latest = most recent date
    if complete:
        chosen = max(complete)
        if chosen != latest:
            print(
                f"WARNING: newest CCKP graph {latest} has no provenance triple "
                f"(load may be incomplete) — using last complete snapshot {chosen}."
            )
    else:
        chosen = latest
        print(
            f"WARNING: no CCKP graph carries a prov:Activity provenance triple — "
            f"using latest {chosen} unverified."
        )

    _resolved_graph_uri = chosen
    _resolved_graph_at = time.monotonic()
    print(f"Resolved CCKP graph: {_resolved_graph_uri}")
    return _resolved_graph_uri


# Keywords that would let an agent-written query escape the injected GRAPH
# scope: a nested `GRAPH ?g {}` matches every named graph (other portals,
# old snapshots); FROM/FROM NAMED replaces the dataset; SERVICE federates
# out entirely.
_SCOPE_ESCAPES = re.compile(r"\b(GRAPH|FROM|SERVICE)\b", re.IGNORECASE)


def sparql_query(params: Dict[str, Any]) -> Dict[str, Any]:
    query = params.get("query")
    if not query:
        return {"error": "query is required"}

    masked = _mask(query)
    form = _query_form(masked)
    if form not in ("SELECT", "ASK"):
        return {
            "error": (
                f"Only SELECT and ASK queries are supported (got {form or 'no query form'}). "
                "Rewrite as a SELECT over the properties you need."
            )
        }
    escape = _SCOPE_ESCAPES.search(masked)
    if escape:
        return {
            "error": (
                f"Queries may not use {escape.group(1).upper()} — the Lambda scopes every "
                "query to the current CCKP graph itself. Remove it and resend."
            )
        }

    graph_uri = _resolve_cckp_graph()
    scoped_query = _wrap_in_graph(query, graph_uri)
    if form == "SELECT":
        scoped_query = _ensure_limit(scoped_query)
    return sparql_request(scoped_query, include_default_prefixes=True)


def _wrap_in_graph(query: str, graph_uri: str) -> str:
    """Wrap a SELECT/ASK query's WHERE-clause body in a GRAPH <uri> { ... }
    block, so it's scoped to one named graph instead of running against the
    (shared, multi-snapshot) default graph.

    Finds the first top-level `{` ... matching `}` pair (the WHERE clause
    body, located on masked text so braces inside literals/IRIs/comments
    don't count) and wraps its *contents* in a GRAPH block, leaving any
    leading PREFIX/SELECT/aggregate head and any trailing GROUP BY/ORDER
    BY/LIMIT/VALUES untouched. sparql_query rejects GRAPH/FROM/SERVICE in
    agent text before calling this, so the injected scope can't be
    overridden from inside the body.
    """
    body = _find_where_body(_mask(query))
    if body is None:
        # No brace body at all (e.g. a malformed query) — let the endpoint
        # itself reject it with a real SPARQL syntax error rather than
        # silently failing here.
        return query
    start, end = body
    inner = query[start + 1:end]
    return f"{query[:start]}{{ GRAPH <{graph_uri}> {{{inner}}} }}{query[end + 1:]}"


def get_schema(params: Dict[str, Any]) -> Dict[str, Any]:
    graph_uri = _resolve_cckp_graph()
    query = f"""\
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?term ?kind ?label ?comment ?domain ?range WHERE {{
  GRAPH <{graph_uri}> {{
    {{
      ?term a owl:Class .
      BIND("Class" AS ?kind)
    }} UNION {{
      ?term a owl:ObjectProperty .
      BIND("ObjectProperty" AS ?kind)
    }} UNION {{
      ?term a owl:DatatypeProperty .
      BIND("DatatypeProperty" AS ?kind)
    }}
    FILTER(STRSTARTS(STR(?term), "https://w3id.org/mc2-center/cckp-portal/"))
    OPTIONAL {{ ?term rdfs:label ?label }}
    OPTIONAL {{ ?term rdfs:comment ?comment }}
    OPTIONAL {{ ?term rdfs:domain ?domain }}
    OPTIONAL {{ ?term rdfs:range ?range }}
  }}
}} ORDER BY ?kind ?term"""
    query = _ensure_limit(query)
    return sparql_request(query, include_default_prefixes=False)


def get_shape(params: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve the SHACL shape for one CCKP class.

    FORMERLY A KNOWN LIMITATION: cckp_portal.shacl.ttl used to have only two
    sh:targetClass-based identifying-field shapes — DatasetShape and
    GrantShape — so this function returned an empty result for
    Publication/Tool/EducationalResource. Fixed upstream in
    ../data-models/plans/cckp_shacl_shape_gaps.md, which added
    PublicationShape, ToolShape, and EducationalResourceShape (all
    sh:targetClass-based), for 5 total. NOT YET LIVE: that fix only landed
    in data-models' schema files. Per that plan's Approach §6, data-models
    is deliberately holding the rebuild/republish to SageBrain until this
    repo's graph-scoping (_resolve_cckp_graph) is confirmed live — so
    until the next dated snapshot is published, the live graph still only
    has the two old shapes and this function still returns empty for the
    other three classes. No client-side code change is needed here; the
    query already scopes correctly via sh:targetClass cckp:{class_name}.
    """
    class_name = params.get("className", "").strip()
    if not class_name:
        return {"error": "className is required"}

    if ":" in class_name:
        class_name = class_name.split(":", 1)[1]

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", class_name):
        return {"error": f"Invalid className: {class_name!r}"}

    graph_uri = _resolve_cckp_graph()
    query = f"""\
PREFIX sh: <http://www.w3.org/ns/shacl#>

SELECT ?shape ?label ?comment ?path ?datatype ?nodeKind ?class ?minCount ?maxCount
WHERE {{
  GRAPH <{graph_uri}> {{
    ?shape a sh:NodeShape ;
           sh:targetClass cckp:{class_name} .
    OPTIONAL {{ ?shape rdfs:label ?label }}
    OPTIONAL {{ ?shape rdfs:comment ?comment }}
    OPTIONAL {{
      ?shape sh:property ?prop .
      OPTIONAL {{ ?prop sh:path ?path }}
      OPTIONAL {{ ?prop sh:datatype ?datatype }}
      OPTIONAL {{ ?prop sh:nodeKind ?nodeKind }}
      OPTIONAL {{ ?prop sh:class ?class }}
      OPTIONAL {{ ?prop sh:minCount ?minCount }}
      OPTIONAL {{ ?prop sh:maxCount ?maxCount }}
    }}
  }}
}}
ORDER BY ?path
"""
    query = _ensure_limit(query)
    result = sparql_request(query, include_default_prefixes=True)
    result["className"] = class_name
    return result


def count_by_type(params: Dict[str, Any]) -> Dict[str, Any]:
    """CCKP inventory: explicit per-class counts, not an unscoped `?s a
    ?type` (which would include every other portal's classes on this
    shared store, and every historical snapshot of each)."""
    graph_uri = _resolve_cckp_graph()
    union_blocks = "\n    UNION\n    ".join(
        f'{{ ?s a cckp:{cls} . BIND("{cls}" AS ?type) }}' for cls in CCKP_CLASSES
    )
    query = f"""\
SELECT ?type (COUNT(?s) AS ?count) WHERE {{
  GRAPH <{graph_uri}> {{
    {union_blocks}
  }}
}} GROUP BY ?type ORDER BY DESC(?count)"""
    return sparql_request(query, include_default_prefixes=True)
