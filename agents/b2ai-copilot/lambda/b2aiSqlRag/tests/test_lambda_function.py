"""Unit tests for the b2aiSqlRag Lambda function.

All Synapse network calls are mocked so no live token/endpoint is needed.
"""

import base64
import gzip
import json
import os
import socket
import time
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

import lambda_function
from lambda_function import (
    _bare_id,
    _check_sql_tables,
    _coerce_property_value,
    _d4d_render_value,
    _d4d_section_key,
    _make_response,
    _parse_bedrock_pseudo_json,
    _parse_bundle,
    _resolve_table,
    _sql_strip_string_literals,
    _SqlScanError,
    build_portal_url,
    compare_d4d,
    extract_params,
    get_d4d,
    lambda_handler,
    list_d4ds,
    search_d4d,
    GC_ORG_IDS,
    PORTAL_ROUTES,
    QUERY_TIMEOUT,
    TABLES,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE_D4D_DATA_PATH = os.path.join(FIXTURES_DIR, "sample_d4d_data.json")


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def _load_json_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_event(api_path, properties=None, http_method="POST"):
    """Build a Bedrock action-group event using the apiPath style."""
    return {
        "messageVersion": "1.0",
        "actionGroup": "b2aiSqlRag",
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
        "actionGroup": "b2aiSqlRag",
        "function": function,
        "parameters": parameters or [],
    }


def _body(response):
    """Extract the parsed JSON body from a Lambda response dict."""
    return json.loads(
        response["response"]["responseBody"]["application/json"]["body"]
    )


def _bundle(headers, rows, count=None):
    """Build a fake QueryResultBundle as returned by the Synapse REST API."""
    return {
        "queryCount": count if count is not None else len(rows),
        "queryResult": {
            "queryResults": {
                "headers": [{"name": h} for h in headers],
                "rows": [{"values": r} for r in rows],
            }
        },
    }


# ---------------------------------------------------------------------------
# _make_response
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


# ---------------------------------------------------------------------------
# extract_params
# ---------------------------------------------------------------------------

class TestExtractParams:
    def test_from_request_body(self):
        event = _api_event("/sql-query", [
            {"name": "table", "type": "string", "value": "standards"},
            {"name": "sql", "type": "string", "value": "SELECT * FROM {table}"},
        ])
        assert extract_params(event) == {
            "table": "standards",
            "sql": "SELECT * FROM {table}",
        }

    def test_from_parameters_list(self):
        event = _function_event("getColumns", [
            {"name": "table", "type": "string", "value": "standards"},
        ])
        assert extract_params(event) == {"table": "standards"}

    def test_empty_event(self):
        assert extract_params({}) == {}

    def test_malformed_property_skipped(self):
        event = _api_event("/sql-query", [{"wrong_key": "oops"}])
        assert extract_params(event) == {}

    def test_malformed_property_mixed_with_valid(self):
        event = _api_event("/sql-query", [
            {"wrong_key": "oops"},
            {"name": "table", "value": "standards"},
        ])
        assert extract_params(event) == {"table": "standards"}

    def test_array_typed_value_decoded_from_json_string(self):
        # Real Bedrock requestBody events deliver array-typed values as a
        # JSON-encoded string, not a native list (confirmed via AWS docs —
        # see _coerce_property_value's docstring).
        event = _api_event("/sql-query", [
            {"name": "ids", "type": "array", "value": '["syn1", "syn2"]'},
        ])
        assert extract_params(event) == {"ids": ["syn1", "syn2"]}

    def test_array_typed_value_decoded_from_malformed_pseudo_json(self):
        event = _api_event("/portal-url", [
            {"name": "tags", "type": "array", "value": "[FHIR, HL7 CDA]"},
        ])
        assert extract_params(event) == {
            "tags": ["FHIR", "HL7 CDA"],
        }

    def test_scalar_typed_value_left_alone(self):
        event = _api_event("/sql-query", [
            {"name": "table", "type": "string", "value": "standards"},
        ])
        assert extract_params(event) == {"table": "standards"}

    def test_array_value_already_a_list_passes_through(self):
        # Direct/synthetic invocations (and possibly future correctly-typed
        # Bedrock behavior) may hand over a real list already — must not be
        # altered.
        event = _api_event("/portal-url", [
            {"name": "tags", "type": "array", "value": ["glioma"]},
        ])
        assert extract_params(event) == {"tags": ["glioma"]}


class TestCoercePropertyValue:
    def test_non_array_object_type_passthrough(self):
        assert _coerce_property_value("string", "[not, touched]") == "[not, touched]"

    def test_empty_bracket_array(self):
        assert _coerce_property_value("array", "[]") == []

    def test_unparseable_non_bracketed_string_returned_as_is(self):
        # Not valid JSON and not bracketed — nothing safe to do; let the
        # caller's own validation produce a clear error instead of guessing.
        assert _coerce_property_value("array", "not a list at all") == "not a list at all"

    def test_facets_pseudo_json_object_with_nested_array(self):
        # Regression: Bedrock can send `facets` as
        # "[{columnName=topic, values=[Image, Genome]}]" -- bare key=value
        # pairs, not valid JSON. The naive comma-split fallback used to
        # shred this into unusable string fragments.
        value = "[{columnName=topic, values=[Image, Genome]}]"
        assert _coerce_property_value("array", value) == [
            {"columnName": "topic", "values": ["Image", "Genome"]}
        ]

    def test_facets_pseudo_json_multiple_facets(self):
        value = (
            "[{columnName=topic, values=[Image]}, "
            "{columnName=category, values=[Ontology or Vocabulary]}]"
        )
        assert _coerce_property_value("array", value) == [
            {"columnName": "topic", "values": ["Image"]},
            {"columnName": "category", "values": ["Ontology or Vocabulary"]},
        ]

    def test_plain_list_pseudo_json_still_works(self):
        assert _coerce_property_value("array", "[FHIR, HL7 CDA]") == ["FHIR", "HL7 CDA"]

    def test_pseudo_json_value_with_spaces(self):
        assert _coerce_property_value("array", "[data model, ontology]") == [
            "data model",
            "ontology",
        ]


class TestParseBedrockPseudoJson:
    """Direct unit tests of the bracket-aware pseudo-JSON parser."""

    def test_nested_object_in_array(self):
        assert _parse_bedrock_pseudo_json(
            "[{columnName=topic, values=[Image, Genome]}]"
        ) == [{"columnName": "topic", "values": ["Image", "Genome"]}]

    def test_simple_array(self):
        assert _parse_bedrock_pseudo_json("[a, b, c]") == ["a", "b", "c"]

    def test_empty_array(self):
        assert _parse_bedrock_pseudo_json("[]") == []

    def test_value_with_spaces_preserved(self):
        assert _parse_bedrock_pseudo_json("[FHIR, HL7 CDA]") == ["FHIR", "HL7 CDA"]

    def test_top_level_commas_not_split_inside_nested_brackets(self):
        text = "[{columnName=topic, values=[Image, Genome]}, {columnName=category, values=[X]}]"
        result = _parse_bedrock_pseudo_json(text)
        assert len(result) == 2
        assert result[0]["columnName"] == "topic"
        assert result[1]["columnName"] == "category"


# ---------------------------------------------------------------------------
# _resolve_table
# ---------------------------------------------------------------------------

class TestResolveTable:
    @pytest.mark.parametrize("alias", list(TABLES))
    def test_known_alias(self, alias):
        assert _resolve_table(alias) == TABLES[alias]

    def test_unrelated_syn_id_rejected(self):
        with pytest.raises(ValueError, match="Unknown table"):
            _resolve_table("syn12345678")

    @pytest.mark.parametrize("alias", list(TABLES))
    def test_known_syn_id_resolves_to_pinned_version(self, alias):
        bare = TABLES[alias].split(".")[0]
        assert _resolve_table(bare) == TABLES[alias]
        assert _resolve_table(bare + ".1") == TABLES[alias]

    def test_unknown_table_raises(self):
        with pytest.raises(ValueError, match="Unknown table"):
            _resolve_table("bogus")


# ---------------------------------------------------------------------------
# _parse_bundle
# ---------------------------------------------------------------------------

class TestParseBundle:
    def test_flattens_rows(self):
        bundle = _bundle(["name", "category"], [["FHIR", "Data Standard"]])
        result = _parse_bundle(bundle)
        assert result == {
            "count": 1,
            "headers": ["name", "category"],
            "rows": [{"name": "FHIR", "category": "Data Standard"}],
        }

    def test_empty_bundle(self):
        assert _parse_bundle({}) == {"count": None, "headers": [], "rows": []}


# ---------------------------------------------------------------------------
# lambda_handler – routing
# ---------------------------------------------------------------------------

class TestHandlerRouting:
    """Each function is reached via both apiPath and function-name dispatch."""

    @patch("lambda_function._run_query")
    def test_sql_query_api_path(self, mock_run_query):
        mock_run_query.return_value = _bundle(["name"], [["FHIR"]])
        event = _api_event("/sql-query", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["rows"] == [{"name": "FHIR"}]
        # {table} placeholder was replaced with the resolved synId
        called_sql = mock_run_query.call_args[0][1]
        assert "{table}" not in called_sql
        assert TABLES["standards"] in called_sql

    @patch("lambda_function._run_query")
    def test_sql_query_function(self, mock_run_query):
        mock_run_query.return_value = _bundle(["name"], [["FHIR"]])
        event = _function_event("sqlQuery", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        resp = lambda_handler(event, None)
        assert _body(resp)["rows"] == [{"name": "FHIR"}]

    @patch("lambda_function._run_query")
    def test_get_columns_api_path(self, mock_run_query):
        mock_run_query.return_value = {
            "selectColumns": [{"name": "id"}, {"name": "name"}],
            "queryResult": {"queryResults": {"headers": [], "rows": []}},
        }
        event = _api_event("/columns", [{"name": "table", "value": "standards"}])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["table"] == "standards"
        assert body["columns"] == ["id", "name"]

    @patch("lambda_function._run_query")
    def test_get_columns_function(self, mock_run_query):
        mock_run_query.return_value = {
            "selectColumns": [{"name": "name"}],
            "queryResult": {"queryResults": {"headers": [], "rows": []}},
        }
        resp = lambda_handler(
            _function_event("getColumns", [{"name": "table", "value": "organizations"}]), None
        )
        assert _body(resp)["columns"] == ["name"]

    @patch("lambda_function._run_query")
    def test_count_by_type_api_path(self, mock_run_query):
        mock_run_query.return_value = _bundle([], [], count=42)
        resp = lambda_handler(_api_event("/count-by-type"), None)
        body = _body(resp)
        assert set(body["counts"]) == set(TABLES)
        assert all(v == 42 for v in body["counts"].values())

    @patch("lambda_function._run_query")
    def test_count_by_type_function(self, mock_run_query):
        mock_run_query.return_value = _bundle([], [], count=7)
        resp = lambda_handler(_function_event("countByType"), None)
        assert _body(resp)["counts"]["standards"] == 7

    @patch("lambda_function._run_query", side_effect=Exception("boom"))
    def test_count_by_type_partial_failure(self, _mock):
        resp = lambda_handler(_function_event("countByType"), None)
        body = _body(resp)
        assert "errors" in body
        assert all("boom" in msg for msg in body["errors"].values())

    def test_count_by_type_queries_run_concurrently(self):
        # Regression: count_by_type used to query its 7 tables one at a
        # time, so it alone could use up to 7x a single query's share of
        # the invocation timeout budget. Each of the 7 mocked queries below
        # sleeps 0.15s; run sequentially that's >=1.0s, concurrently it
        # should take roughly one query's worth of time.
        def _slow_query(*args, **kwargs):
            time.sleep(0.15)
            return {"queryCount": 1}

        with patch("lambda_function._run_query", side_effect=_slow_query):
            start = time.monotonic()
            result = lambda_function.count_by_type({})
            elapsed = time.monotonic() - start

        assert set(result["counts"]) == set(TABLES)
        assert elapsed < 0.15 * len(TABLES) / 2

    def test_unknown_function(self):
        event = _function_event("noSuchFunction")
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert "error" in body
        assert "Unknown function" in body["error"]
        assert resp["response"]["httpStatusCode"] == 200


# ---------------------------------------------------------------------------
# lambda_handler – validation & error handling (424-prevention)
# ---------------------------------------------------------------------------

class TestHandlerErrorPaths:
    """Verify the handler always returns a well-formed response."""

    def test_missing_table_returns_validation_error(self):
        event = _api_event("/sql-query", [{"name": "sql", "value": "SELECT 1"}])
        resp = lambda_handler(event, None)
        assert _body(resp) == {"error": "table is required"}

    def test_missing_sql_returns_validation_error(self):
        event = _api_event("/sql-query", [{"name": "table", "value": "standards"}])
        resp = lambda_handler(event, None)
        assert _body(resp) == {"error": "sql is required"}

    def test_unknown_table_returns_error(self):
        event = _api_event("/sql-query", [
            {"name": "table", "value": "bogus"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        resp = lambda_handler(event, None)
        assert "Unknown table" in _body(resp)["error"]

    def test_missing_table_for_columns(self):
        resp = lambda_handler(_api_event("/columns"), None)
        assert _body(resp) == {"error": "table is required"}

    @patch("lambda_function._run_query", side_effect=TimeoutError("timed out"))
    def test_timeout_returns_200_with_error(self, _mock):
        event = _api_event("/sql-query", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        assert "timed out" in _body(resp)["error"]

    @patch("lambda_function._run_query", side_effect=RuntimeError("boom"))
    def test_generic_exception_returns_200_with_error(self, _mock):
        event = _api_event("/sql-query", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        assert "boom" in _body(resp)["error"]

    def test_malformed_event_does_not_crash(self):
        """A property dict missing 'name' would previously crash the Lambda."""
        event = _api_event("/sql-query", [{"wrong": "data"}])
        resp = lambda_handler(event, None)
        assert resp["response"]["httpStatusCode"] == 200
        assert "error" in _body(resp)

    def test_completely_empty_event(self):
        resp = lambda_handler({}, None)
        assert "response" in resp
        assert resp["response"]["httpStatusCode"] == 200

    def test_response_always_has_required_keys(self):
        """Even with a garbage event the response has the Bedrock-required shape."""
        resp = lambda_handler({"garbage": True}, None)
        r = resp["response"]
        assert "actionGroup" in r
        assert "httpStatusCode" in r
        assert "responseBody" in r
        # body should be valid JSON
        json.loads(r["responseBody"]["application/json"]["body"])


# ---------------------------------------------------------------------------
# _run_query network layer
# ---------------------------------------------------------------------------

class TestRunQueryNetwork:
    @patch("lambda_function._request")
    def test_start_then_poll_success(self, mock_request):
        mock_request.side_effect = [
            (200, {"token": "tok-1"}),
            (200, {"queryCount": 1, "queryResult": {"queryResults": {"headers": [], "rows": []}}}),
        ]
        from lambda_function import _run_query
        result = _run_query(TABLES["standards"], "SELECT * FROM {table}", 25, 0x3)
        assert result["queryCount"] == 1

    @patch("lambda_function._request")
    def test_start_failure_raises(self, mock_request):
        mock_request.return_value = (400, {"reason": "bad sql"})
        from lambda_function import _run_query
        with pytest.raises(Exception, match="query start failed"):
            _run_query(TABLES["standards"], "BAD SQL", 25, 0x3)

    @patch("lambda_function._request")
    def test_poll_failure_raises(self, mock_request):
        mock_request.side_effect = [
            (200, {"token": "tok-1"}),
            (500, {"reason": "server error"}),
        ]
        from lambda_function import _run_query
        with pytest.raises(Exception, match="query failed"):
            _run_query(TABLES["standards"], "SELECT * FROM {table}", 25, 0x3)

    @patch("lambda_function.time.sleep")
    @patch("lambda_function._remaining_budget")
    @patch("lambda_function._request")
    def test_poll_raises_immediately_when_budget_exhausted(
        self, mock_request, mock_remaining, mock_sleep
    ):
        """Regression for the ~2x timeout-budget overrun: the poll loop must
        stop as soon as the shared invocation budget is spent, not wait for
        its own fresh QUERY_TIMEOUT-based deadline."""
        mock_request.side_effect = [
            (200, {"token": "tok-1"}),
            (202, {}),
        ]
        mock_remaining.return_value = 0
        from lambda_function import _run_query
        with pytest.raises(TimeoutError, match="still running"):
            _run_query(TABLES["standards"], "SELECT * FROM {table}", 25, 0x3)
        mock_sleep.assert_not_called()

    @patch("lambda_function.time.sleep")
    @patch("lambda_function._remaining_budget")
    @patch("lambda_function._request")
    def test_poll_sleep_capped_by_remaining_budget(
        self, mock_request, mock_remaining, mock_sleep
    ):
        mock_request.side_effect = [
            (200, {"token": "tok-1"}),
            (202, {}),
            (200, {"queryCount": 0, "queryResult": {"queryResults": {"headers": [], "rows": []}}}),
        ]
        mock_remaining.return_value = 0.3  # less than the usual 1.0s poll interval
        from lambda_function import _run_query
        _run_query(TABLES["standards"], "SELECT * FROM {table}", 25, 0x3)
        mock_sleep.assert_called_once_with(0.3)


# ---------------------------------------------------------------------------
# _request – HTTP layer
# ---------------------------------------------------------------------------

class TestRequest:
    @patch("lambda_function.urllib.request.urlopen")
    def test_http_error_returns_status_and_detail(self, mock_urlopen):
        err = urllib.error.HTTPError(url="http://x", code=400, msg="Bad", hdrs={}, fp=None)
        err.read = lambda: b'{"reason": "bad"}'
        mock_urlopen.side_effect = err
        from lambda_function import _request
        status, detail = _request("GET", "http://x")
        assert status == 400
        assert detail == {"reason": "bad"}

    @patch("lambda_function.urllib.request.urlopen")
    def test_socket_timeout_raises_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = socket.timeout("timed out")
        from lambda_function import _request
        with pytest.raises(TimeoutError):
            _request("GET", "http://x")

    @patch("lambda_function.urllib.request.urlopen")
    def test_non_json_success_body_does_not_raise(self, mock_urlopen):
        """A non-JSON 200 body must not blow up json.loads — callers rely on
        always getting something .get()-able back."""
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = lambda *a: None
        mock_urlopen.return_value.status = 200
        mock_urlopen.return_value.read.return_value = b"not json at all"
        from lambda_function import _request
        status, body = _request("GET", "http://x")
        assert status == 200
        assert body == {"raw": "not json at all"}

    @patch("lambda_function.urllib.request.urlopen")
    def test_http_error_non_json_body(self, mock_urlopen):
        err = urllib.error.HTTPError(url="http://x", code=502, msg="Bad Gateway", hdrs={}, fp=None)
        err.read = lambda: b"<html>gateway error</html>"
        mock_urlopen.side_effect = err
        from lambda_function import _request
        status, detail = _request("GET", "http://x")
        assert status == 502
        assert detail == {"raw": "<html>gateway error</html>"}

    @patch("lambda_function.urllib.request.urlopen")
    def test_no_authorization_header_when_token_unset(self, mock_urlopen):
        """These tables work anonymously — SYNAPSE_AUTH_TOKEN is optional and
        no Authorization header should be sent when it's empty."""
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = lambda *a: None
        mock_urlopen.return_value.status = 200
        mock_urlopen.return_value.read.return_value = b"{}"
        from lambda_function import _request
        _request("GET", "http://x")
        sent_request = mock_urlopen.call_args[0][0]
        assert "Authorization" not in sent_request.headers

    @patch("lambda_function.SYNAPSE_AUTH_TOKEN", "my-token")
    @patch("lambda_function.urllib.request.urlopen")
    def test_authorization_header_sent_when_token_set(self, mock_urlopen):
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = lambda *a: None
        mock_urlopen.return_value.status = 200
        mock_urlopen.return_value.read.return_value = b"{}"
        from lambda_function import _request
        _request("GET", "http://x")
        sent_request = mock_urlopen.call_args[0][0]
        assert sent_request.headers["Authorization"] == "Bearer my-token"

    @patch("lambda_function.urllib.request.urlopen")
    @patch("lambda_function.time.monotonic")
    def test_urlopen_timeout_uses_remaining_invocation_budget(self, mock_monotonic, mock_urlopen):
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = lambda *a: None
        mock_urlopen.return_value.status = 200
        mock_urlopen.return_value.read.return_value = b"{}"
        mock_monotonic.side_effect = [0.0, 10.0]
        lambda_function._start_invocation_budget()
        try:
            from lambda_function import _request
            _request("GET", "http://x")
        finally:
            lambda_function._end_invocation_budget()
        assert mock_urlopen.call_args.kwargs["timeout"] == pytest.approx(QUERY_TIMEOUT - 10.0)

    @patch("lambda_function.time.monotonic")
    def test_request_raises_timeout_when_invocation_budget_already_exhausted(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, QUERY_TIMEOUT + 1]
        lambda_function._start_invocation_budget()
        try:
            from lambda_function import _request
            with pytest.raises(TimeoutError, match="budget exhausted"):
                _request("GET", "http://x")
        finally:
            lambda_function._end_invocation_budget()


# ---------------------------------------------------------------------------
# Invocation-wide query-timeout budget
# ---------------------------------------------------------------------------

class TestInvocationBudget:
    def setup_method(self):
        lambda_function._end_invocation_budget()

    def teardown_method(self):
        lambda_function._end_invocation_budget()

    def test_remaining_budget_full_when_no_invocation_started(self):
        # Direct calls to the query helpers (unit tests, benchmark scripts)
        # must keep working with a fresh budget when no invocation deadline
        # has been set.
        assert lambda_function._remaining_budget() == pytest.approx(QUERY_TIMEOUT)

    @patch("lambda_function.time.monotonic")
    def test_remaining_budget_shrinks_after_start(self, mock_monotonic):
        mock_monotonic.side_effect = [100.0, 105.0]
        lambda_function._start_invocation_budget()
        assert lambda_function._remaining_budget() == pytest.approx(QUERY_TIMEOUT - 5.0)

    @patch("lambda_function.time.monotonic")
    def test_remaining_budget_never_negative(self, mock_monotonic):
        mock_monotonic.side_effect = [0.0, QUERY_TIMEOUT + 100]
        lambda_function._start_invocation_budget()
        assert lambda_function._remaining_budget() == 0.0

    @patch("lambda_function._run_query")
    def test_lambda_handler_sets_deadline_during_and_clears_after(self, mock_run_query):
        seen = {}

        def _capture(*args, **kwargs):
            seen["deadline_during"] = lambda_function._INVOCATION_DEADLINE
            return _bundle(["name"], [["FHIR"]])

        mock_run_query.side_effect = _capture
        event = _api_event("/sql-query", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        assert lambda_function._INVOCATION_DEADLINE is None
        lambda_handler(event, None)
        assert seen["deadline_during"] is not None
        assert lambda_function._INVOCATION_DEADLINE is None

    @patch("lambda_function._run_query", side_effect=RuntimeError("boom"))
    def test_lambda_handler_clears_deadline_even_on_error(self, _mock):
        event = _api_event("/sql-query", [
            {"name": "table", "value": "standards"},
            {"name": "sql", "value": "SELECT * FROM {table}"},
        ])
        lambda_handler(event, None)
        assert lambda_function._INVOCATION_DEADLINE is None


# ---------------------------------------------------------------------------
# _parse_response_body
# ---------------------------------------------------------------------------

class TestParseResponseBody:
    def test_valid_json(self):
        from lambda_function import _parse_response_body
        assert _parse_response_body('{"a": 1}') == {"a": 1}

    def test_empty_string(self):
        from lambda_function import _parse_response_body
        assert _parse_response_body("") == {}

    def test_invalid_json_falls_back_to_raw(self):
        from lambda_function import _parse_response_body
        assert _parse_response_body("not json") == {"raw": "not json"}


# ---------------------------------------------------------------------------
# build_portal_url
# ---------------------------------------------------------------------------

class TestBuildPortalUrl:
    def test_standard_detail_url(self):
        result = build_portal_url({"resourceType": "standards", "id": "B2AI_STANDARD:1"})
        assert result == {"url": "/Explore/Standard/DetailsPage?id=B2AI_STANDARD:1"}

    def test_organization_detail_url(self):
        result = build_portal_url({"resourceType": "organizations", "id": "B2AI_ORG:5"})
        assert result == {
            "url": "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:5"
        }

    def test_topic_detail_url(self):
        result = build_portal_url({"resourceType": "topics", "id": "B2AI_TOPIC:2"})
        assert result == {"url": "/Explore/DataTopic/DetailsPage?id=B2AI_TOPIC:2"}

    def test_search_without_term(self):
        result = build_portal_url({"resourceType": "search"})
        assert result == {"url": "/Search/Standards"}

    def test_search_with_term(self):
        result = build_portal_url({"resourceType": "search", "searchTerm": "FHIR"})
        assert result == {"url": "/Search/Standards?SEARCH_TERM=FHIR"}

    def test_search_with_term_is_url_encoded(self):
        result = build_portal_url({"resourceType": "search", "searchTerm": "data model"})
        assert result == {"url": "/Search/Standards?SEARCH_TERM=data+model"}

    def test_search_term_must_be_a_string(self):
        result = build_portal_url({"resourceType": "search", "searchTerm": ["FHIR"]})
        assert result == {"error": "searchTerm must be a string"}

    def test_url_is_relative_not_absolute(self):
        result = build_portal_url({"resourceType": "standards", "id": "B2AI_STANDARD:1"})
        assert not result["url"].startswith("http")

    def test_missing_resource_type(self):
        assert build_portal_url({}) == {"error": "resourceType is required"}

    def test_unknown_resource_type(self):
        result = build_portal_url({"resourceType": "datasets", "id": "B2AI_DATA:1"})
        assert "error" in result
        assert "standards" in result["error"]  # names valid options instead

    def test_missing_id(self):
        result = build_portal_url({"resourceType": "standards"})
        assert result == {"error": "id is required"}

    def test_id_prefix_mismatch(self):
        # An org id passed for a standards lookup should fail fast rather
        # than silently build a dead link.
        result = build_portal_url({"resourceType": "standards", "id": "B2AI_ORG:1"})
        assert "error" in result
        assert "B2AI_STANDARD:" in result["error"]

    @pytest.mark.parametrize("alias", list(PORTAL_ROUTES))
    def test_all_confirmed_routes_have_id_prefix(self, alias):
        assert PORTAL_ROUTES[alias]["id_prefix"].startswith("B2AI_")

    def test_via_lambda_handler(self):
        event = _function_event("buildPortalUrl", [
            {"name": "resourceType", "value": "standards"},
            {"name": "id", "value": "B2AI_STANDARD:42"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["url"] == "/Explore/Standard/DetailsPage?id=B2AI_STANDARD:42"

    def test_via_lambda_handler_api_path(self):
        event = _api_event("/portal-url", [
            {"name": "resourceType", "value": "search"},
            {"name": "searchTerm", "value": "ontology"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["url"] == "/Search/Standards?SEARCH_TERM=ontology"

    # -----------------------------------------------------------------------
    # Facet deep-linking (qw0)
    # -----------------------------------------------------------------------

    @staticmethod
    def _decode_qw0(url):
        qw0 = url.split("qw0=")[1]
        return json.loads(gzip.decompress(base64.b64decode(urllib.parse.unquote(qw0))))

    def test_facets_only(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "topic", "values": ["Image"]}],
        })
        assert "error" not in result
        assert result["url"].startswith("/Search/Standards?qw0=")
        assert self._decode_qw0(result["url"]) == {
            "selectedFacets": [
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": "topic",
                    "facetValues": ["Image"],
                }
            ]
        }

    def test_facets_multiple_columns_and_across_columns(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [
                {"columnName": "topic", "values": ["Image"]},
                {"columnName": "category", "values": ["Ontology or Vocabulary"]},
            ],
        })
        assert self._decode_qw0(result["url"]) == {
            "selectedFacets": [
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": "topic",
                    "facetValues": ["Image"],
                },
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": "category",
                    "facetValues": ["Ontology or Vocabulary"],
                },
            ]
        }

    def test_term_and_facets(self):
        result = build_portal_url({
            "resourceType": "search",
            "searchTerm": "segmentation",
            "facets": [{"columnName": "topic", "values": ["Image"]}],
        })
        assert result["url"].startswith(
            "/Search/Standards?SEARCH_TERM=segmentation&qw0="
        )
        assert self._decode_qw0(result["url"]) == {
            "selectedFacets": [
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": "topic",
                    "facetValues": ["Image"],
                }
            ]
        }

    def test_facet_values_or_within_one_column(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "topic", "values": ["Image", "Genome"]}],
        })
        assert self._decode_qw0(result["url"])["selectedFacets"][0]["facetValues"] == [
            "Image",
            "Genome",
        ]

    def test_bad_facet_column(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "notARealColumn", "values": ["x"]}],
        })
        assert "error" in result
        assert "notARealColumn" in result["error"]
        assert "topic" in result["error"]

    def test_facet_empty_values_rejected(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "topic", "values": []}],
        })
        assert "error" in result

    def test_facet_values_must_be_non_empty_strings(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "topic", "values": ["Image", ""]}],
        })
        assert "error" in result

    def test_facet_values_must_be_a_list(self):
        result = build_portal_url({
            "resourceType": "search",
            "facets": [{"columnName": "topic", "values": "Image"}],
        })
        assert "error" in result

    def test_facets_must_be_a_list(self):
        result = build_portal_url({"resourceType": "search", "facets": "topic"})
        assert "error" in result

    def test_duplicate_columns_are_merged(self):
        # DECISION: duplicate columnName entries are merged (their values
        # unioned) rather than rejected, so a caller assembling facets
        # incrementally doesn't have to pre-deduplicate them.
        result = build_portal_url({
            "resourceType": "search",
            "facets": [
                {"columnName": "topic", "values": ["Image"]},
                {"columnName": "topic", "values": ["Genome", "Image"]},
            ],
        })
        assert self._decode_qw0(result["url"]) == {
            "selectedFacets": [
                {
                    "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                    "columnName": "topic",
                    "facetValues": ["Image", "Genome"],
                }
            ]
        }

    def test_no_facets_means_no_qw0(self):
        result = build_portal_url({"resourceType": "search", "searchTerm": "FHIR"})
        assert "qw0" not in result["url"]

    def test_facets_via_lambda_handler_api_path(self):
        # Bedrock delivers an array-typed property as a JSON-encoded string
        # in the requestBody.properties event style; extract_params/
        # _coerce_property_value must decode it back into a real list of
        # facet objects.
        event = _api_event("/portal-url", [
            {"name": "resourceType", "type": "string", "value": "search"},
            {
                "name": "facets",
                "type": "array",
                "value": json.dumps([{"columnName": "topic", "values": ["Image"]}]),
            },
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["url"].startswith("/Search/Standards?qw0=")
        decoded = self._decode_qw0(body["url"])
        assert decoded["selectedFacets"][0]["columnName"] == "topic"
        assert decoded["selectedFacets"][0]["facetValues"] == ["Image"]

    def test_facets_via_lambda_handler_function_style(self):
        # Same JSON-string coercion, but via the "function" + "parameters"
        # event style instead of apiPath + requestBody.properties.
        event = _function_event("buildPortalUrl", [
            {"name": "resourceType", "type": "string", "value": "search"},
            {"name": "searchTerm", "type": "string", "value": "segmentation"},
            {
                "name": "facets",
                "type": "array",
                "value": json.dumps([{"columnName": "topic", "values": ["Image"]}]),
            },
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["url"].startswith(
            "/Search/Standards?SEARCH_TERM=segmentation&qw0="
        )
        decoded = self._decode_qw0(body["url"])
        assert decoded["selectedFacets"][0]["columnName"] == "topic"
        assert decoded["selectedFacets"][0]["facetValues"] == ["Image"]

    def test_facets_via_lambda_handler_bedrock_pseudo_json(self):
        # Regression: Bedrock's real (malformed, non-JSON) pseudo-JSON
        # encoding for a nested array-of-objects property, end to end
        # through extract_params -> _coerce_property_value -> build_portal_url.
        event = _api_event("/portal-url", [
            {"name": "resourceType", "type": "string", "value": "search"},
            {
                "name": "facets",
                "type": "array",
                "value": "[{columnName=topic, values=[Image, Genome]}]",
            },
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["url"].startswith("/Search/Standards?qw0=")
        decoded = self._decode_qw0(body["url"])
        assert decoded["selectedFacets"][0]["columnName"] == "topic"
        assert decoded["selectedFacets"][0]["facetValues"] == ["Image", "Genome"]


# ---------------------------------------------------------------------------
# Table version pinning
# ---------------------------------------------------------------------------

class TestTablePinning:
    def test_all_tables_are_versioned(self):
        """Every alias must carry a pinned '.N' version so answers match what
        the portal's Detail Pages currently show."""
        for alias, syn_id in TABLES.items():
            assert "." in syn_id, f"{alias} ({syn_id}) is not pinned to a version"

    def test_bare_id_strips_version(self):
        assert _bare_id("syn65676531.99") == "syn65676531"

    def test_bare_id_passthrough_when_unversioned(self):
        assert _bare_id("syn65676531") == "syn65676531"

    def test_gc_org_ids(self):
        assert GC_ORG_IDS == ["B2AI_ORG:114", "B2AI_ORG:115", "B2AI_ORG:116", "B2AI_ORG:117"]

    @patch("lambda_function._request")
    def test_sql_query_url_uses_bare_id_sql_uses_versioned_id(self, mock_request):
        """The async query endpoint must be hit with the bare synId, while the
        SQL FROM clause carries the pinned version."""
        from lambda_function import sql_query
        mock_request.side_effect = [
            (200, {"token": "tok-1"}),
            (200, {"queryCount": 0, "queryResult": {"queryResults": {"headers": [], "rows": []}}}),
        ]
        sql_query({"table": "standards", "sql": "SELECT * FROM {table}"})
        start_url = mock_request.call_args_list[0][0][1]
        assert f"/entity/{_bare_id(TABLES['standards'])}/table/query/async/start" in start_url
        assert "." not in start_url.split("/entity/")[1].split("/table")[0]
        sent_body = mock_request.call_args_list[0][0][2]
        assert TABLES["standards"] in sent_body["query"]["sql"]


# ---------------------------------------------------------------------------
# _parse_response_body — control characters in D4D content_text
# ---------------------------------------------------------------------------

class TestParseResponseBodyControlChars:
    def test_literal_control_characters_are_tolerated(self):
        """Synapse's table-query response embeds D4D content_text with
        literal (unescaped) newlines inside the JSON string -- confirmed
        live against syn68885644.13. strict=False must accept that instead
        of falling back to {"raw": ...}."""
        from lambda_function import _parse_response_body
        raw = '{"content_text": "line one\nline two"}'
        result = _parse_response_body(raw)
        assert "raw" not in result
        assert result["content_text"] == "line one\nline two"


# ---------------------------------------------------------------------------
# _d4d_render_value — JSON value -> readable text
# ---------------------------------------------------------------------------

class TestD4DRenderValue:
    def test_none_is_empty(self):
        assert _d4d_render_value(None) == ""

    def test_scalars(self):
        assert _d4d_render_value("hello") == "hello"
        assert _d4d_render_value(42) == "42"
        assert _d4d_render_value(3.5) == "3.5"
        assert _d4d_render_value(True) == "True"
        assert _d4d_render_value(False) == "False"

    def test_list_of_scalars_is_bulleted(self):
        assert _d4d_render_value(["a", "b"]) == "- a\n- b"

    def test_empty_list_and_dict_are_empty(self):
        assert _d4d_render_value([]) == ""
        assert _d4d_render_value({}) == ""

    def test_all_scalar_dict_is_inline(self):
        assert _d4d_render_value({"id": "x-1", "name": "X"}) == "ID: x-1; Name: X"

    def test_dict_key_label_overrides(self):
        text = _d4d_render_value({"funder_id": "f-1", "funder_url": "http://x", "doi": "10.1/x"})
        assert text == "Funder ID: f-1; Funder URL: http://x; DOI: 10.1/x"

    def test_none_and_empty_values_dropped_from_dict(self):
        text = _d4d_render_value({"a": "keep", "b": None, "c": ""})
        assert text == "A: keep"

    def test_list_of_dicts_bulleted_inline(self):
        text = _d4d_render_value([{"id": "1", "name": "A"}, {"id": "2", "name": "B"}])
        assert text == "- ID: 1; Name: A\n- ID: 2; Name: B"

    def test_dict_with_nested_dict_is_multiline_and_indented(self):
        text = _d4d_render_value({
            "name": "Sample instance",
            "funder": {"id": "funder-001", "name": "Sample Funding Program"},
        })
        assert text == (
            "Name: Sample instance\n"
            "Funder:\n"
            "  ID: funder-001; Name: Sample Funding Program"
        )

    def test_dict_with_nested_list_is_multiline_and_indented(self):
        text = _d4d_render_value({"license": "CC BY-NC 4.0", "formats": ["CSV", "JSON"]})
        assert text == (
            "License: CC BY-NC 4.0\n"
            "Formats:\n"
            "  - CSV\n"
            "  - JSON"
        )


# ---------------------------------------------------------------------------
# _ensure_d4d_loaded — reads d4d_data.json (no more HTML fetch/parse)
# ---------------------------------------------------------------------------

class TestD4DDataLoading:
    def test_loads_and_renders_sample_fixture(self, monkeypatch):
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", SAMPLE_D4D_DATA_PATH)
        lambda_function._ensure_d4d_loaded()
        assert lambda_function._D4D_LOADED is True
        assert set(lambda_function._D4D_DOCS.keys()) == {"B2AI_ORG:114", "B2AI_ORG:115"}
        assert lambda_function._D4D_SOURCE["repo"] == "bridge2ai/data-sheets-schema"
        assert "license" in lambda_function._D4D_FIELDS
        assert lambda_function._D4D_FIELD_INDEX["license"] == "license"
        assert lambda_function._D4D_FIELD_INDEX["distribution info"] == "distribution_info"

    def test_retries_when_docs_render_to_nothing(self, tmp_path, monkeypatch):
        empty_path = tmp_path / "empty_d4d_data.json"
        empty_path.write_text(json.dumps({
            "source": {}, "sections": [], "fields": {}, "docs": {},
        }))
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", str(empty_path))
        lambda_function._ensure_d4d_loaded()
        assert lambda_function._D4D_LOADED is False
        assert lambda_function._D4D_DOCS == {}

        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", SAMPLE_D4D_DATA_PATH)
        lambda_function._ensure_d4d_loaded()
        assert lambda_function._D4D_LOADED is True
        assert "B2AI_ORG:114" in lambda_function._D4D_DOCS

    def test_missing_file_raises_and_does_not_mark_loaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", str(tmp_path / "nope.json"))
        with pytest.raises(OSError):
            lambda_function._ensure_d4d_loaded()
        assert lambda_function._D4D_LOADED is False


# ---------------------------------------------------------------------------
# D4D ops — listD4Ds / getD4D (outline, section, field) / compareD4D / searchD4D
# ---------------------------------------------------------------------------

def _d4d_org_names_side_effect(org_names):
    """A `_run_query` side_effect answering the organizations-name lookup
    the D4D ops issue (D4D content itself now comes from the local JSON
    file, not a Synapse query)."""
    def _side_effect(entity_id, sql, limit, part_mask):
        if sql.strip().startswith("SELECT id, name"):
            rows = [[oid, name] for oid, name in org_names.items()]
            return _bundle(["id", "name"], rows)
        raise AssertionError(f"unexpected sql in D4D test: {sql}")
    return _side_effect


@pytest.fixture(autouse=True)
def _reset_d4d_caches():
    """The D4D ops cache the loaded data file/org names in module globals
    across warm invocations -- reset them before/after every test so tests
    don't leak state into each other."""
    def _clear():
        lambda_function._D4D_DOCS.clear()
        lambda_function._D4D_ORG_NAMES.clear()
        lambda_function._D4D_SOURCE.clear()
        lambda_function._D4D_FIELDS.clear()
        lambda_function._D4D_FIELD_INDEX.clear()
        lambda_function._D4D_LOADED = False
    _clear()
    yield
    _clear()


class _SampleD4DDataMixin:
    ORG_NAMES = {
        "B2AI_ORG:114": "Sample Grand Challenge",
        "B2AI_ORG:115": "Other Sample Grand Challenge",
    }

    @pytest.fixture(autouse=True)
    def _use_sample_data(self, monkeypatch):
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", SAMPLE_D4D_DATA_PATH)

    def _patch_org_names(self, mock_run_query):
        mock_run_query.side_effect = _d4d_org_names_side_effect(self.ORG_NAMES)


class TestD4DOps(_SampleD4DDataMixin):
    @patch("lambda_function._run_query")
    def test_list_d4ds(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = list_d4ds({})
        assert [d["orgId"] for d in result["d4ds"]] == ["B2AI_ORG:114", "B2AI_ORG:115"]
        assert result["d4ds"][0]["name"] == "Sample Grand Challenge"
        assert result["d4ds"][0]["link"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"
        assert result["d4ds"][0]["title"] == "Sample GC Dataset Documentation"
        assert result["source"]["repo"] == "bridge2ai/data-sheets-schema"

    @patch("lambda_function._run_query")
    def test_list_d4ds_skips_orgs_with_no_content(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = list_d4ds({})
        # GC_ORG_IDS includes B2AI_ORG:116/117, which aren't in the sample data at all.
        assert "B2AI_ORG:116" not in [d["orgId"] for d in result["d4ds"]]
        assert "B2AI_ORG:117" not in [d["orgId"] for d in result["d4ds"]]

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_list_d4ds_falls_back_when_org_names_query_fails(self, _mock):
        result = list_d4ds({})
        assert "error" not in result
        by_org = {d["orgId"]: d for d in result["d4ds"]}
        assert by_org["B2AI_ORG:114"]["name"] == "SAMPLE-A"
        assert by_org["B2AI_ORG:115"]["name"] == "SAMPLE-B"

    @patch("lambda_function._run_query")
    def test_get_d4d_outline(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert result["orgId"] == "B2AI_ORG:114"
        assert result["orgName"] == "Sample Grand Challenge"
        assert result["orgLink"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"
        assert result["title"] == "Sample GC Dataset Documentation"
        assert result["source"]["repo"] == "bridge2ai/data-sheets-schema"
        assert [s["id"] for s in result["sections"]] == [
            "motivation", "composition", "collection-process", "distribution", "other",
        ]
        assert all("size" in s and "fields" in s for s in result["sections"])
        distribution = next(s for s in result["sections"] if s["id"] == "distribution")
        assert [f["id"] for f in distribution["fields"]] == ["distribution_info", "license"]
        for f in distribution["fields"]:
            assert set(f) == {"id", "label", "size", "portalSection"}
            assert f["portalSection"] == "Distribution"

    @patch("lambda_function._run_query")
    def test_get_d4d_outline_omits_empty_sections_per_org(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:115"})
        # B2AI_ORG:115 has no `collection_instances`, so that section is absent.
        assert [s["id"] for s in result["sections"]] == [
            "motivation", "composition", "distribution", "other",
        ]

    @patch("lambda_function._run_query")
    def test_get_d4d_section_text(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "distribution"})
        assert result["section"] == "distribution"
        assert result["heading"] == "Distribution"
        assert "### Distribution Info" in result["text"]
        assert "### License" in result["text"]
        assert "CC BY-NC 4.0" in result["text"]
        assert "nextOffset" not in result

    @patch("lambda_function._run_query")
    def test_get_d4d_section_matches_heading_case_and_spacing_insensitively(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        by_id = get_d4d({"orgId": "B2AI_ORG:114", "section": "distribution"})
        by_heading = get_d4d({"orgId": "B2AI_ORG:114", "section": "DISTRIBUTION"})
        assert by_id["section"] == by_heading["section"] == "distribution"
        assert by_id["text"] == by_heading["text"]

    @patch("lambda_function._run_query")
    def test_get_d4d_section_matches_id_with_separators_normalized(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        # "collection-process" is the section id; hyphens, underscores, and
        # no separator at all should all resolve to it, case-insensitively.
        for variant in ("Collection Process", "collection_process", "COLLECTIONPROCESS"):
            result = get_d4d({"orgId": "B2AI_ORG:114", "section": variant})
            assert result["section"] == "collection-process", variant
            assert result["heading"] == "Collection Process"

    @patch("lambda_function._run_query")
    def test_get_d4d_section_renders_nested_values(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "collection-process"})
        text = result["text"]
        assert "ID: instance-001" in text
        assert "Name: Sample instance" in text
        assert "Funder:" in text
        assert "ID: funder-001; Name: Sample Funding Program" in text

    @patch("lambda_function._run_query")
    def test_get_d4d_section_pagination_reconstructs_exact_text(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        full = get_d4d({"orgId": "B2AI_ORG:114", "section": "collection-process"})
        assert "nextOffset" not in full

        with patch("lambda_function.D4D_SECTION_BUDGET", 20):
            page1 = get_d4d({"orgId": "B2AI_ORG:114", "section": "collection-process"})
            assert len(page1["text"]) == 20
            assert page1["nextOffset"] == 20

            collected = page1["text"]
            offset = page1["nextOffset"]
            while True:
                page = get_d4d({
                    "orgId": "B2AI_ORG:114", "section": "collection-process", "offset": offset,
                })
                assert page["totalLength"] == full["totalLength"]
                collected += page["text"]
                if "nextOffset" not in page:
                    break
                offset = page["nextOffset"]

            assert collected == full["text"]

    @patch("lambda_function._run_query")
    def test_get_d4d_missing_org_id(self, mock_run_query):
        assert get_d4d({}) == {"error": "orgId is required"}
        mock_run_query.assert_not_called()

    @patch("lambda_function._run_query")
    def test_get_d4d_unknown_org_id(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:999"})
        assert "error" in result
        assert "B2AI_ORG:114" in result["error"]

    @patch("lambda_function._run_query")
    def test_get_d4d_unknown_section_lists_valid_ids(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "nonexistent"})
        assert "error" in result
        assert "motivation" in result["error"]
        assert "distribution" in result["error"]

    @patch("lambda_function._run_query")
    def test_get_d4d_section_and_field_together_is_error(self, mock_run_query):
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "distribution", "field": "license"})
        assert "error" in result
        mock_run_query.assert_not_called()

    @patch("lambda_function._run_query")
    def test_get_d4d_field_by_key(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "field": "license"})
        assert result == {
            "orgId": "B2AI_ORG:114",
            "orgName": "Sample Grand Challenge",
            "orgLink": "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114",
            "field": "license",
            "label": "License",
            "description": "The dataset's license.",
            "schemaSection": "distribution",
            "portalSection": "Distribution",
            "inSchema": False,
            "text": "CC BY-NC 4.0",
        }

    @patch("lambda_function._run_query")
    def test_get_d4d_field_matches_label_case_and_spacing_insensitively(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        by_label = get_d4d({"orgId": "B2AI_ORG:114", "field": "DISTRIBUTION info"})
        by_key = get_d4d({"orgId": "B2AI_ORG:114", "field": "distribution_info"})
        assert by_label["field"] == by_key["field"] == "distribution_info"

    @patch("lambda_function._run_query")
    def test_get_d4d_field_pagination(self, mock_run_query):
        page1 = get_d4d({"orgId": "B2AI_ORG:114", "field": "large_notes"})
        assert page1["field"] == "large_notes"
        assert page1["inSchema"] is False
        assert len(page1["text"]) == lambda_function.D4D_SECTION_BUDGET
        assert "nextOffset" in page1

        collected = page1["text"]
        offset = page1["nextOffset"]
        while True:
            page = get_d4d({"orgId": "B2AI_ORG:114", "field": "large_notes", "offset": offset})
            collected += page["text"]
            if "nextOffset" not in page:
                break
            offset = page["nextOffset"]
        assert "Note item 1:" in collected
        assert "Note item 400:" in collected

    @patch("lambda_function._run_query")
    def test_get_d4d_field_unknown_for_org_lists_valid_field_ids(self, mock_run_query):
        # `license` exists globally but B2AI_ORG:115 has no value for it.
        result = get_d4d({"orgId": "B2AI_ORG:115", "field": "license"})
        assert "error" in result
        assert "purpose" in result["error"]
        assert "large_notes" in result["error"]
        assert "license" not in result["error"].split("Valid fields: ")[-1].split(", ")

    @patch("lambda_function._run_query")
    def test_get_d4d_field_totally_unknown(self, mock_run_query):
        result = get_d4d({"orgId": "B2AI_ORG:114", "field": "not_a_real_field"})
        assert "error" in result

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_get_d4d_outline_falls_back_when_org_names_query_fails(self, _mock, capsys):
        # A Synapse hiccup fetching org display names must not fail the
        # whole call -- D4D content is fully bundled, so this falls back to
        # the bundled `docs[org]["gc"]` label instead of erroring out.
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert "error" not in result
        assert result["orgName"] == "SAMPLE-A"
        assert result["orgLink"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"
        assert "synapse down" in capsys.readouterr().out

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_get_d4d_field_falls_back_when_org_names_query_fails(self, _mock):
        result = get_d4d({"orgId": "B2AI_ORG:114", "field": "license"})
        assert "error" not in result
        assert result["orgName"] == "SAMPLE-A"
        assert result["orgLink"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_get_d4d_org_names_failure_retries_on_next_call(self, mock_run_query):
        # The failure isn't cached: a later call whose org-names query
        # succeeds picks up the live name instead of being stuck with the
        # bundled fallback forever.
        get_d4d({"orgId": "B2AI_ORG:114"})
        assert lambda_function._D4D_ORG_NAMES == {}
        mock_run_query.side_effect = _d4d_org_names_side_effect(self.ORG_NAMES)
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert result["orgName"] == "Sample Grand Challenge"

    def test_get_d4d_data_load_failure_returns_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", str(tmp_path / "nope.json"))
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert "error" in result


class TestCompareD4D(_SampleD4DDataMixin):
    @patch("lambda_function._run_query")
    def test_compare_present_and_absent_orgs(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = compare_d4d({"field": "license"})
        assert result["field"] == "license"
        assert result["label"] == "License"
        assert result["schemaSection"] == "distribution"
        assert result["portalSection"] == "Distribution"
        by_org = {o["orgId"]: o for o in result["orgs"]}
        assert set(by_org) == set(GC_ORG_IDS)
        assert by_org["B2AI_ORG:114"] == {
            "orgId": "B2AI_ORG:114",
            "name": "Sample Grand Challenge",
            "present": True,
            "text": "CC BY-NC 4.0",
            "truncated": False,
        }
        assert by_org["B2AI_ORG:115"]["present"] is False
        assert by_org["B2AI_ORG:115"]["text"] == ""
        assert by_org["B2AI_ORG:116"]["present"] is False

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_compare_falls_back_when_org_names_query_fails(self, _mock):
        result = compare_d4d({"field": "license"})
        assert "error" not in result
        by_org = {o["orgId"]: o for o in result["orgs"]}
        assert by_org["B2AI_ORG:114"]["name"] == "SAMPLE-A"
        assert by_org["B2AI_ORG:115"]["name"] == "SAMPLE-B"
        # No bundled GC has orgId 116/117 in the sample fixture -- falls
        # back all the way to the raw orgId.
        assert by_org["B2AI_ORG:116"]["name"] == "B2AI_ORG:116"

    @patch("lambda_function._run_query")
    def test_compare_matches_by_label(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = compare_d4d({"field": "Large Notes"})
        assert result["field"] == "large_notes"

    @patch("lambda_function._run_query")
    def test_compare_caps_and_flags_truncation(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = compare_d4d({"field": "large_notes"})
        by_org = {o["orgId"]: o for o in result["orgs"]}
        per_org_budget = lambda_function.D4D_SECTION_BUDGET // len(GC_ORG_IDS)
        assert len(by_org["B2AI_ORG:114"]["text"]) <= per_org_budget
        assert by_org["B2AI_ORG:114"]["truncated"] is True
        assert by_org["B2AI_ORG:115"]["truncated"] is False
        total_text_len = sum(len(o["text"]) for o in result["orgs"])
        assert total_text_len <= lambda_function.D4D_SECTION_BUDGET

    @patch("lambda_function._run_query")
    def test_compare_missing_field(self, mock_run_query):
        assert compare_d4d({}) == {"error": "field is required"}
        mock_run_query.assert_not_called()

    @patch("lambda_function._run_query")
    def test_compare_unknown_field(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        result = compare_d4d({"field": "not_a_real_field"})
        assert "error" in result
        assert "license" in result["error"]


class TestSearchD4D(_SampleD4DDataMixin):
    def test_search_hits_have_the_documented_shape(self):
        result = search_d4d({"query": "consent"})
        assert result["query"] == "consent"
        assert result["results"]
        for hit in result["results"]:
            assert set(hit) == {"orgId", "section", "heading", "field", "label", "snippet"}
            assert len(hit["snippet"]) <= 300

    def test_search_cross_org(self):
        result = search_d4d({"query": "consent"})
        org_ids_hit = {r["orgId"] for r in result["results"]}
        assert "B2AI_ORG:114" in org_ids_hit
        assert "B2AI_ORG:115" in org_ids_hit

    def test_search_matches_field_label_even_without_text_match(self):
        result = search_d4d({"query": "large notes"})
        hits = [h for h in result["results"] if h["field"] == "large_notes"]
        assert hits
        assert hits[0]["label"] == "Large Notes"

    def test_search_scoped_to_one_org(self):
        result = search_d4d({"query": "consent", "orgId": "B2AI_ORG:114"})
        assert result["results"]
        assert all(r["orgId"] == "B2AI_ORG:114" for r in result["results"])
        assert result["truncated"] is False
        assert "orgHits" not in result

    def test_search_scoped_to_one_org_reports_truncated(self):
        with patch("lambda_function.D4D_MAX_SNIPPETS", 5):
            result = search_d4d({"query": "widget", "orgId": "B2AI_ORG:114"})
        assert len(result["results"]) == 5
        assert result["truncated"] is True
        assert "orgHits" not in result

    def test_search_scoped_pages_through_every_match(self):
        # 114 has 22 "widget" matches: with a page size of 5, following
        # nextOffset must reach all of them exactly once, in order.
        full = search_d4d({"query": "widget", "orgId": "B2AI_ORG:114"})
        seen, offset = [], 0
        with patch("lambda_function.D4D_MAX_SNIPPETS", 5):
            while True:
                page = search_d4d({"query": "widget", "orgId": "B2AI_ORG:114", "offset": offset})
                assert page["totalMatches"] == 22
                seen.extend(h["field"] for h in page["results"])
                if "nextOffset" not in page:
                    break
                offset = page["nextOffset"]
        assert len(seen) == 22 == len(set(seen))
        assert seen[:len(full["results"])] == [h["field"] for h in full["results"]]
        assert page["truncated"] is True  # last page still leaves earlier ones out

    def test_search_no_hits(self):
        result = search_d4d({"query": "blockchain"})
        assert result["results"] == []
        assert result["truncated"] is False
        assert result["orgHits"] == [
            {"orgId": oid, "matches": 0, "returned": 0} for oid in GC_ORG_IDS
        ]

    @patch("lambda_function._run_query")
    def test_search_missing_query(self, mock_run_query):
        assert search_d4d({}) == {"error": "query is required"}
        mock_run_query.assert_not_called()

    def test_search_unknown_org_id(self):
        result = search_d4d({"query": "consent", "orgId": "B2AI_ORG:999"})
        assert "error" in result

    def test_search_allocates_cap_fairly_across_orgs(self):
        # Fixture: B2AI_ORG:114 has 22 fields matching "widget",
        # B2AI_ORG:115 has 3, and B2AI_ORG:116/117 have none (and no D4D
        # content at all in this fixture) -- 25 total matches against a cap
        # of 20. Round-robin by rank fully includes the smaller org (115)
        # before the larger one (114) exhausts the shared cap, unlike the
        # old fill-in-GC_ORG_IDS-order behavior, which would have returned
        # only org 114's hits.
        result = search_d4d({"query": "widget"})
        assert len(result["results"]) == lambda_function.D4D_MAX_SNIPPETS
        assert result["truncated"] is True

        by_org = {h["orgId"]: h for h in result["orgHits"]}
        assert by_org["B2AI_ORG:114"] == {"orgId": "B2AI_ORG:114", "matches": 22, "returned": 17}
        assert by_org["B2AI_ORG:115"] == {"orgId": "B2AI_ORG:115", "matches": 3, "returned": 3}
        assert by_org["B2AI_ORG:116"] == {"orgId": "B2AI_ORG:116", "matches": 0, "returned": 0}
        assert by_org["B2AI_ORG:117"] == {"orgId": "B2AI_ORG:117", "matches": 0, "returned": 0}

        returned_org_ids = [r["orgId"] for r in result["results"]]
        assert returned_org_ids.count("B2AI_ORG:114") == 17
        assert returned_org_ids.count("B2AI_ORG:115") == 3

    def test_search_preserves_each_orgs_own_hit_order(self):
        result = search_d4d({"query": "widget"})
        org114_fields = [
            r["field"] for r in result["results"] if r["orgId"] == "B2AI_ORG:114"
        ]
        assert org114_fields == sorted(org114_fields, key=lambda f: int(f.rsplit("_", 1)[-1]))


# ---------------------------------------------------------------------------
# D4D cache poisoning: an empty/unusable org-names bundle must not
# permanently mark the warm-container cache as "loaded" and block a later
# retry (the underlying data file's own retry behavior is covered by
# TestD4DDataLoading above).
# ---------------------------------------------------------------------------

class TestD4DOrgNamesCachePoisoning:
    @patch("lambda_function._run_query")
    def test_ensure_d4d_org_names_loaded_retries_after_empty_bundle(self, mock_run_query):
        mock_run_query.return_value = _bundle(["id", "name"], [])
        lambda_function._ensure_d4d_org_names_loaded()
        assert lambda_function._D4D_ORG_NAMES == {}

        mock_run_query.return_value = _bundle(
            ["id", "name"], [["B2AI_ORG:114", "Sample Grand Challenge"]]
        )
        lambda_function._ensure_d4d_org_names_loaded()
        assert lambda_function._D4D_ORG_NAMES == {"B2AI_ORG:114": "Sample Grand Challenge"}


# ---------------------------------------------------------------------------
# D4D ops wired into lambda_handler (apiPath + function styles)
# ---------------------------------------------------------------------------

class TestD4DHandlerRouting(_SampleD4DDataMixin):
    @patch("lambda_function._run_query")
    def test_list_d4ds_api_path(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        resp = lambda_handler(_api_event("/d4d-list"), None)
        body = _body(resp)
        assert len(body["d4ds"]) == 2

    @patch("lambda_function._run_query")
    def test_list_d4ds_function(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        resp = lambda_handler(_function_event("listD4Ds"), None)
        body = _body(resp)
        assert len(body["d4ds"]) == 2

    @patch("lambda_function._run_query")
    def test_get_d4d_api_path(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        event = _api_event("/d4d", [
            {"name": "orgId", "value": "B2AI_ORG:114"},
            {"name": "section", "value": "distribution"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["section"] == "distribution"

    @patch("lambda_function._run_query")
    def test_get_d4d_function(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        resp = lambda_handler(
            _function_event("getD4D", [{"name": "orgId", "value": "B2AI_ORG:114"}]), None
        )
        body = _body(resp)
        assert body["orgId"] == "B2AI_ORG:114"
        assert "sections" in body

    @patch("lambda_function._run_query")
    def test_get_d4d_with_field_api_path(self, mock_run_query):
        event = _api_event("/d4d", [
            {"name": "orgId", "value": "B2AI_ORG:114"},
            {"name": "field", "value": "license"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["field"] == "license"
        assert body["text"] == "CC BY-NC 4.0"

    @patch("lambda_function._run_query")
    def test_get_d4d_with_field_function(self, mock_run_query):
        resp = lambda_handler(
            _function_event("getD4D", [
                {"name": "orgId", "value": "B2AI_ORG:114"},
                {"name": "field", "value": "license"},
            ]),
            None,
        )
        body = _body(resp)
        assert body["field"] == "license"

    @patch("lambda_function._run_query")
    def test_compare_d4d_api_path(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        resp = lambda_handler(
            _api_event("/d4d-compare", [{"name": "field", "value": "license"}]), None
        )
        body = _body(resp)
        assert body["field"] == "license"
        assert len(body["orgs"]) == len(GC_ORG_IDS)

    @patch("lambda_function._run_query")
    def test_compare_d4d_function(self, mock_run_query):
        self._patch_org_names(mock_run_query)
        resp = lambda_handler(
            _function_event("compareD4D", [{"name": "field", "value": "license"}]), None
        )
        body = _body(resp)
        assert body["field"] == "license"

    @patch("lambda_function._run_query")
    def test_search_d4d_api_path(self, mock_run_query):
        resp = lambda_handler(
            _api_event("/d4d-search", [{"name": "query", "value": "consent"}]), None
        )
        body = _body(resp)
        assert body["query"] == "consent"

    @patch("lambda_function._run_query")
    def test_search_d4d_function(self, mock_run_query):
        resp = lambda_handler(
            _function_event("searchD4D", [{"name": "query", "value": "consent"}]), None
        )
        body = _body(resp)
        assert len(body["results"]) > 0


# ---------------------------------------------------------------------------
# d4d_data.json contract — validated against the real bundled file when
# present (it's generated by a separate tool, not committed by this change,
# so this test is a no-op skip until that file lands).
# ---------------------------------------------------------------------------

class TestRealD4DDataContract:
    def test_real_data_file_matches_contract(self):
        path = lambda_function._D4D_DATA_PATH
        if not os.path.exists(path):
            pytest.skip("d4d_data.json not present")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        assert set(data) >= {"source", "sections", "fields", "docs"}

        docs = data["docs"]
        assert len(docs) == 4
        assert set(docs.keys()) == set(GC_ORG_IDS)

        sections = data["sections"]
        section_ids = [s["id"] for s in sections]
        assert section_ids[-1] == "other"
        assert len(section_ids) == len(set(section_ids)), "duplicate section ids"

        # Exact match: the contract says schemaSection is a sections[].id.
        # The Lambda's `_d4d_section_key` tolerates a heading in its place,
        # but a build that emits one is a build bug and should fail here.
        fields = data["fields"]
        assert fields
        for key, meta in fields.items():
            assert meta.get("schemaSection") in section_ids, (
                f"field {key!r} has schemaSection {meta.get('schemaSection')!r}, "
                f"which isn't one of {section_ids!r}"
            )
            assert isinstance(meta.get("inSchema"), bool), f"field {key!r} inSchema must be a bool"
            assert meta.get("label"), f"field {key!r} is missing a label"

        for org_id, doc in docs.items():
            assert doc.get("title"), f"{org_id} is missing a title"
            assert isinstance(doc.get("data"), dict), f"{org_id}.data must be an object"
            for key in doc["data"]:
                assert key in fields, f"{org_id} has an undeclared top-level field {key!r}"

        source = data["source"]
        for key in ("repo", "commit", "synapseTable"):
            assert source.get(key), f"source.{key} is missing"


# ---------------------------------------------------------------------------
# getD4D(orgId) outline size cap — _d4d_fit_outline
# ---------------------------------------------------------------------------

class TestD4DOutlineBudget:
    def test_fit_outline_is_a_noop_under_budget(self):
        outline = {
            "sections": [
                {"id": "s", "fields": [{"id": "a", "label": "A", "size": 1, "portalSection": None}],
                 "fieldsTruncated": False},
            ],
        }
        fitted = lambda_function._d4d_fit_outline(json.loads(json.dumps(outline)))
        assert fitted == outline

    def test_fit_outline_trims_largest_section_and_flags_it(self):
        big_field = {"label": "x" * 200, "size": 1, "portalSection": None}
        many_fields = [dict(big_field, id=f"f{i}") for i in range(300)]
        outline = {
            "orgId": "B2AI_ORG:114",
            "orgName": "X",
            "orgLink": "/x",
            "title": "T",
            "source": {},
            "sections": [
                {"id": "big", "heading": "Big", "description": "", "size": 1,
                 "fields": many_fields, "fieldsTruncated": False},
                {"id": "small", "heading": "Small", "description": "", "size": 1,
                 "fields": [dict(big_field, id="s1")], "fieldsTruncated": False},
            ],
        }
        fitted = lambda_function._d4d_fit_outline(outline)
        assert lambda_function._d4d_outline_size(fitted) <= lambda_function.D4D_OUTLINE_BUDGET

        big_section = next(s for s in fitted["sections"] if s["id"] == "big")
        small_section = next(s for s in fitted["sections"] if s["id"] == "small")
        assert big_section["fieldsTruncated"] is True
        assert len(big_section["fields"]) < 300
        assert small_section["fieldsTruncated"] is False
        assert len(small_section["fields"]) == 1

    @patch("lambda_function._run_query")
    def test_get_d4d_outline_end_to_end_stays_under_budget(self, mock_run_query, tmp_path, monkeypatch):
        mock_run_query.side_effect = _d4d_org_names_side_effect({"B2AI_ORG:114": "Big Org"})
        n = 400
        fields_meta = {
            f"field_{i}": {
                "label": f"Field Number {i} With A Somewhat Long Descriptive Label",
                "description": None,
                "descriptionSource": None,
                "extraSource": None,
                "schemaSection": "other",
                "portalSection": None,
                "inSchema": True,
            }
            for i in range(n)
        }
        data = {
            "source": {"repo": "r", "commit": "c", "synapseTable": "syn1.1"},
            "sections": [{"id": "other", "heading": "Other", "description": ""}],
            "fields": fields_meta,
            "docs": {
                "B2AI_ORG:114": {
                    "gc": "X",
                    "title": "Big Doc",
                    "data": {k: f"value for {k}" for k in fields_meta},
                }
            },
        }
        path = tmp_path / "big_d4d_data.json"
        path.write_text(json.dumps(data))
        monkeypatch.setattr(lambda_function, "_D4D_DATA_PATH", str(path))

        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert len(json.dumps(result)) <= lambda_function.D4D_OUTLINE_BUDGET
        other = next(s for s in result["sections"] if s["id"] == "other")
        assert other["fieldsTruncated"] is True
        assert len(other["fields"]) < n

# ---------------------------------------------------------------------------
# SQL table allowlist
# ---------------------------------------------------------------------------

class TestSqlTableAllowlist:
    def _run(self, sql, table="standards"):
        with patch("lambda_function._run_query") as run:
            run.return_value = {"queryResult": {"queryResults": {"headers": [], "rows": []}}, "queryCount": 0}
            result = lambda_function.sql_query({"table": table, "sql": sql})
        return result, run

    def test_placeholder_query_runs(self):
        result, run = self._run("SELECT * FROM {table} WHERE category = 'Registry'")
        assert "error" not in result
        assert run.call_args[0][1] == f"SELECT * FROM {TABLES['standards']} WHERE category = 'Registry'"

    def test_other_table_in_from_rejected(self):
        result, run = self._run("SELECT * FROM syn68258237")
        assert "error" in result
        run.assert_not_called()

    def test_unpinned_version_of_same_table_rejected(self):
        result, run = self._run("SELECT * FROM syn65676531")
        assert "error" in result
        run.assert_not_called()

    def test_arbitrary_table_rejected(self):
        result, run = self._run("SELECT * FROM syn12345678")
        assert "error" in result
        run.assert_not_called()

    def test_syn_id_inside_string_literal_allowed(self):
        result, run = self._run("SELECT * FROM {table} WHERE description LIKE '%syn123%'")
        assert "error" not in result
        run.assert_called_once()

    def test_double_quoted_string_does_not_hide_a_synid(self):
        # Regression for the table-guard bypass: a single quote inside a
        # double-quoted identifier ("a'") used to pair with an unrelated
        # later single quote, masking `syn99999` as if it were inside a
        # string literal.
        result, run = self._run(
            "SELECT \"a'\" FROM syn99999 WHERE x = 'y'"
        )
        assert "error" in result
        assert "syn99999" in result["error"]
        run.assert_not_called()

    def test_double_quoted_table_identifier_rejected(self):
        result, run = self._run('SELECT * FROM "syn99999"')
        assert "error" in result
        run.assert_not_called()

    def test_backtick_quoted_table_identifier_rejected(self):
        result, run = self._run("SELECT * FROM `syn99999`")
        assert "error" in result
        run.assert_not_called()

    def test_line_comment_rejected(self):
        result, run = self._run("SELECT * FROM {table} -- syn99999")
        assert "error" in result
        run.assert_not_called()

    def test_block_comment_rejected(self):
        result, run = self._run("SELECT * FROM {table} /* syn99999 */")
        assert "error" in result
        run.assert_not_called()

    def test_escaped_single_quote_in_string_literal_allowed(self):
        result, run = self._run(
            "SELECT * FROM {table} WHERE description LIKE 'it''s a %syn123% test'"
        )
        assert "error" not in result
        run.assert_called_once()

    def test_unterminated_single_quote_rejected(self):
        result, run = self._run("SELECT * FROM {table} WHERE x = 'oops")
        assert "error" in result
        run.assert_not_called()

    def test_unterminated_double_quote_rejected(self):
        result, run = self._run('SELECT "oops FROM {table}')
        assert "error" in result
        run.assert_not_called()


class TestSqlStripStringLiterals:
    """Direct unit tests of the quote/comment scanner used by the SQL
    table-guard, independent of sql_query's table resolution."""

    def test_double_quoted_identifier_with_embedded_single_quote(self):
        # The confirmed exploit: a lone ' inside a "..." identifier must not
        # be treated as opening a string literal.
        sql = "SELECT \"a'\" FROM syn99999 WHERE x = 'y'"
        result = _check_sql_tables(sql, TABLES["standards"])
        assert result is not None
        assert "syn99999" in result

    def test_backtick_escape_doubled(self):
        out = _sql_strip_string_literals("SELECT `a``b` FROM t")
        assert out == "SELECT `a``b` FROM t"

    def test_double_quote_escape_doubled(self):
        out = _sql_strip_string_literals('SELECT "a""b" FROM t')
        assert out == 'SELECT "a""b" FROM t'

    def test_single_quote_literal_is_blanked(self):
        out = _sql_strip_string_literals("SELECT * FROM t WHERE x = 'syn123'")
        assert "syn123" not in out

    def test_unterminated_single_quote_raises(self):
        with pytest.raises(_SqlScanError):
            _sql_strip_string_literals("SELECT * FROM t WHERE x = 'oops")

    def test_unterminated_backtick_raises(self):
        with pytest.raises(_SqlScanError):
            _sql_strip_string_literals("SELECT * FROM `oops")

    def test_line_comment_raises(self):
        with pytest.raises(_SqlScanError):
            _sql_strip_string_literals("SELECT * FROM t -- comment")

    def test_block_comment_raises(self):
        with pytest.raises(_SqlScanError):
            _sql_strip_string_literals("SELECT * FROM t /* comment */")


def test_facet_url_is_deterministic():
    params = {"resourceType": "search", "facets": [{"columnName": "topic", "values": ["Image"]}]}
    assert build_portal_url(params) == build_portal_url(params)
