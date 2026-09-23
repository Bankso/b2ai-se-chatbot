"""Unit tests for the cckpGraphRag Lambda function.

All SPARQL network calls are mocked so no endpoint is needed. The real
wire protocol is async submit-then-poll (JSON POST -> job_id -> GET poll
until status=="complete"), not the synchronous TSV form this Lambda
originally inherited unchanged from NF-OSI's nfGraphRag — see
plans/implement-sparql-backend.md for the full investigation.
"""

import json
import socket
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import lambda_function
from lambda_function import (
    CCKP_GRAPH_PREFIX,
    _ensure_limit,
    _make_response,
    _wrap_in_graph,
    extract_params,
    lambda_handler,
    sparql_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_event(api_path, properties=None, http_method="POST"):
    """Build a Bedrock action-group event using the apiPath style."""
    return {
        "messageVersion": "1.0",
        "actionGroup": "cckpGraphRag",
        "apiPath": api_path,
        "httpMethod": http_method,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": properties or [],
                }
            }
        },
    }


def _function_event(function, parameters=None):
    """Build a Bedrock action-group event using the function style."""
    return {
        "messageVersion": "1.0",
        "actionGroup": "cckpGraphRag",
        "function": function,
        "parameters": parameters or [],
    }


def _body(response):
    """Extract the parsed JSON body from a Lambda response dict."""
    return json.loads(
        response["response"]["responseBody"]["application/json"]["body"]
    )


TEST_GRAPH_URI = "urn:sagebrain:cckp:2026-09-15"


def _bindings_response(variables, rows):
    """Build the double-encoded {"results": "<json-string>"} shape a real
    'complete' poll response carries — `results` is itself a JSON string
    containing SPARQL 1.1 Query Results JSON."""
    sparql_json = {
        "head": {"vars": variables},
        "results": {
            "bindings": [
                {var: {"type": "literal", "value": val} for var, val in zip(variables, row)}
                for row in rows
            ]
        },
    }
    return {"job_id": "job-1", "status": "complete", "results": json.dumps(sparql_json)}


def _mock_response(payload):
    """A urlopen()-shaped MagicMock context manager returning `payload` as
    UTF-8 JSON bytes from .read(). Dunder methods (__enter__/__exit__) are
    looked up on the type for `with` statements, so a plain object with
    instance attributes named __enter__/__exit__ does NOT work — MagicMock
    handles this correctly."""
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    return mock


@pytest.fixture(autouse=True)
def _reset_graph_cache():
    """The resolved graph URI is a module-level cache — reset it between
    tests so one test's resolution doesn't leak into the next."""
    lambda_function._resolved_graph_uri = None
    lambda_function._resolved_graph_at = 0.0
    lambda_function._invocation_deadline = None
    yield
    lambda_function._resolved_graph_uri = None
    lambda_function._resolved_graph_at = 0.0
    lambda_function._invocation_deadline = None


@pytest.fixture
def resolved_graph():
    """Stub out graph resolution to a fixed URI (avoiding the extra
    submit/poll round-trip) for tests that aren't specifically exercising
    _resolve_cckp_graph's own logic. Using a fixture rather than a
    class-level @patch decorator — the latter conflicts with
    @pytest.mark.parametrize's positional-argument injection."""
    with patch("lambda_function._resolve_cckp_graph", return_value=TEST_GRAPH_URI):
        yield


# ---------------------------------------------------------------------------
# _make_response / extract_params — unchanged behavior, unchanged tests
# ---------------------------------------------------------------------------

class TestMakeResponse:
    def test_normal_body(self):
        resp = _make_response("ag", "/path", "POST", 200, {"ok": True})
        assert resp["response"]["httpStatusCode"] == 200
        assert json.loads(resp["response"]["responseBody"]["application/json"]["body"]) == {"ok": True}

    def test_non_serializable_body(self):
        resp = _make_response("ag", "/path", "POST", 200, {"bad": object()})
        body = json.loads(resp["response"]["responseBody"]["application/json"]["body"])
        assert "error" in body
        assert "serialized" in body["error"]


class TestExtractParams:
    def test_from_request_body(self):
        event = _api_event("/sparql-query", [
            {"name": "query", "type": "string", "value": "SELECT 1"},
        ])
        assert extract_params(event) == {"query": "SELECT 1"}

    def test_from_parameters_list(self):
        event = _function_event("sparqlQuery", [
            {"name": "query", "type": "string", "value": "SELECT 1"},
        ])
        assert extract_params(event) == {"query": "SELECT 1"}

    def test_empty_event(self):
        assert extract_params({}) == {}

    def test_malformed_property_raises(self):
        event = _api_event("/sparql-query", [{"wrong_key": "oops"}])
        with pytest.raises(KeyError):
            extract_params(event)


# ---------------------------------------------------------------------------
# _ensure_limit — code-level LIMIT enforcement
# ---------------------------------------------------------------------------

class TestEnsureLimit:
    def test_appends_default_when_absent(self):
        result = _ensure_limit("SELECT ?s WHERE { ?s ?p ?o }")
        assert "LIMIT 200" in result

    def test_leaves_existing_limit_alone(self):
        query = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 50"
        assert _ensure_limit(query) == query

    def test_case_insensitive_detection(self):
        query = "SELECT ?s WHERE { ?s ?p ?o } limit 10"
        assert _ensure_limit(query) == query

    def test_custom_default(self):
        result = _ensure_limit("SELECT ?s WHERE { ?s ?p ?o }", default_limit=5)
        assert "LIMIT 5" in result

    def test_subquery_limit_does_not_satisfy_outer_cap(self):
        query = "SELECT ?d WHERE { { SELECT ?d WHERE { ?d a cckp:Dataset } LIMIT 5 } UNION { ?d a cckp:Publication } }"
        result = _ensure_limit(query)
        assert result.rstrip().endswith("LIMIT 200")

    def test_oversized_limit_is_clamped(self):
        result = _ensure_limit("SELECT ?s WHERE { ?s ?p ?o } LIMIT 100000")
        assert result == "SELECT ?s WHERE { ?s ?p ?o } LIMIT 200"

    def test_limit_inserted_before_trailing_values(self):
        query = 'SELECT ?d WHERE { ?d cckp:assay ?a } VALUES ?a { "ATAC-Seq" }'
        result = _ensure_limit(query)
        assert result.index("LIMIT 200") < result.index("VALUES")

    def test_limit_inside_literal_is_ignored(self):
        query = 'SELECT ?d WHERE { ?d cckp:description "LIMIT 5" }'
        assert "LIMIT 200" in _ensure_limit(query)


# ---------------------------------------------------------------------------
# _wrap_in_graph — textual GRAPH-clause injection
# ---------------------------------------------------------------------------

class TestWrapInGraph:
    def test_simple_query(self):
        query = "SELECT ?s WHERE { ?s a cckp:Dataset }"
        wrapped = _wrap_in_graph(query, TEST_GRAPH_URI)
        assert wrapped == f"SELECT ?s WHERE {{ GRAPH <{TEST_GRAPH_URI}> {{ ?s a cckp:Dataset }} }}"

    def test_nested_braces_preserved(self):
        query = "SELECT ?s WHERE { ?s a cckp:Dataset . OPTIONAL { ?s cckp:description ?d } }"
        wrapped = _wrap_in_graph(query, TEST_GRAPH_URI)
        assert wrapped.count("{") == wrapped.count("}")
        assert f"GRAPH <{TEST_GRAPH_URI}>" in wrapped
        assert "OPTIONAL { ?s cckp:description ?d }" in wrapped

    def test_no_brace_returns_unchanged(self):
        query = "SELECT ?s"
        assert _wrap_in_graph(query, TEST_GRAPH_URI) == query

    def test_preserves_trailing_clauses(self):
        query = "SELECT ?s WHERE { ?s a cckp:Dataset } LIMIT 10"
        wrapped = _wrap_in_graph(query, TEST_GRAPH_URI)
        assert wrapped.endswith("LIMIT 10")

    def test_braces_inside_literal_do_not_end_body(self):
        query = 'SELECT ?s WHERE { ?s cckp:description ?d . FILTER(CONTAINS(?d, "}")) } LIMIT 10'
        wrapped = _wrap_in_graph(query, TEST_GRAPH_URI)
        assert wrapped == (
            f'SELECT ?s WHERE {{ GRAPH <{TEST_GRAPH_URI}> {{ ?s cckp:description ?d . '
            'FILTER(CONTAINS(?d, "}")) } } LIMIT 10'
        )


# ---------------------------------------------------------------------------
# sparql_request — async submit/poll network layer
# ---------------------------------------------------------------------------

class TestSparqlRequest:
    def _mock_urlopen_sequence(self, mock_urlopen, responses):
        """Queue a sequence of JSON payloads to return from successive
        urlopen() calls (submit, then N polls)."""
        mock_urlopen.side_effect = [_mock_response(payload) for payload in responses]

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_success_parses_bindings(self, mock_urlopen, _sleep):
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            _bindings_response(["s", "p"], [["syn1", "cckp:label"], ["syn2", "cckp:label"]]),
        ])
        result = sparql_request("SELECT ?s ?p WHERE { ?s ?p ?o }")
        assert result == {
            "headers": ["s", "p"],
            "rows": [{"s": "syn1", "p": "cckp:label"}, {"s": "syn2", "p": "cckp:label"}],
            "count": 2,
        }

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_pending_then_complete(self, mock_urlopen, _sleep):
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            {"job_id": "job-1", "status": "running"},
            _bindings_response(["n"], [["42"]]),
        ])
        result = sparql_request("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")
        assert result["rows"] == [{"n": "42"}]

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_status_error_raises_with_error_text(self, mock_urlopen, _sleep):
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            {"job_id": "job-1", "status": "error", "error": "malformed query"},
        ])
        with pytest.raises(Exception, match="malformed query"):
            sparql_request("SELECT BAD")

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_unrecognized_status_raises_generic_exception(self, mock_urlopen, _sleep):
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            {"job_id": "job-1", "status": "something_else"},
        ])
        with pytest.raises(Exception, match="unrecognized status"):
            sparql_request("SELECT 1")

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_ask_returns_boolean(self, mock_urlopen, _sleep):
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            {"job_id": "job-1", "status": "complete", "results": json.dumps({"head": {}, "boolean": True})},
        ])
        assert sparql_request("ASK { ?d a cckp:Dataset }") == {"boolean": True}

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_poll_429_is_retried(self, mock_urlopen, _sleep):
        err = urllib.error.HTTPError(url="http://x", code=429, msg="Too Many", hdrs={}, fp=None)
        err.read = lambda: b"rate limited"
        mock_urlopen.side_effect = [
            _mock_response({"job_id": "job-1", "status": "pending"}),
            err,
            _mock_response(_bindings_response(["n"], [["1"]])),
        ]
        assert sparql_request("SELECT ?n WHERE { ?n ?p ?o }")["rows"] == [{"n": "1"}]

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_submit_429_is_retried(self, mock_urlopen, _sleep):
        err = urllib.error.HTTPError(url="http://x", code=429, msg="Too Many", hdrs={}, fp=None)
        err.read = lambda: b"rate limited"
        mock_urlopen.side_effect = [
            err,
            _mock_response({"job_id": "job-1", "status": "pending"}),
            _mock_response(_bindings_response(["n"], [["1"]])),
        ]
        assert sparql_request("SELECT ?n WHERE { ?n ?p ?o }")["count"] == 1

    @patch("lambda_function.urllib.request.urlopen")
    def test_poll_non_retryable_http_error_raises(self, mock_urlopen):
        err = urllib.error.HTTPError(url="http://x", code=403, msg="Forbidden", hdrs={}, fp=None)
        err.read = lambda: b"denied"
        mock_urlopen.side_effect = [_mock_response({"job_id": "job-1", "status": "pending"}), err]
        with patch("lambda_function.time.sleep", return_value=None):
            with pytest.raises(Exception, match="SPARQL poll error 403"):
                sparql_request("SELECT 1")

    @patch("lambda_function.urllib.request.urlopen")
    def test_shared_invocation_budget_caps_later_jobs(self, mock_urlopen):
        # An earlier job in the same invocation already used the budget:
        # the next submit must refuse rather than start a fresh 85s clock.
        lambda_function._invocation_deadline = time.monotonic() - 1
        with pytest.raises(TimeoutError, match="budget exhausted"):
            sparql_request("SELECT 1")
        mock_urlopen.assert_not_called()

    @patch("lambda_function.urllib.request.urlopen")
    def test_submit_timeout_raises_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(reason=socket.timeout("timed out"))
        with pytest.raises(TimeoutError, match="timed out"):
            sparql_request("SELECT 1")

    @patch("lambda_function.urllib.request.urlopen")
    def test_submit_http_error_re_raises(self, mock_urlopen):
        err = urllib.error.HTTPError(url="http://x", code=400, msg="Bad", hdrs={}, fp=None)
        err.read = lambda: b"bad query"
        mock_urlopen.side_effect = err
        with pytest.raises(Exception, match="SPARQL submit error 400"):
            sparql_request("SELECT BAD")

    @patch("lambda_function.urllib.request.urlopen")
    def test_submit_missing_job_id_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"status": "pending"})
        with pytest.raises(Exception, match="no job_id"):
            sparql_request("SELECT 1")

    @patch("lambda_function.time.monotonic")
    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_poll_deadline_exceeded_raises_timeout(self, mock_urlopen, _sleep, mock_monotonic):
        # First call establishes the deadline; subsequent calls report time
        # already past it so the poll loop gives up on its first check.
        mock_monotonic.side_effect = [0, 1000, 1000, 1000]
        self._mock_urlopen_sequence(mock_urlopen, [
            {"job_id": "job-1", "status": "pending"},
            {"job_id": "job-1", "status": "running"},
        ])
        with pytest.raises(TimeoutError, match="timed out"):
            sparql_request("SELECT 1")


# ---------------------------------------------------------------------------
# _resolve_cckp_graph — named-graph version scoping
# ---------------------------------------------------------------------------

class TestResolveCckpGraph:
    def _resolve_with_rows(self, mock_urlopen, rows):
        mock_urlopen.side_effect = [
            _mock_response({"job_id": "job-1", "status": "pending"}),
            _mock_response(_bindings_response(["g", "prov"], rows)),
        ]
        return lambda_function._resolve_cckp_graph()

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_picks_latest_complete_cckp_graph_excluding_others(self, mock_urlopen, _sleep):
        graph = self._resolve_with_rows(mock_urlopen, [
            ["urn:sagebrain:nf:2026-09-20", "1"],
            ["urn:sagebrain:cckp:2026-08-01", "1"],
            ["urn:sagebrain:cckp:2026-09-15", "1"],
            ["urn:sagebrain:als:2026-09-30", "1"],
            ["http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph", "0"],
        ])
        assert graph == "urn:sagebrain:cckp:2026-09-15"
        assert graph.startswith(CCKP_GRAPH_PREFIX)

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_skips_newest_graph_without_provenance(self, mock_urlopen, _sleep, capsys):
        graph = self._resolve_with_rows(mock_urlopen, [
            ["urn:sagebrain:cckp:2026-09-15", "1"],
            ["urn:sagebrain:cckp:2026-10-01", "0"],  # mid-load
        ])
        assert graph == "urn:sagebrain:cckp:2026-09-15"
        assert "WARNING" in capsys.readouterr().out

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_falls_back_to_latest_when_no_graph_has_provenance(self, mock_urlopen, _sleep, capsys):
        graph = self._resolve_with_rows(mock_urlopen, [
            ["urn:sagebrain:cckp:2026-09-15", "0"],
            ["urn:sagebrain:cckp:2026-10-01", "0"],
        ])
        assert graph == "urn:sagebrain:cckp:2026-10-01"
        assert "WARNING" in capsys.readouterr().out

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_discovery_is_type_anchored_not_full_scan(self, mock_urlopen, _sleep):
        self._resolve_with_rows(mock_urlopen, [["urn:sagebrain:cckp:2026-09-15", "1"]])
        submitted = json.loads(mock_urlopen.call_args_list[0][0][0].data)["query"]
        assert "?s a cckp:Dataset" in submitted
        assert "?s ?p ?o" not in submitted

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_no_cckp_graph_raises(self, mock_urlopen, _sleep):
        with pytest.raises(Exception, match="No named graph found"):
            self._resolve_with_rows(mock_urlopen, [["urn:sagebrain:nf:2026-09-20", "1"]])

    def test_cached_after_first_resolution(self):
        lambda_function._resolved_graph_uri = TEST_GRAPH_URI
        lambda_function._resolved_graph_at = time.monotonic()
        with patch("lambda_function.urllib.request.urlopen") as mock_urlopen:
            graph = lambda_function._resolve_cckp_graph()
            assert graph == TEST_GRAPH_URI
            mock_urlopen.assert_not_called()

    @patch("lambda_function.time.sleep", return_value=None)
    @patch("lambda_function.urllib.request.urlopen")
    def test_cache_expires_after_ttl(self, mock_urlopen, _sleep):
        lambda_function._resolved_graph_uri = "urn:sagebrain:cckp:2026-09-15"
        lambda_function._resolved_graph_at = (
            time.monotonic() - lambda_function.GRAPH_CACHE_TTL_SECONDS - 1
        )
        graph = self._resolve_with_rows(mock_urlopen, [["urn:sagebrain:cckp:2026-10-01", "1"]])
        assert graph == "urn:sagebrain:cckp:2026-10-01"


# ---------------------------------------------------------------------------
# lambda_handler – routing (graph resolution + sparql_request mocked so
# each handler function's own scoping/limit logic is what's under test)
# ---------------------------------------------------------------------------

BINDINGS_STUB = {"headers": ["s"], "rows": [{"s": "syn1"}], "count": 1}


@pytest.mark.usefixtures("resolved_graph")
class TestHandlerRouting:
    """Each function is reached via both apiPath and function-name dispatch."""

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_sparql_query_api_path(self, _mock):
        event = _api_event("/sparql-query", [
            {"name": "query", "value": "SELECT ?s WHERE { ?s a cckp:Dataset }"},
        ])
        resp = lambda_handler(event, None)
        assert _body(resp) == BINDINGS_STUB

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_sparql_query_function(self, _mock):
        event = _function_event("sparqlQuery", [
            {"name": "query", "value": "SELECT ?s WHERE { ?s a cckp:Dataset }"},
        ])
        resp = lambda_handler(event, None)
        assert _body(resp) == BINDINGS_STUB

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_sparql_query_scopes_to_graph_and_adds_limit(self, mock_request):
        event = _api_event("/sparql-query", [
            {"name": "query", "value": "SELECT ?s WHERE { ?s a cckp:Dataset }"},
        ])
        lambda_handler(event, None)
        sent_query = mock_request.call_args[0][0]
        assert f"GRAPH <{TEST_GRAPH_URI}>" in sent_query
        assert "LIMIT" in sent_query

    @pytest.mark.parametrize("query", [
        "CONSTRUCT { ?d cckp:x ?n } WHERE { ?d a cckp:Dataset }",
        "DESCRIBE <https://example.org/x>",
    ])
    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_sparql_query_rejects_non_select_ask(self, mock_request, query):
        resp = lambda_handler(_api_event("/sparql-query", [{"name": "query", "value": query}]), None)
        assert "Only SELECT and ASK" in _body(resp)["error"]
        mock_request.assert_not_called()

    @pytest.mark.parametrize("query", [
        "SELECT ?x WHERE { GRAPH ?g { ?x a cckp:Dataset } }",
        "SELECT ?x FROM <urn:sagebrain:nf:2026-09-20> WHERE { ?x a cckp:Dataset }",
        "SELECT ?x WHERE { SERVICE <https://example.org/sparql> { ?x a cckp:Dataset } }",
    ])
    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_sparql_query_rejects_scope_escapes(self, mock_request, query):
        resp = lambda_handler(_api_event("/sparql-query", [{"name": "query", "value": query}]), None)
        assert "may not use" in _body(resp)["error"]
        mock_request.assert_not_called()

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_scope_keywords_inside_literals_are_allowed(self, mock_request):
        query = 'SELECT ?d WHERE { ?d a cckp:Dataset ; cckp:description ?t . FILTER(CONTAINS(?t, "data from graph")) }'
        lambda_handler(_api_event("/sparql-query", [{"name": "query", "value": query}]), None)
        mock_request.assert_called_once()

    @patch("lambda_function.sparql_request", return_value={"boolean": True})
    def test_ask_is_scoped_but_not_limited(self, mock_request):
        query = "ASK { ?d a cckp:Dataset }"
        resp = lambda_handler(_api_event("/sparql-query", [{"name": "query", "value": query}]), None)
        sent = mock_request.call_args[0][0]
        assert f"GRAPH <{TEST_GRAPH_URI}>" in sent
        assert "LIMIT" not in sent
        assert _body(resp) == {"boolean": True}

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_get_schema_api_path(self, _mock):
        resp = lambda_handler(_api_event("/schema"), None)
        assert _body(resp) == BINDINGS_STUB

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_get_schema_scopes_to_graph_and_filters_namespace(self, mock_request):
        lambda_handler(_api_event("/schema"), None)
        sent_query = mock_request.call_args[0][0]
        assert f"GRAPH <{TEST_GRAPH_URI}>" in sent_query
        assert "https://w3id.org/mc2-center/cckp-portal/" in sent_query
        assert "LIMIT" in sent_query

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_get_shape_api_path(self, _mock):
        event = _api_event("/shape", [
            {"name": "className", "value": "Dataset"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["className"] == "Dataset"
        assert body["headers"] == ["s"]

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_get_shape_function_with_prefix(self, _mock):
        event = _function_event("getShape", [
            {"name": "className", "value": "cckp:Tool"},
        ])
        resp = lambda_handler(event, None)
        assert _body(resp)["className"] == "Tool"

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_get_shape_scopes_to_graph(self, mock_request):
        event = _api_event("/shape", [{"name": "className", "value": "Dataset"}])
        lambda_handler(event, None)
        sent_query = mock_request.call_args[0][0]
        assert f"GRAPH <{TEST_GRAPH_URI}>" in sent_query

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_count_by_type_api_path(self, _mock):
        resp = lambda_handler(_api_event("/count-by-type"), None)
        assert _body(resp) == BINDINGS_STUB

    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_count_by_type_enumerates_explicit_classes(self, mock_request):
        lambda_handler(_function_event("countByType"), None)
        sent_query = mock_request.call_args[0][0]
        for cls in ("Dataset", "Publication", "Tool", "Grant", "EducationalResource"):
            assert f"cckp:{cls}" in sent_query
        # Explicitly NOT the old unscoped `?s a ?type` pattern.
        assert "?s a ?type" not in sent_query
        assert f"GRAPH <{TEST_GRAPH_URI}>" in sent_query

    def test_unknown_function(self):
        event = _function_event("noSuchFunction")
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert "error" in body
        assert "Unknown function" in body["error"]
        assert resp["response"]["httpStatusCode"] == 200


# ---------------------------------------------------------------------------
# lambda_handler – error handling (424-prevention)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("resolved_graph")
class TestHandlerErrorPaths:
    """Verify the handler always returns a well-formed response."""

    @patch("lambda_function.sparql_request", side_effect=TimeoutError("timed out"))
    def test_timeout_returns_200_with_error(self, _mock):
        event = _api_event("/schema")
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        assert "timed out" in _body(resp)["error"]

    @patch("lambda_function.sparql_request", side_effect=RuntimeError("boom"))
    def test_generic_exception_returns_200_with_error(self, _mock):
        event = _api_event("/schema")
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        assert "boom" in _body(resp)["error"]

    def test_malformed_event_does_not_crash(self):
        """A property dict missing 'name' would previously crash the Lambda."""
        event = _api_event("/sparql-query", [{"wrong": "data"}])
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        body = _body(resp)
        assert "error" in body

    def test_completely_empty_event(self):
        resp = lambda_handler({}, None)
        assert "response" in resp
        assert resp["response"]["httpStatusCode"] == 200

    def test_missing_query_returns_validation_error(self):
        event = _api_event("/sparql-query")
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body == {"error": "query is required"}

    def test_missing_classname_returns_validation_error(self):
        event = _api_event("/shape")
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body == {"error": "className is required"}

    @pytest.mark.parametrize("bad_name", [
        "Animal Model",       # space
        "Foo.Bar",            # dot
        "A}B",                # closing brace
        "x; DROP",            # semicolon
        "cckp:Bad>Name",      # angle bracket (after prefix strip)
    ])
    def test_invalid_classname_rejected(self, bad_name):
        event = _api_event("/shape", [{"name": "className", "value": bad_name}])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert "Invalid className" in body.get("error", "")

    @pytest.mark.parametrize("good_name", [
        "Dataset",
        "Tool",
        "cckp:Dataset",
        "_Private",
        "Type2",
    ])
    @patch("lambda_function.sparql_request", return_value=BINDINGS_STUB)
    def test_valid_classname_accepted(self, _mock, good_name):
        event = _api_event("/shape", [{"name": "className", "value": good_name}])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert "headers" in body

    def test_response_always_has_required_keys(self):
        """Even with a garbage event the response has the Bedrock-required shape."""
        resp = lambda_handler({"garbage": True}, None)
        r = resp["response"]
        assert "actionGroup" in r
        assert "httpStatusCode" in r
        assert "responseBody" in r
        # body should be valid JSON
        json.loads(r["responseBody"]["application/json"]["body"])
