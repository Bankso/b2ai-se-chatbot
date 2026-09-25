"""Unit tests for the b2aiSqlRag Lambda function.

All Synapse network calls are mocked so no live token/endpoint is needed.
"""

import base64
import gzip
import json
import os
import socket
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

import lambda_function
from lambda_function import (
    _bare_id,
    _coerce_property_value,
    _make_response,
    _parse_bundle,
    _parse_d4d_document,
    _resolve_table,
    build_portal_url,
    extract_params,
    get_d4d,
    lambda_handler,
    list_d4ds,
    search_d4d,
    GC_ORG_IDS,
    PORTAL_ROUTES,
    TABLES,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


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
# D4D HTML parsing
# ---------------------------------------------------------------------------

class TestParseD4DDocument:
    def setup_method(self):
        self.html = _load_fixture("sample_d4d_org114.html")
        self.doc = _parse_d4d_document(self.html)

    def test_title(self):
        assert self.doc["title"] == "Sample GC Dataset Documentation"

    def test_section_ids_and_headings_in_order(self):
        assert [s["id"] for s in self.doc["sections"]] == [
            "motivation", "composition", "collection-process", "distribution",
        ]
        assert [s["heading"] for s in self.doc["sections"]] == [
            "Motivation", "Composition", "Collection Process", "Distribution",
        ]

    def test_section_description_captured(self):
        motivation = self.doc["sections"][0]
        assert motivation["description"] == "Why was the dataset created?"

    def test_link_rendered_as_markdown(self):
        motivation = self.doc["sections"][0]["text"]
        assert "[https://example.org/datasets/1](https://example.org/datasets/1)" in motivation

    def test_table_rendered_as_rows(self):
        motivation = self.doc["sections"][0]["text"]
        assert "Description: Understand consent workflows in sample data." in motivation
        assert "ID: purpose-001" in motivation
        assert "Name: Understanding consent" in motivation

    def test_nested_dl_in_list_rendered_inline(self):
        motivation = self.doc["sections"][0]["text"]
        assert "ID: funder-001" in motivation
        assert "Name: Sample Funding Program" in motivation
        assert "Funded for testing purposes only." in motivation

    def test_plain_list_rendered_as_bullets(self):
        composition = self.doc["sections"][1]["text"]
        assert "- Sample Keyword" in composition
        assert "- Consent" in composition

    def test_plain_scalar_value(self):
        distribution = self.doc["sections"][3]["text"]
        assert distribution.strip() == "How will the dataset be distributed?\n\n**License**\nCC BY-NC 4.0"

    def test_required_indicator_asterisk_not_leaked_into_label(self):
        motivation = self.doc["sections"][0]["text"]
        assert "**ID**" in motivation
        assert "**ID *" not in motivation
        assert "ID *" not in motivation

    def test_script_and_style_dropped(self):
        full_text = json.dumps(self.doc)
        assert "console.log" not in full_text
        assert "color: red" not in full_text

    def test_ol_with_nested_dl_rendered(self):
        collection = self.doc["sections"][2]["text"]
        assert "ID: instance-001" in collection
        assert "Name: Sample instance" in collection


# ---------------------------------------------------------------------------
# D4D ops — listD4Ds / getD4D / searchD4D
# ---------------------------------------------------------------------------

def _d4d_run_query_side_effect(html_by_org, org_names):
    """A `_run_query` side_effect that answers both the D4D content query
    and the organizations-name lookup query the D4D ops issue."""
    def _side_effect(entity_id, sql, limit, part_mask):
        if "content_text" in sql:
            rows = [[oid, "html", html] for oid, html in html_by_org.items()]
            return _bundle(["content_id", "content_type", "content_text"], rows)
        if sql.strip().startswith("SELECT id, name"):
            rows = [[oid, name] for oid, name in org_names.items()]
            return _bundle(["id", "name"], rows)
        raise AssertionError(f"unexpected sql in D4D test: {sql}")
    return _side_effect


@pytest.fixture(autouse=True)
def _reset_d4d_caches():
    """The D4D ops cache parsed docs/org names in module globals across warm
    invocations -- reset them before/after every test so tests don't leak
    state into each other."""
    lambda_function._D4D_DOCS.clear()
    lambda_function._D4D_ORG_NAMES.clear()
    lambda_function._D4D_LOADED = False
    yield
    lambda_function._D4D_DOCS.clear()
    lambda_function._D4D_ORG_NAMES.clear()
    lambda_function._D4D_LOADED = False


class TestD4DOps:
    HTML_BY_ORG = {
        "B2AI_ORG:114": _load_fixture("sample_d4d_org114.html"),
        "B2AI_ORG:115": _load_fixture("sample_d4d_org115.html"),
    }
    ORG_NAMES = {
        "B2AI_ORG:114": "Sample Grand Challenge",
        "B2AI_ORG:115": "Other Sample Grand Challenge",
    }

    def _patch_run_query(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(self.HTML_BY_ORG, self.ORG_NAMES)

    @patch("lambda_function._run_query")
    def test_list_d4ds(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = list_d4ds({})
        assert [d["orgId"] for d in result["d4ds"]] == ["B2AI_ORG:114", "B2AI_ORG:115"]
        assert result["d4ds"][0]["name"] == "Sample Grand Challenge"
        assert result["d4ds"][0]["link"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"
        assert result["d4ds"][0]["title"] == "Sample GC Dataset Documentation"

    @patch("lambda_function._run_query")
    def test_list_d4ds_skips_orgs_with_no_content(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            {"B2AI_ORG:114": self.HTML_BY_ORG["B2AI_ORG:114"]}, self.ORG_NAMES
        )
        result = list_d4ds({})
        assert [d["orgId"] for d in result["d4ds"]] == ["B2AI_ORG:114"]

    @patch("lambda_function._run_query")
    def test_get_d4d_outline(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert result["orgId"] == "B2AI_ORG:114"
        assert result["orgName"] == "Sample Grand Challenge"
        assert result["orgLink"] == "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"
        assert [s["id"] for s in result["sections"]] == [
            "motivation", "composition", "collection-process", "distribution",
        ]
        assert all("size" in s for s in result["sections"])

    @patch("lambda_function._run_query")
    def test_get_d4d_section_text(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "distribution"})
        assert result["section"] == "distribution"
        assert result["heading"] == "Distribution"
        assert "CC BY-NC 4.0" in result["text"]
        assert "nextOffset" not in result

    @patch("lambda_function._run_query")
    def test_get_d4d_section_pagination(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        with patch("lambda_function.D4D_SECTION_BUDGET", 20):
            page1 = get_d4d({"orgId": "B2AI_ORG:114", "section": "motivation"})
            assert len(page1["text"]) == 20
            assert page1["nextOffset"] == 20
            total = page1["totalLength"]
            assert total > 20

            # Walk the whole section by paging; concatenation must
            # reconstruct the section's original text with no gaps/overlaps.
            collected = page1["text"]
            offset = page1["nextOffset"]
            while True:
                page = get_d4d({
                    "orgId": "B2AI_ORG:114", "section": "motivation", "offset": offset,
                })
                assert page["totalLength"] == total
                collected += page["text"]
                if "nextOffset" not in page:
                    break
                offset = page["nextOffset"]

            expected = _parse_d4d_document(self.HTML_BY_ORG["B2AI_ORG:114"])
            expected_text = next(
                s["text"] for s in expected["sections"] if s["id"] == "motivation"
            )
            assert collected == expected_text

    @patch("lambda_function._run_query")
    def test_get_d4d_missing_org_id(self, mock_run_query):
        assert get_d4d({}) == {"error": "orgId is required"}
        mock_run_query.assert_not_called()

    @patch("lambda_function._run_query")
    def test_get_d4d_unknown_org_id(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:999"})
        assert "error" in result
        assert "B2AI_ORG:114" in result["error"]

    @patch("lambda_function._run_query")
    def test_get_d4d_unknown_section_lists_valid_ids(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = get_d4d({"orgId": "B2AI_ORG:114", "section": "nonexistent"})
        assert "error" in result
        assert "motivation" in result["error"]
        assert "distribution" in result["error"]

    @patch("lambda_function._run_query")
    def test_search_d4d_cross_org(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = search_d4d({"query": "consent"})
        assert result["query"] == "consent"
        org_ids_hit = {r["orgId"] for r in result["results"]}
        assert "B2AI_ORG:114" in org_ids_hit
        assert "B2AI_ORG:115" in org_ids_hit
        for hit in result["results"]:
            assert "consent" in hit["snippet"].lower()
            assert len(hit["snippet"]) <= 300

    @patch("lambda_function._run_query")
    def test_search_d4d_scoped_to_one_org(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = search_d4d({"query": "consent", "orgId": "B2AI_ORG:114"})
        assert all(r["orgId"] == "B2AI_ORG:114" for r in result["results"])

    @patch("lambda_function._run_query")
    def test_search_d4d_missing_query(self, mock_run_query):
        assert search_d4d({}) == {"error": "query is required"}
        mock_run_query.assert_not_called()

    @patch("lambda_function._run_query")
    def test_search_d4d_unknown_org_id(self, mock_run_query):
        self._patch_run_query(mock_run_query)
        result = search_d4d({"query": "consent", "orgId": "B2AI_ORG:999"})
        assert "error" in result

    @patch("lambda_function._run_query", side_effect=Exception("synapse down"))
    def test_get_d4d_load_failure_returns_error(self, _mock):
        result = get_d4d({"orgId": "B2AI_ORG:114"})
        assert "error" in result
        assert "synapse down" in result["error"]


# ---------------------------------------------------------------------------
# D4D ops wired into lambda_handler (apiPath + function styles)
# ---------------------------------------------------------------------------

class TestD4DHandlerRouting:
    @patch("lambda_function._run_query")
    def test_list_d4ds_api_path(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        resp = lambda_handler(_api_event("/d4d-list"), None)
        body = _body(resp)
        assert len(body["d4ds"]) == 2

    @patch("lambda_function._run_query")
    def test_list_d4ds_function(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        resp = lambda_handler(_function_event("listD4Ds"), None)
        body = _body(resp)
        assert len(body["d4ds"]) == 2

    @patch("lambda_function._run_query")
    def test_get_d4d_api_path(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        event = _api_event("/d4d", [
            {"name": "orgId", "value": "B2AI_ORG:114"},
            {"name": "section", "value": "distribution"},
        ])
        resp = lambda_handler(event, None)
        body = _body(resp)
        assert body["section"] == "distribution"

    @patch("lambda_function._run_query")
    def test_get_d4d_function(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        resp = lambda_handler(
            _function_event("getD4D", [{"name": "orgId", "value": "B2AI_ORG:114"}]), None
        )
        body = _body(resp)
        assert body["orgId"] == "B2AI_ORG:114"
        assert "sections" in body

    @patch("lambda_function._run_query")
    def test_search_d4d_api_path(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        resp = lambda_handler(
            _api_event("/d4d-search", [{"name": "query", "value": "consent"}]), None
        )
        body = _body(resp)
        assert body["query"] == "consent"

    @patch("lambda_function._run_query")
    def test_search_d4d_function(self, mock_run_query):
        mock_run_query.side_effect = _d4d_run_query_side_effect(
            TestD4DOps.HTML_BY_ORG, TestD4DOps.ORG_NAMES
        )
        resp = lambda_handler(
            _function_event("searchD4D", [{"name": "query", "value": "consent"}]), None
        )
        body = _body(resp)
        assert len(body["results"]) > 0


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
