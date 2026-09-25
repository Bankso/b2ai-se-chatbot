import base64
import concurrent.futures
import gzip
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional


SYNAPSE_BASE_URL = os.environ.get(
    "SYNAPSE_BASE_URL", "https://repo-prod.prod.sagebase.org"
)
# Optional: the tables below are all open-access TableEntities and can be
# queried anonymously. Set SYNAPSE_AUTH_TOKEN only if a future table added to
# TABLES requires authentication.
SYNAPSE_AUTH_TOKEN = os.environ.get("SYNAPSE_AUTH_TOKEN", "")

# LAMBDA_TIMEOUT should match the Lambda function's configured timeout (seconds).
# QUERY_TIMEOUT is kept shorter so the Lambda always has time to return a clean
# error response before AWS forcibly kills the invocation (which causes a 424).
_LAMBDA_TIMEOUT = int(os.environ.get("LAMBDA_TIMEOUT", "30"))
QUERY_TIMEOUT = int(os.environ.get("QUERY_TIMEOUT", str(max(_LAMBDA_TIMEOUT - 5, 5))))

# A single per-invocation deadline (module global, set by `lambda_handler` at
# the start of each invocation via `_start_invocation_budget`) so QUERY_TIMEOUT
# is a budget for the *whole* invocation, not a fresh allowance handed out to
# every network call. Without this, `_request`'s own urlopen timeout and
# `_run_query`'s poll deadline could each independently spend up to
# QUERY_TIMEOUT seconds, letting a single query use roughly 2x the intended
# budget and overrun the Lambda's real (LAMBDA_TIMEOUT-second) timeout.
# `_remaining_budget` is what every timeout/poll-deadline computation should
# call, rather than referencing QUERY_TIMEOUT directly.
_INVOCATION_DEADLINE: Optional[float] = None


def _start_invocation_budget() -> None:
    """Start a fresh QUERY_TIMEOUT-second budget for this invocation."""
    global _INVOCATION_DEADLINE
    _INVOCATION_DEADLINE = time.monotonic() + QUERY_TIMEOUT


def _end_invocation_budget() -> None:
    """Clear the invocation deadline (called once the response is ready)."""
    global _INVOCATION_DEADLINE
    _INVOCATION_DEADLINE = None


def _remaining_budget() -> float:
    """Seconds left in the current invocation's query-timeout budget.

    When no invocation deadline is set -- i.e. `_start_invocation_budget`
    hasn't been called, which is the case for any direct call to a query
    helper from outside `lambda_handler` (unit tests, the benchmark scripts
    that import this module) -- this falls back to a fresh QUERY_TIMEOUT-
    second budget on every call, so those callers keep working unchanged.
    """
    if _INVOCATION_DEADLINE is None:
        return float(QUERY_TIMEOUT)
    return max(0.0, _INVOCATION_DEADLINE - time.monotonic())

# Confirmed Bridge2AI Standards Explorer denormalized tables in Synapse
# project syn63096806 ("standards-data"). Verified live against the Synapse
# REST API (entity name + column list) on 2026-09-24.
#
# PINNED to the exact versions the live portal queries (confirmed against
# Sage-Bionetworks/synapse-web-monorepo,
# apps/portals/b2ai.standards/src/config/resources.ts on main, 2026-09-24) so
# an answer computed here always matches what a Detail Page currently shows,
# rather than silently drifting if the underlying table is edited after this
# pin was taken. Re-pin whenever the portal bumps its pins --
# `scripts/check_table_pins.py` diffs these values against resources.ts and
# exits non-zero on drift. Row counts below are for the *pinned version*, not
# necessarily the table's current HEAD (which can differ -- e.g. `standards`
# HEAD had 1156 rows on 2026-09-24 but the pinned .99 snapshot has 1029).
#
# Values carry the version suffix (e.g. "syn65676531.99") because the SQL
# `FROM` clause needs the version to pin a query to that snapshot. The
# Synapse table-query REST API's *entity path* (`/entity/{id}/table/query/...`)
# takes the bare, unversioned synId -- see `_bare_id()`, used everywhere a
# syn_id from this table is turned into a request URL.
TABLES = {
    "standards": "syn65676531.99",  # DST_denormalized (1029 rows at this pin)
    "datasets": "syn68258237.15",  # DataSet_denormalized (116 rows at this pin)
    "organizations": "syn69693360.32",  # Organization_denormalized (130 rows at this pin)
    "topics": "syn75081383.8",  # DataTopic_denormalized (82 rows at this pin)
    "substrates": "syn63096834.32",  # DataSubstrate (81 rows at this pin)
    "manifest": "syn72106735.21",  # Manifest (70 rows at this pin)
    "d4d": "syn68885644.13",  # D4D_content (5 rows: 4 html + 1 css)
}

# The 4 Bridge2AI Grand Challenge orgs that have a D4D (Datasheet for
# Dataset) -- confirmed against resources.ts's GC_ORG_IDS and live against
# organizations/D4D_content. Order matches resources.ts.
GC_ORG_IDS = ["B2AI_ORG:114", "B2AI_ORG:115", "B2AI_ORG:116", "B2AI_ORG:117"]

_VERSION_SUFFIX_RE = re.compile(r"\.(\d+)$")


def _bare_id(syn_id: str) -> str:
    """Strip a trailing '.N' version suffix, e.g. "syn123.45" -> "syn123".

    The Synapse table-query REST API's entity path takes the bare, unversioned
    synId -- confirmed live: POSTing to
    `/entity/{bareId}/table/query/async/start` with a *versioned* SQL `FROM`
    clause (and a bare `entityId` in the request body) returns rows/columns
    for that exact pinned version, matching the portal. (Synapse also happens
    to tolerate a dotted id in the entity path itself, but this Lambda
    follows the documented bare-id contract rather than relying on that
    leniency.)
    """
    return _VERSION_SUFFIX_RE.sub("", syn_id)

# Confirmed portal Detail Page routes (Sage-Bionetworks/synapse-web-monorepo,
# apps/portals/b2ai.standards/src/config/routesConfig.tsx), and the id
# prefix each table alias's `id` column uses (confirmed via live sample rows).
# Only tables with a confirmed Detail Page route are listed -- datasets,
# substrates, and manifest have no confirmed dedicated route (the portal's
# `/Explore` path now just redirects to `/Search`, and only the Standards
# search tab exists there; see build_portal_url's docstring).
PORTAL_ROUTES = {
    "standards": {
        "path": "/Explore/Standard/DetailsPage",
        "id_prefix": "B2AI_STANDARD:",
    },
    "organizations": {
        "path": "/Explore/Organization/OrganizationDetailsPage",
        "id_prefix": "B2AI_ORG:",
    },
    "topics": {
        "path": "/Explore/DataTopic/DetailsPage",
        "id_prefix": "B2AI_TOPIC:",
    },
}

# The facet columns shown on /Search/Standards -- confirmed live against
# Sage-Bionetworks/synapse-web-monorepo,
# apps/portals/b2ai.standards/src/config/synapseConfigs/searchConfig.tsx, and
# via direct Synapse API replication (see
# plans/retarget-b2ai-standards-explorer.md's "Search: RESOLVED" note). Any
# other columnName is rejected rather than silently building a facet the
# search page doesn't render/apply.
FACET_COLUMNS = (
    "applicationNames",
    "category",
    "collections",
    "dataTypes",
    "hasAIApplication",
    "isOpen",
    "mature",
    "registration",
    "relevantOrgNames",
    "topic",
    "topicDescription",
    "usedInBridge2AI",
)

# partMask bits (Synapse): query results = 0x1, count = 0x2, select columns = 0x4
PART_RESULTS = 0x1
PART_COUNT = 0x2
PART_SELECT_COLUMNS = 0x4

DEFAULT_LIMIT = 25
MAX_LIMIT = 200


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
    Lambda handler for exposing the b2ai_rag SQL helper functions to an agent.

    Queries the Bridge2AI Standards Explorer's curated Synapse denormalized
    tables (standards, datasets, organizations, topics, substrates, manifest,
    d4d) directly via the Synapse table-query REST API, plus a D4D
    (Datasheets for Datasets) exploration sub-routine (listD4Ds/getD4D/
    searchD4D) that parses the d4d table's HTML rows into pageable sections.
    The entire body is wrapped in a top-level try/except so that a
    well-formed response is *always* returned; an unhandled exception would
    cause an AWS invocation failure, which Bedrock surfaces as a 424 error.

    Also starts (and, in a finally, clears) this invocation's query-timeout
    budget -- see `_start_invocation_budget`/`_remaining_budget` -- so every
    Synapse call made while handling this event shares one QUERY_TIMEOUT-
    second budget instead of each independently getting a fresh one.
    """
    action_group = api_path = http_method = None
    _start_invocation_budget()
    try:
        print(f"Received event: {json.dumps(event)}")

        action_group = event.get("actionGroup")
        api_path = event.get("apiPath", "")
        http_method = event.get("httpMethod", "POST")
        function = map_api_path_to_function(api_path) or event.get("function")
        params = extract_params(event)

        print(f"API path: {api_path}, function: {function}, params: {params}")

        if function == "sqlQuery":
            response_body = sql_query(params)
        elif function == "buildPortalUrl":
            response_body = build_portal_url(params)
        elif function == "getColumns":
            response_body = get_columns_fn(params)
        elif function == "countByType":
            response_body = count_by_type(params)
        elif function == "listD4Ds":
            response_body = list_d4ds(params)
        elif function == "getD4D":
            response_body = get_d4d(params)
        elif function == "searchD4D":
            response_body = search_d4d(params)
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
        _end_invocation_budget()

    print(f"Returning response: {json.dumps(response)}")
    return response


def map_api_path_to_function(api_path: str) -> Optional[str]:
    mapping = {
        "/sql-query": "sqlQuery",
        "/portal-url": "buildPortalUrl",
        "/columns": "getColumns",
        "/count-by-type": "countByType",
        "/d4d-list": "listD4Ds",
        "/d4d": "getD4D",
        "/d4d-search": "searchD4D",
    }
    return mapping.get(api_path)


def _split_top_level(text: str) -> List[str]:
    """Split `text` on commas that sit at bracket depth 0 (i.e. not nested
    inside a `[...]` or `{...}` span), so a nested array/object's own commas
    aren't mistaken for separators at this level."""
    parts = []
    depth = 0
    current: List[str] = []
    for c in text:
        if c in "[{":
            depth += 1
            current.append(c)
        elif c in "]}":
            depth -= 1
            current.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
    parts.append("".join(current))
    return [p.strip() for p in parts]


def _strip_matching_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _parse_bedrock_pseudo_json(text: str) -> Any:
    """Parse Bedrock's malformed pseudo-JSON for array/object-typed action
    group parameters: bracketed, but with bare, unquoted `key=value` pairs
    instead of valid JSON, e.g. Bedrock can send `facets` as
    "[{columnName=topic, values=[Image, Genome]}]" instead of valid JSON
    '[{"columnName": "topic", "values": ["Image", "Genome"]}]" — a quirk
    also independently reported on AWS re:Post, not something specific to
    this Lambda.

    Recurses into nested `[...]`/`{...}` spans (splitting each only on its
    own top-level commas, via `_split_top_level`) so a nested array value —
    like `values` above — gets the same treatment, not just the outermost
    one. Falls through to a bare (quote-stripped) string for anything that
    isn't itself bracketed.
    """
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_bedrock_pseudo_json(item) for item in _split_top_level(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        obj: Dict[str, Any] = {}
        for pair in _split_top_level(inner):
            key, sep, value = pair.partition("=")
            obj[key.strip()] = _parse_bedrock_pseudo_json(value) if sep else None
        return obj
    return _strip_matching_quotes(text)


def _coerce_property_value(declared_type: Optional[str], value: Any) -> Any:
    """Decode a Bedrock action-group property value into a real Python type.

    Bedrock always sends `value` as a string in the Lambda invocation event,
    even for properties whose OpenAPI schema declares `type: array` or
    `type: object` — confirmed via AWS's own docs for the action-group
    Lambda input event (agents-lambda.html), which show every `properties`
    entry as `{"name": "string", "type": "string", "value": "string"}` with
    no exception for non-scalar types.

    Worse, array/object values sometimes arrive as malformed pseudo-JSON
    (see `_parse_bedrock_pseudo_json`) instead of valid JSON. A plain
    `json.loads` alone doesn't cover that case, so this falls back to
    `_parse_bedrock_pseudo_json` — a bracket-aware manual parse — when JSON
    decoding fails.
    """
    if declared_type not in ("array", "object") or not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    if (text.startswith("[") and text.endswith("]")) or (
        text.startswith("{") and text.endswith("}")
    ):
        try:
            return _parse_bedrock_pseudo_json(text)
        except Exception:
            pass
    return value


def extract_params(event: Dict[str, Any]) -> Dict[str, Any]:
    request_body = event.get("requestBody", {})
    content = request_body.get("content", {})
    application_json = content.get("application/json", {})
    properties = application_json.get("properties", [])

    params: Dict[str, Any] = {}
    for prop in properties:
        if "name" in prop and "value" in prop:
            params[prop["name"]] = _coerce_property_value(prop.get("type"), prop["value"])

    for item in event.get("parameters", []):
        if "name" in item and "value" in item:
            params[item["name"]] = _coerce_property_value(item.get("type"), item["value"])

    return params


def _resolve_table(table: str) -> str:
    """Resolve an alias, or a synId of one of TABLES, to its pinned id.

    Only the pinned B2AI tables are queryable. A raw synId is accepted only
    if its bare id belongs to TABLES, and always resolves to the *pinned*
    version, so neither an arbitrary Synapse table nor an unpinned version
    can be reached this way.
    """
    if table in TABLES:
        return TABLES[table]
    if isinstance(table, str) and table.startswith("syn"):
        bare = _bare_id(table)
        for pinned in TABLES.values():
            if _bare_id(pinned) == bare:
                return pinned
    raise ValueError(
        f"Unknown table {table!r}. Use one of: {', '.join(TABLES)}"
    )


_SQL_SYN_ID_RE = re.compile(r"\bsyn\d+(?:\.\d+)?\b", re.IGNORECASE)


class _SqlScanError(Exception):
    """Raised by `_sql_strip_string_literals` for SQL this Lambda refuses to
    reason about (unterminated quotes, or comments that could hide text)."""


def _scan_quoted_span(sql: str, start: int, quote_char: str) -> int:
    """Scan a quoted span whose opening `quote_char` is at `sql[start]`.

    Returns the index just past the matching closing quote. A doubled quote
    char inside the span (`''`, `""`, or ` `` `) is the standard SQL escape
    for a literal quote character and does not close the span. Raises
    `_SqlScanError` if the span is never closed.
    """
    n = len(sql)
    j = start + 1
    while j < n:
        if sql[j] == quote_char:
            if j + 1 < n and sql[j + 1] == quote_char:
                j += 2
                continue
            return j + 1
        j += 1
    raise _SqlScanError(f"unterminated {quote_char!r}-quoted span")


def _sql_strip_string_literals(sql: str) -> str:
    """Left-to-right scan of `sql` that blanks out single-quoted string
    literal contents, leaving everything else -- including double-quoted
    and backtick-quoted identifiers -- intact for the synId check below.

    A single global regex for "strip quoted spans" can't tell a single-quote
    string literal from a single-quote character that merely appears inside
    a *double*-quoted identifier (e.g. `"a'"`); that stray quote can pair
    with an unrelated later quote and hide a real synId in between
    (confirmed exploit: `SELECT "a'" FROM syn99999 WHERE x = 'y'`, where the
    old regex-based strip paired the `'` in `"a'"` with the `'` opening
    `'y'` and hid `syn99999` in the "literal" it thought that formed).
    Tracking quote state explicitly avoids that: only a `'` seen outside any
    other quoted span opens a real string literal.

    Double-quoted and backtick-quoted identifiers are intentionally left in
    place (not blanked) rather than stripped, because a quoted identifier
    can itself name a table (e.g. `FROM "syn99999"`), so its synId must
    still be checked. Only single-quoted string literals are exempt.

    Also raises `_SqlScanError` on a SQL comment (`--` or `/* */`), which
    could hide a table reference from a naive substring/regex check either
    way, and on any unterminated quote.
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            end = _scan_quoted_span(sql, i, "'")
            out.append(" " * (end - i))
            i = end
        elif c == '"':
            end = _scan_quoted_span(sql, i, '"')
            out.append(sql[i:end])
            i = end
        elif c == "`":
            end = _scan_quoted_span(sql, i, "`")
            out.append(sql[i:end])
            i = end
        elif sql.startswith("--", i):
            raise _SqlScanError("SQL comments ('--') are not allowed")
        elif sql.startswith("/*", i):
            raise _SqlScanError("SQL comments ('/* */') are not allowed")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _check_sql_tables(sql: str, syn_id: str) -> Optional[str]:
    """Return an error if the SQL references any table other than syn_id.

    Synapse executes whatever table the SQL's FROM clause names, regardless
    of the entity id in the request path (confirmed live: a query sent to
    syn65676531's endpoint with `FROM syn68258237` returned that table's
    rows). So the SQL itself must be checked: every synId outside a
    single-quoted string literal (including one inside a double-quoted or
    backtick-quoted identifier) must be exactly the resolved, pinned id.
    """
    try:
        unquoted = _sql_strip_string_literals(sql)
    except _SqlScanError as e:
        return f"Invalid SQL: {e}"
    for ref in _SQL_SYN_ID_RE.findall(unquoted):
        if ref.lower() != syn_id.lower():
            return (
                f"SQL references {ref!r}, but queries may only read the "
                f"resolved table {syn_id!r}. Use the literal {{table}} "
                "placeholder in the FROM clause."
            )
    return None


def _parse_response_body(text: str) -> Any:
    """Best-effort JSON decode.

    Falls back to a raw-text wrapper on non-JSON/malformed bodies so callers
    can always call .get() on the result without raising.

    `strict=False` is required, not cosmetic: confirmed live against
    `syn68885644.13` (D4D_content), Synapse's table-query response embeds
    `content_text` (raw D4D HTML) with literal unescaped control characters
    (e.g. raw newlines) inside the JSON string, which is invalid per strict
    JSON (RFC 8259) even though it's what the server actually sends. Under
    strict mode, `json.loads` raises on that response and every D4D query
    silently degrades into the `{"raw": text}` fallback below, breaking the
    D4D sub-routine's HTML parsing. `strict=False` accepts the literal
    control characters instead.
    """
    if not text:
        return {}
    try:
        return json.loads(text, strict=False)
    except ValueError:
        return {"raw": text}


def _request(method: str, url: str, body: Optional[dict] = None):
    """Issue one HTTP request, timing it out at whatever's left of the
    current invocation's query-timeout budget (`_remaining_budget`) rather
    than a fresh QUERY_TIMEOUT every call -- see the `_INVOCATION_DEADLINE`
    module comment for why that matters."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    # These tables are open-access and queryable anonymously; only send an
    # Authorization header when a token is actually configured, since Synapse
    # rejects a malformed/empty Bearer header rather than treating it the
    # same as no header at all.
    if SYNAPSE_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {SYNAPSE_AUTH_TOKEN}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    remaining = _remaining_budget()
    if remaining <= 0:
        raise TimeoutError(
            f"Synapse query timed out after {QUERY_TIMEOUT}s (invocation budget exhausted)"
        )
    try:
        with urllib.request.urlopen(req, timeout=remaining) as resp:
            return resp.status, _parse_response_body(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return e.code, _parse_response_body(detail)
    except (urllib.error.URLError, socket.timeout) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TimeoutError(f"Synapse query timed out after {QUERY_TIMEOUT}s")
        raise Exception(f"Request failed: {e}")


def _run_query(syn_id: str, sql: str, limit: int, part_mask: int) -> dict:
    """Start an async table query against one synId, poll, return the raw
    bundle. Both the start/poll requests (via `_request`) and the poll
    deadline/sleep interval below are bounded by `_remaining_budget()`, the
    shared per-invocation timeout budget, not an independent QUERY_TIMEOUT
    each."""
    start_url = f"{SYNAPSE_BASE_URL}/repo/v1/entity/{syn_id}/table/query/async/start"
    body = {
        "concreteType": "org.sagebionetworks.repo.model.table.QueryBundleRequest",
        "entityId": syn_id,
        "query": {"sql": sql, "limit": limit},
        "partMask": part_mask,
    }
    status, resp = _request("POST", start_url, body)
    if status not in (200, 201) or not isinstance(resp, dict) or "token" not in resp:
        raise Exception(f"query start failed (HTTP {status}): {resp}")
    token = resp["token"]

    get_url = f"{SYNAPSE_BASE_URL}/repo/v1/entity/{syn_id}/table/query/async/get/{token}"
    while True:
        status, resp = _request("GET", get_url)
        if status in (200, 201):
            return resp
        if status == 202:  # still processing
            remaining = _remaining_budget()
            if remaining <= 0:
                raise TimeoutError("Synapse query is still running")
            time.sleep(min(1.0, remaining))
            continue
        raise Exception(f"query failed (HTTP {status}): {resp}")


def _parse_bundle(bundle: dict) -> dict:
    """Flatten a QueryResultBundle into {count, headers, rows[dict]}."""
    qr = ((bundle or {}).get("queryResult") or {}).get("queryResults") or {}
    headers = [h.get("name") for h in qr.get("headers", [])]
    rows = []
    for row in qr.get("rows", []):
        record = dict(zip(headers, row.get("values", [])))
        rows.append(record)
    return {"count": bundle.get("queryCount"), "headers": headers, "rows": rows}


def _clamp_limit(limit: Any) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def sql_query(params: Dict[str, Any]) -> Dict[str, Any]:
    table = params.get("table")
    sql = params.get("sql")
    if not table:
        return {"error": "table is required"}
    if not sql:
        return {"error": "sql is required"}

    try:
        syn_id = _resolve_table(table)
    except ValueError as e:
        return {"error": str(e)}

    sql = sql.replace("{table}", syn_id)
    error = _check_sql_tables(sql, syn_id)
    if error:
        return {"error": error}
    limit = _clamp_limit(params.get("limit", DEFAULT_LIMIT))
    bundle = _run_query(_bare_id(syn_id), sql, limit, PART_RESULTS | PART_COUNT)
    return _parse_bundle(bundle)


def build_portal_url(params: Dict[str, Any]) -> Dict[str, Any]:
    """Build a working Bridge2AI Standards Explorer portal path.

    Returns a *relative* path (e.g. "/Explore/Standard/DetailsPage?id=..."),
    matching every other redirect target this agent has ever used — a plain
    literal Collection/Detail Page path is always relative, never a full URL
    with a domain. The chat frontend's redirect handler expects `<target>` to
    be a path it resolves against its own base URL, not an already-absolute
    URL to use as-is.

    Confirmed routes (Sage-Bionetworks/synapse-web-monorepo,
    apps/portals/b2ai.standards/src/config/routesConfig.tsx):
      - /Explore/Standard/DetailsPage?id=<id>            (id like B2AI_STANDARD:1)
      - /Explore/Organization/OrganizationDetailsPage?id=<id>  (id like B2AI_ORG:1)
      - /Explore/DataTopic/DetailsPage?id=<id>           (id like B2AI_TOPIC:1)
      - /Search/Standards                                (search tab)
    `/Explore` on its own now just redirects to `/Search` (PORTALS-4227) —
    there is no confirmed Collection-page route for datasets, substrates, or
    manifest, so `resourceType` only accepts "standards", "organizations",
    "topics", or "search".

    Each Detail Page reads its id from a plain `?id=` query param (confirmed
    via useGetPortalComponentSearchParams() in StandardsDetailsPage.tsx) —
    no compression/encoding needed, unlike CCKP's `qw0` scheme. `id` is
    validated against the expected prefix for `resourceType` (e.g.
    B2AI_STANDARD: / B2AI_ORG: / B2AI_TOPIC:, confirmed against live sample
    rows) so a mismatched id/resourceType pair fails fast with a clear error
    instead of producing a dead link.

    Search deep-linking (confirmed via
    apps/synapse-portal-framework/src/components/PortalSearch/
    PortalFullTextSearchField.tsx and SearchParamAwareQueryWrapperPlotNav.tsx,
    and apps/portals/b2ai.standards/src/config/synapseConfigs/searchConfig.tsx):
    the Standards search tab (the only tab; backed by search index
    syn74909093, whose `definingSQL` covers the pinned `standards` table's
    1029 docs) reads its free-text query from a single URL param,
    `SEARCH_TERM` (e.g. "/Search/Standards?SEARCH_TERM=FHIR"), and its facet
    filters from an optional `qw0` param: urllib-quote(base64(gzip(JSON))) of
    a diff-only query object `{"selectedFacets": [FacetColumnValuesRequest,
    ...]}` -- the same encoding scheme as the CCKP Explore builder (see
    `agents/cckp-copilot/lambda/cckpSqlRag/lambda_function.py`'s
    `build_explore_url` in git history), independently confirmed working
    here via direct Synapse API replication and live browser counts.

    Search semantics -- RESOLVED via source trace, direct Synapse API
    replication, and live browser counts on 2026-09-24 (see
    plans/retarget-b2ai-standards-explorer.md's "Search: RESOLVED" note for
    the full evidence trail):
      - The request is `POST /repo/v1/search/query/async/start` with
        `{"multi_match": {"query": <SEARCH_TERM>, "fuzziness": "AUTO"}}` and
        no `operator` set. Multi-word `SEARCH_TERM` values are therefore
        OR'd/unioned across words, never ANDed: "imaging" (107 hits) +
        "genomics" (112 hits) -> "imaging genomics" (213 hits, an exact
        ID-set union; the live browser also shows 213). A second pair gave
        "FHIR" (69) + "ontology" (270) -> 326. More words in `SEARCH_TERM`
        only ever broadens the result set, never narrows it.
      - Matching is case-insensitive, stemmed (e.g. "ontologies" ~=
        "ontology" ~= "ontolog") and fuzzy (typo-tolerant), but is NOT
        prefix matching -- "onto" alone returns 0 hits. Quoted phrases and
        `synNNN`-style tokens switch the query to `simple_query_string`,
        and operators (`+`/`-`/`AND`/`OR`) aren't reliably honored either
        way -- never tell a user that quoting or operators will narrow a
        search.
      - Facets AND across columns, OR within one column's values, and a
        search term ANDs against the combined facets. Live-verified:
        `topic=[Image]` alone -> 67 hits; `topic=[Image]` +
        `category=[Ontology or Vocabulary]` -> 1 hit (AND across columns;
        a direct SQL `HAS`/`=` query over the pinned `standards` table also
        gives 1); `topic=[Image, Genome]` -> 121 hits (OR within one
        column's values; SQL `HAS` also gives 121);
        `SEARCH_TERM=segmentation` (16 alone) + `topic=[Image]` -> 3 hits
        (term AND facets).
      - Practical implication: to require several concepts at once, use one
        precise search term plus one or more facets (or several facets on
        different columns) -- never rely on stacking multiple words into
        `SEARCH_TERM`, since that only broadens.

    `searchTerm` is a single free-text string (not a list). `facets` is an
    optional list of `{"columnName": str, "values": [str, ...]}` objects:
    `columnName` must be one of the facet columns shown on the page (see
    `FACET_COLUMNS`, e.g. "topic", "category") and `values` must be a
    non-empty list of non-empty strings; an unknown `columnName` or a bad
    `values` shape is rejected with an explicit error rather than silently
    building a facet the page won't apply. Duplicate `columnName` entries
    across the list are merged (their `values` unioned) rather than
    rejected, so a caller assembling facets incrementally doesn't have to
    pre-deduplicate them.

    When `facets` is given, the returned `url` also carries `qw0` (see
    above), self-verified before returning by decoding it back and
    comparing to the query object just built -- the same self-verify
    pattern the CCKP builder uses, since a silent encoding bug here would
    otherwise surface only as a dead search link. `urllib.parse.urlencode`
    renders a space in `SEARCH_TERM` as "+", not "%20"; that's fine here,
    not a bug, because the portal reads `SEARCH_TERM` via a plain
    `URLSearchParams.get()` call (`searchParams.get(SEARCH_TERM)` in
    `SearchParamAwareQueryWrapperPlotNav.tsx`), and `URLSearchParams`
    decodes "+" back to a space per the WHATWG URL spec's
    application/x-www-form-urlencoded parsing, regardless of whether the
    query string was produced by an actual HTML form.
    """
    resource_type = params.get("resourceType")
    if not resource_type:
        return {"error": "resourceType is required"}

    if resource_type == "search":
        query = {}
        search_term = params.get("searchTerm")
        if search_term:
            if not isinstance(search_term, str):
                return {"error": "searchTerm must be a string"}
            query["SEARCH_TERM"] = search_term

        facets = params.get("facets")
        qw0 = None
        if facets:
            if not isinstance(facets, list):
                return {"error": "facets must be a list of {columnName, values} objects"}
            merged: Dict[str, List[str]] = {}
            for facet in facets:
                if not isinstance(facet, dict):
                    return {
                        "error": "each facets entry must be an object with columnName and values"
                    }
                column_name = facet.get("columnName")
                if column_name not in FACET_COLUMNS:
                    return {
                        "error": (
                            f"Unknown facet columnName {column_name!r}. Use one of: "
                            f"{', '.join(FACET_COLUMNS)}."
                        )
                    }
                values = facet.get("values")
                if not isinstance(values, list) or not values or not all(
                    isinstance(v, str) and v for v in values
                ):
                    return {
                        "error": (
                            f"facet {column_name!r} values must be a non-empty list "
                            "of non-empty strings"
                        )
                    }
                bucket = merged.setdefault(column_name, [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)

            selected_facets = [
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": column_name,
                    "facetValues": values,
                }
                for column_name, values in merged.items()
            ]
            facet_query = {"selectedFacets": selected_facets}
            payload = json.dumps(facet_query, separators=(",", ":")).encode("utf-8")
            # mtime=0 keeps the gzip header (and so the URL) deterministic.
            compressed = gzip.compress(payload, mtime=0)
            qw0 = urllib.parse.quote(base64.b64encode(compressed).decode("ascii"))

            # Self-verify before returning, same pattern as the CCKP builder
            # (git history: agents/cckp-copilot/lambda/cckpSqlRag/
            # lambda_function.py, build_explore_url): decode our own qw0
            # back to the query object we just built. This only catches a
            # bug in this function's own encoding (e.g. a future change to
            # the gzip/base64/urlencode steps) -- it can't catch the model
            # mangling the string afterward, since that happens downstream
            # of this return value -- but it's still worth doing: better to
            # return an explicit error here than a URL that silently
            # doesn't work.
            try:
                roundtrip = json.loads(
                    gzip.decompress(base64.b64decode(urllib.parse.unquote(qw0)))
                )
            except Exception as e:
                return {"error": f"internal error: qw0 failed to self-verify ({e})"}
            if roundtrip != facet_query:
                return {"error": "internal error: qw0 self-verification mismatch"}

        url = "/Search/Standards"
        query_parts = []
        if query:
            query_parts.append(urllib.parse.urlencode(query))
        if qw0 is not None:
            query_parts.append(f"qw0={qw0}")
        if query_parts:
            url += "?" + "&".join(query_parts)
        return {"url": url}

    if resource_type not in PORTAL_ROUTES:
        return {
            "error": (
                f"Unknown resourceType {resource_type!r}. Use one of: "
                f"{', '.join(list(PORTAL_ROUTES) + ['search'])}."
            )
        }

    entity_id = params.get("id")
    if not entity_id:
        return {"error": "id is required"}

    route = PORTAL_ROUTES[resource_type]
    if not entity_id.startswith(route["id_prefix"]):
        return {
            "error": (
                f"id {entity_id!r} does not look like a {resource_type} id "
                f"(expected it to start with {route['id_prefix']!r})."
            )
        }

    url = f"{route['path']}?id={urllib.parse.quote(entity_id, safe=":")}"
    return {"url": url}


def get_columns_fn(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the deployed column names for one table.

    The SQL's `FROM {syn_id}` carries the pinned version (when `table` is a
    known alias) so the columns reported match that exact snapshot's schema,
    not necessarily the table's current HEAD -- confirmed live that a
    versioned `FROM` combined with a bare-id entity path (`_bare_id`) returns
    `selectColumns` for that pinned version, so no separate version-aware
    branch is needed here beyond using `syn_id` (versioned) in the SQL and
    `_bare_id(syn_id)` in the request URL, same as every other query op.
    """
    table = params.get("table")
    if not table:
        return {"error": "table is required"}

    try:
        syn_id = _resolve_table(table)
    except ValueError as e:
        return {"error": str(e)}

    bundle = _run_query(
        _bare_id(syn_id), f"SELECT * FROM {syn_id} LIMIT 1", 1, PART_RESULTS | PART_SELECT_COLUMNS
    )
    columns = [c.get("name") for c in (bundle.get("selectColumns") or [])]
    if not columns:  # fall back to the result-set headers
        columns = _parse_bundle(bundle)["headers"]
    return {"table": table, "columns": columns}


def count_by_type(params: Dict[str, Any]) -> Dict[str, Any]:
    """Count rows in every table, one query per table, run concurrently.

    The 7 tables were previously counted one at a time, so this function
    alone could burn up to 7x a single query's share of the invocation's
    timeout budget. A `ThreadPoolExecutor` (stdlib -- the Lambda stays
    single-file/no third-party deps) runs them in parallel instead; each
    still shares the same `_remaining_budget()` invocation deadline, and a
    per-table failure is still reported individually rather than failing the
    whole call.
    """
    counts = {}
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TABLES)) as pool:
        future_to_alias = {
            pool.submit(_run_query, _bare_id(syn_id), f"SELECT * FROM {syn_id}", 1, PART_COUNT): alias
            for alias, syn_id in TABLES.items()
        }
        for future in concurrent.futures.as_completed(future_to_alias):
            alias = future_to_alias[future]
            try:
                bundle = future.result()
                counts[alias] = bundle.get("queryCount")
            except Exception as e:
                errors[alias] = str(e)
    result = {"counts": counts}
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# D4D (Datasheets for Datasets) exploration sub-routine
# ---------------------------------------------------------------------------
#
# D4D_content (syn68885644, pinned .13) has one HTML row per Bridge2AI Grand
# Challenge org (content_id = the org's `id`, e.g. "B2AI_ORG:114") plus one
# CSS row (content_id is null) that this Lambda ignores. Each HTML document
# is 38-60 KB -- too large to return whole through a Bedrock action group
# (~25 KB response limit) -- so it's parsed once into named sections that can
# be paged individually.
#
# Real HTML structure, confirmed live against all 4 GC rows on 2026-09-24
# (this is NOT the generic Datasheets-for-Datasets heading set the plan
# guessed at -- there is no "Preprocessing" heading, and there is an extra
# "Human Subjects" heading):
#
#   <h1>{Consortium} Dataset Documentation</h1>
#   <div class="section">                          (one per top-level heading)
#     <h2 class="section-title">Motivation</h2>          (also: Composition,
#     <p class="section-description">...</p>              Collection Process,
#     <div class="section-content">                       Uses, Distribution,
#       <div class="data-item">                            Maintenance,
#         <label class="item-label required-field|optional-field">          Human Subjects)
#           {Field label}
#           <span class="required-indicator">*</span>  (only on required fields)
#         </label>
#         <div class="item-value">{scalar text | <a href> | <table class="data-table">
#           with <thead><tr><th>...</th></tr></thead><tbody><tr><td>...} |
#           <ul|ol class="formatted-list"><li>{text | <dl class="nested-dict">
#           <dt>Key</dt><dd>Value</dd>...</dl>}</li></ul>}
#         </div>
#       </div>
#       ...
#     </div>
#   </div>
#   ...
#
# There are no subsection headings (h3+) in the real data -- each section is
# a flat list of labeled fields -- so a section's stable id is simply a slug
# of its own heading (e.g. "collection-process"), not a multi-level path.

D4D_SECTION_BUDGET = 15000  # chars per getD4D(section=...) response
D4D_SNIPPET_CHARS = 300
D4D_MAX_SNIPPETS = 20

# Cold-start caches, populated on first use and reused across warm Lambda
# invocations (module globals persist between invocations in the same
# execution environment).
_D4D_DOCS: Dict[str, Dict[str, Any]] = {}  # orgId -> {"title": ..., "sections": [...]}
_D4D_ORG_NAMES: Dict[str, str] = {}  # orgId -> org name
_D4D_LOADED = False


class _D4DNode:
    """A minimal HTML element node: a tag, its attributes, and its children
    (each child is either another _D4DNode or a str of literal text)."""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: List[Any] = []


class _D4DTreeBuilder(HTMLParser):
    """Builds a lightweight DOM tree from D4D HTML using only the stdlib.

    Not a general-purpose HTML5 parser -- it never needs to be, since the D4D
    documents are machine-generated with a fixed, well-formed structure (see
    the module comment above). `<script>`/`<style>` content is dropped
    entirely per the plan ("drop script/style").
    """

    _VOID = {"br", "img", "hr", "meta", "link", "input"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _D4DNode("root", [])
        self._stack = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        node = _D4DNode(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self._skip_depth or tag in self._SKIP:
            return
        self._stack[-1].children.append(_D4DNode(tag, attrs))

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        # Close back to the nearest matching open tag (tolerates any
        # unclosed/mismatched tags rather than raising).
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._stack[-1].children.append(data)


def _d4d_has_class(node: Any, cls: str) -> bool:
    if isinstance(node, str):
        return False
    return cls in (node.attrs.get("class") or "").split()


def _d4d_find_all(node: _D4DNode, tag: str, _out=None) -> List[_D4DNode]:
    """Recursively find every descendant with the given tag name."""
    out = [] if _out is None else _out
    for c in node.children:
        if not isinstance(c, str):
            if c.tag == tag:
                out.append(c)
            _d4d_find_all(c, tag, out)
    return out


def _d4d_find_all_class(node: _D4DNode, tag: str, cls: str, _out=None) -> List[_D4DNode]:
    """Recursively find descendants with the given tag+class, not descending
    further into a match (so nested same-class elements aren't double-counted)."""
    out = [] if _out is None else _out
    for c in node.children:
        if not isinstance(c, str):
            if c.tag == tag and _d4d_has_class(c, cls):
                out.append(c)
            else:
                _d4d_find_all_class(c, tag, cls, out)
    return out


def _d4d_text_of(node: Any) -> str:
    """Flatten all text in a node's subtree into one whitespace-normalized string."""
    parts: List[str] = []

    def walk(n):
        if isinstance(n, str):
            parts.append(n)
        else:
            for c in n.children:
                walk(c)

    walk(node)
    return " ".join("".join(parts).split())


def _d4d_label_text(label_node: _D4DNode) -> str:
    """Text of an <label class="item-label ..."> excluding the "*" required
    indicator span, e.g. "ID *" -> "ID"."""
    parts: List[str] = []
    for c in label_node.children:
        if isinstance(c, str):
            parts.append(c)
        elif _d4d_has_class(c, "required-indicator"):
            continue
        else:
            parts.append(_d4d_text_of(c))
    return " ".join(" ".join(parts).split())


def _d4d_render_table(n: _D4DNode) -> str:
    """Render a <table class="data-table"> as one "- Header: value; ..." line
    per data row (header row from <th>, values from each <td> row)."""
    header_cells = [_d4d_text_of(th) for th in _d4d_find_all(n, "th")]
    lines = []
    for tr in _d4d_find_all(n, "tr"):
        tds = _d4d_find_all(tr, "td")
        if not tds:  # header row, already captured via <th> above
            continue
        cells = [_d4d_render_children(td.children).strip() for td in tds]
        if header_cells and len(header_cells) == len(cells):
            row = "; ".join(f"{h}: {v}" for h, v in zip(header_cells, cells) if v)
        else:
            row = "; ".join(v for v in cells if v)
        lines.append(f"- {row}")
    return "\n".join(lines) + ("\n" if lines else "")


def _d4d_render_dl_inline(dl: _D4DNode) -> str:
    """Render a <dl class="nested-dict"> as "Key: value; Key2: value2"."""
    dts = _d4d_find_all(dl, "dt")
    dds = _d4d_find_all(dl, "dd")
    pairs = [
        f"{_d4d_text_of(dt)}: {_d4d_render_children(dd.children).strip()}"
        for dt, dd in zip(dts, dds)
    ]
    return "; ".join(pairs)


def _d4d_render_list(n: _D4DNode) -> str:
    """Render a <ul|ol class="formatted-list"> as a "- " bullet per <li>,
    with a nested <dl> rendered inline on that bullet's line."""
    lines = []
    for li in n.children:
        if isinstance(li, str) or li.tag != "li":
            continue
        dls = [c for c in li.children if not isinstance(c, str) and c.tag == "dl"]
        if dls:
            for dl in dls:
                lines.append(f"- {_d4d_render_dl_inline(dl)}")
        else:
            text = _d4d_render_children(li.children).strip()
            if text:
                lines.append(f"- {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _d4d_render_children(children: List[Any]) -> str:
    return "".join(_d4d_render_node(c) for c in children)


def _d4d_render_node(n: Any) -> str:
    """Render one node (and its subtree) to readable text/markdown.

    Keeps link text (as a markdown link), converts tables/lists to simple
    rows, and falls through to plain concatenated text for everything else.
    """
    if isinstance(n, str):
        return n
    tag = n.tag
    if tag == "a":
        href = n.attrs.get("href", "")
        text = _d4d_text_of(n)
        return f"[{text}]({href})" if href else text
    if tag == "table":
        return _d4d_render_table(n)
    if tag in ("ul", "ol"):
        return _d4d_render_list(n)
    if tag == "dl":
        return _d4d_render_dl_inline(n) + "\n"
    if tag == "br":
        return "\n"
    if tag in ("div", "p"):
        return _d4d_render_children(n.children) + "\n"
    return _d4d_render_children(n.children)


def _d4d_slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _parse_d4d_document(html_text: str) -> Dict[str, Any]:
    """Parse one D4D HTML document into {"title": str, "sections": [...]}.

    Each section is {"id", "heading", "description", "text"} where `text` is
    the section's full rendered content (heading/description not repeated
    inside `text` beyond the description line, so callers can present the
    heading themselves).
    """
    builder = _D4DTreeBuilder()
    builder.feed(html_text)
    root = builder.root

    h1s = _d4d_find_all(root, "h1")
    title = _d4d_text_of(h1s[0]) if h1s else ""

    sections = []
    for sec in _d4d_find_all_class(root, "div", "section"):
        h2s = _d4d_find_all_class(sec, "h2", "section-title")
        heading = _d4d_text_of(h2s[0]) if h2s else "Untitled"
        desc_nodes = _d4d_find_all_class(sec, "p", "section-description")
        description = _d4d_text_of(desc_nodes[0]) if desc_nodes else ""

        lines = []
        if description:
            lines.append(description)
            lines.append("")
        for item in _d4d_find_all_class(sec, "div", "data-item"):
            labels = _d4d_find_all_class(item, "label", "item-label")
            label = _d4d_label_text(labels[0]) if labels else ""
            values = _d4d_find_all_class(item, "div", "item-value")
            value = _d4d_render_children(values[0].children).strip() if values else ""
            if not label and not value:
                continue
            if label:
                lines.append(f"**{label}**")
            lines.append(value)
            lines.append("")
        text = "\n".join(lines).strip() + "\n"

        sections.append({
            "id": _d4d_slugify(heading),
            "heading": heading,
            "description": description,
            "text": text,
        })

    return {"title": title, "sections": sections}


def _ensure_d4d_loaded() -> None:
    """Fetch and parse every D4D HTML row once per warm Lambda container.

    `_D4D_LOADED` is only set once at least one row parsed into a usable
    document -- never unconditionally after the fetch. Setting it
    unconditionally would poison the warm-container cache: a transient
    Synapse hiccup (or a query that simply comes back with zero/unusable
    rows) would still mark D4D data as "loaded", and every D4D call for the
    rest of that container's lifetime would then see an empty `_D4D_DOCS`
    and report "no content available" instead of ever retrying.
    """
    global _D4D_LOADED
    if _D4D_LOADED:
        return
    syn_id = TABLES["d4d"]
    sql = f"SELECT content_id, content_type, content_text FROM {syn_id} WHERE content_type = 'html'"
    bundle = _run_query(_bare_id(syn_id), sql, len(GC_ORG_IDS) + 1, PART_RESULTS)
    loaded_any = False
    for row in _parse_bundle(bundle)["rows"]:
        org_id = row.get("content_id")
        html_text = row.get("content_text")
        if not org_id or not html_text:
            continue
        _D4D_DOCS[org_id] = _parse_d4d_document(html_text)
        loaded_any = True
    if loaded_any:
        _D4D_LOADED = True


def _ensure_d4d_org_names_loaded() -> None:
    """Fetch the GC orgs' display names from the organizations table once.

    Same cache-poisoning concern as `_ensure_d4d_loaded`: the "already
    loaded" check must only be satisfied once at least one usable name row
    has actually been cached. This is naturally the case here already --
    the guard is the cache dict's own truthiness, not a separate flag set
    unconditionally -- but is kept explicit (`loaded_any`) rather than
    relying on that as an implementation detail, so an empty/unusable
    response still leaves `_D4D_ORG_NAMES` empty and a later call retries.
    """
    if _D4D_ORG_NAMES:
        return
    syn_id = TABLES["organizations"]
    id_list = ", ".join(f"'{oid}'" for oid in GC_ORG_IDS)
    sql = f"SELECT id, name FROM {syn_id} WHERE id IN ({id_list})"
    bundle = _run_query(_bare_id(syn_id), sql, len(GC_ORG_IDS), PART_RESULTS)
    for row in _parse_bundle(bundle)["rows"]:
        org_id = row.get("id")
        if org_id:
            _D4D_ORG_NAMES[org_id] = row.get("name") or org_id


def _d4d_org_link(org_id: str) -> str:
    return f"/Explore/Organization/OrganizationDetailsPage?id={org_id}"


def list_d4ds(params: Dict[str, Any]) -> Dict[str, Any]:
    """The Grand Challenge orgs that have a D4D: id, name, detail-page link."""
    try:
        _ensure_d4d_loaded()
        _ensure_d4d_org_names_loaded()
    except Exception as e:
        return {"error": f"Failed to load D4D data: {e}"}

    items = []
    for org_id in GC_ORG_IDS:
        doc = _D4D_DOCS.get(org_id)
        if not doc or not doc.get("sections"):
            continue
        items.append({
            "orgId": org_id,
            "name": _D4D_ORG_NAMES.get(org_id, org_id),
            "title": doc.get("title", ""),
            "link": _d4d_org_link(org_id),
        })
    return {"d4ds": items}


def get_d4d(params: Dict[str, Any]) -> Dict[str, Any]:
    """Without `section`: an outline (title + section ids/headings/sizes).
    With `section`: that section's text, paged under D4D_SECTION_BUDGET."""
    org_id = params.get("orgId")
    if not org_id:
        return {"error": "orgId is required"}

    try:
        _ensure_d4d_loaded()
        _ensure_d4d_org_names_loaded()
    except Exception as e:
        return {"error": f"Failed to load D4D data: {e}"}

    if org_id not in GC_ORG_IDS:
        return {
            "error": (
                f"Unknown orgId {org_id!r}. Grand Challenge orgs with a D4D: "
                f"{', '.join(GC_ORG_IDS)}."
            )
        }

    doc = _D4D_DOCS.get(org_id)
    if not doc or not doc.get("sections"):
        return {"error": f"No D4D content is available for {org_id}."}

    org_name = _D4D_ORG_NAMES.get(org_id, org_id)
    org_link = _d4d_org_link(org_id)

    section_id = params.get("section")
    if not section_id:
        return {
            "orgId": org_id,
            "orgName": org_name,
            "orgLink": org_link,
            "title": doc.get("title", ""),
            "sections": [
                {
                    "id": s["id"],
                    "heading": s["heading"],
                    "description": s.get("description", ""),
                    "size": len(s["text"]),
                }
                for s in doc["sections"]
            ],
        }

    section = next((s for s in doc["sections"] if s["id"] == section_id), None)
    if section is None:
        valid = ", ".join(s["id"] for s in doc["sections"])
        return {"error": f"Unknown section {section_id!r} for {org_id}. Valid sections: {valid}."}

    offset = params.get("offset") or 0
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    text = section["text"]
    chunk = text[offset:offset + D4D_SECTION_BUDGET]
    result = {
        "orgId": org_id,
        "orgName": org_name,
        "orgLink": org_link,
        "section": section_id,
        "heading": section["heading"],
        "text": chunk,
        "totalLength": len(text),
    }
    next_offset = offset + len(chunk)
    if next_offset < len(text):
        result["nextOffset"] = next_offset
    return result


def search_d4d(params: Dict[str, Any]) -> Dict[str, Any]:
    """Case-insensitive keyword search across one or all D4Ds, returning at
    most D4D_MAX_SNIPPETS section-labelled snippets."""
    query = params.get("query")
    if not query or not isinstance(query, str):
        return {"error": "query is required"}

    try:
        _ensure_d4d_loaded()
        _ensure_d4d_org_names_loaded()
    except Exception as e:
        return {"error": f"Failed to load D4D data: {e}"}

    org_id = params.get("orgId")
    if org_id and org_id not in GC_ORG_IDS:
        return {
            "error": (
                f"Unknown orgId {org_id!r}. Grand Challenge orgs with a D4D: "
                f"{', '.join(GC_ORG_IDS)}."
            )
        }
    target_orgs = [org_id] if org_id else GC_ORG_IDS

    needle = query.lower()
    hits = []
    for oid in target_orgs:
        doc = _D4D_DOCS.get(oid)
        if not doc:
            continue
        for section in doc.get("sections", []):
            text = section["text"]
            lower = text.lower()
            start = 0
            while len(hits) < D4D_MAX_SNIPPETS:
                idx = lower.find(needle, start)
                if idx == -1:
                    break
                snippet_start = max(0, idx - 80)
                snippet_end = min(len(text), idx + len(needle) + 220)
                snippet = text[snippet_start:snippet_end].strip()[:D4D_SNIPPET_CHARS]
                hits.append({
                    "orgId": oid,
                    "orgName": _D4D_ORG_NAMES.get(oid, oid),
                    "section": section["id"],
                    "heading": section["heading"],
                    "snippet": snippet,
                })
                start = idx + max(len(needle), 1)
            if len(hits) >= D4D_MAX_SNIPPETS:
                break
        if len(hits) >= D4D_MAX_SNIPPETS:
            break

    return {"query": query, "results": hits}
