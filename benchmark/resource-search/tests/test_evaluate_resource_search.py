"""Unit tests for evaluate_resource_search.py's trace-parsing and grading
functions, using a small synthetic Bedrock trace fixture -- no AWS calls.

Run with:
    python3 -m pytest benchmark/resource-search -q
"""

import json
import sys
from pathlib import Path

import pytest

import evaluate_resource_search as ev
import lambda_function as lf

HERE = Path(__file__).resolve().parent
BENCH_DIR = HERE.parent


# ---------------------------------------------------------------------------
# Synthetic trace fixture
# ---------------------------------------------------------------------------

def _action_group_input_output(api_path, properties, output_body):
    """Build the (invocationInput trace, observation trace) pair for one
    action-group call, matching real Bedrock orchestrationTrace shape."""
    input_trace = {
        "orchestrationTrace": {
            "invocationInput": {
                "invocationType": "ACTION_GROUP",
                "actionGroupInvocationInput": {
                    "actionGroupName": "b2ai-sql-actions",
                    "apiPath": api_path,
                    "verb": "POST",
                    "requestBody": {
                        "content": {
                            "application/json": {"properties": properties}
                        }
                    },
                },
            }
        }
    }
    output_trace = {
        "orchestrationTrace": {
            "observation": {
                "type": "ACTION_GROUP",
                "actionGroupInvocationOutput": {"text": json.dumps(output_body)},
            }
        }
    }
    return input_trace, output_trace


def _action_group_input_output_real_shape(api_path, properties, output_body):
    """Same as `_action_group_input_output`, but with `requestBody.content`
    in the REAL Bedrock agent-runtime trace shape: a bare list of Parameter
    objects per media type (InvocationInput.ActionGroupInvocationInput
    .requestBody, type RequestBody), not the Lambda-invocation-event shape's
    {"properties": [...]} wrapper."""
    input_trace = {
        "orchestrationTrace": {
            "invocationInput": {
                "invocationType": "ACTION_GROUP",
                "actionGroupInvocationInput": {
                    "actionGroupName": "b2ai-sql-actions",
                    "apiPath": api_path,
                    "verb": "POST",
                    "requestBody": {
                        "content": {
                            "application/json": properties
                        }
                    },
                },
            }
        }
    }
    output_trace = {
        "orchestrationTrace": {
            "observation": {
                "type": "ACTION_GROUP",
                "actionGroupInvocationOutput": {"text": json.dumps(output_body)},
            }
        }
    }
    return input_trace, output_trace


@pytest.fixture
def real_shape_trace_events():
    """A synthetic trace using the REAL (list-shaped) requestBody.content --
    sqlQuery, and buildPortalUrl with `facets` sent as a JSON string (Bedrock
    always stringifies non-scalar property values, per lambda_function.py's
    `_coerce_property_value` docstring)."""
    events = []

    i, o = _action_group_input_output_real_shape(
        "/sql-query",
        [
            {"name": "table", "type": "string", "value": "standards"},
            {"name": "sql", "type": "string",
             "value": "SELECT id, acronym FROM {table} WHERE \"acronym\" = 'FHIR'"},
            {"name": "limit", "type": "integer", "value": "25"},
        ],
        {"count": 1, "rows": [{"id": "B2AI_STANDARD:109", "acronym": "FHIR"}]},
    )
    events += [i, o]

    facets_url = lf.build_portal_url({
        "resourceType": "search",
        "facets": [{"columnName": "topic", "values": ["Image"]}],
    })["url"]
    i, o = _action_group_input_output_real_shape(
        "/portal-url",
        [
            {"name": "resourceType", "type": "string", "value": "search"},
            {"name": "facets", "type": "array",
             "value": json.dumps([{"columnName": "topic", "values": ["Image"]}])},
        ],
        {"url": facets_url},
    )
    events += [i, o]

    return events


@pytest.fixture
def synthetic_trace_events():
    """A synthetic multi-call trace: sqlQuery, buildPortalUrl (facets AND),
    buildPortalUrl (multi-word searchTerm -- the forbidden shape),
    getD4D (outline then a section), and searchD4D."""
    events = []

    # 1. sqlQuery: keyword lookup for FHIR
    i, o = _action_group_input_output(
        "/sql-query",
        [
            {"name": "table", "type": "string", "value": "standards"},
            {"name": "sql", "type": "string",
             "value": "SELECT id, acronym, name, category FROM {table} WHERE \"acronym\" = 'FHIR'"},
            {"name": "limit", "type": "integer", "value": "25"},
        ],
        {
            "count": 1,
            "headers": ["id", "acronym", "name", "category"],
            "rows": [{
                "id": "B2AI_STANDARD:109", "acronym": "FHIR",
                "name": "Fast Healthcare Interoperability Resources",
                "category": "Biomedical Standard",
            }],
        },
    )
    events += [i, o]

    # 2. buildPortalUrl: facets AND across two columns (correct shape)
    facets_url = lf.build_portal_url({
        "resourceType": "search",
        "facets": [
            {"columnName": "topic", "values": ["Image"]},
            {"columnName": "category", "values": ["Ontology or Vocabulary"]},
        ],
    })["url"]
    i, o = _action_group_input_output(
        "/portal-url",
        [
            {"name": "resourceType", "type": "string", "value": "search"},
            {"name": "facets", "type": "array",
             "value": json.dumps([
                 {"columnName": "topic", "values": ["Image"]},
                 {"columnName": "category", "values": ["Ontology or Vocabulary"]},
             ])},
        ],
        {"url": facets_url},
    )
    events += [i, o]

    # 3. buildPortalUrl: multi-word searchTerm (the forbidden AND-via-words shape)
    bad_url = lf.build_portal_url({"resourceType": "search", "searchTerm": "imaging genomics"})["url"]
    i, o = _action_group_input_output(
        "/portal-url",
        [
            {"name": "resourceType", "type": "string", "value": "search"},
            {"name": "searchTerm", "type": "string", "value": "imaging genomics"},
        ],
        {"url": bad_url},
    )
    events += [i, o]

    # 4. getD4D: outline (no section)
    i, o = _action_group_input_output(
        "/d4d",
        [{"name": "orgId", "type": "string", "value": "B2AI_ORG:116"}],
        {
            "orgId": "B2AI_ORG:116", "orgName": "Functional Genomics Grand Challenge",
            "title": "CM4AI Dataset Documentation",
            "sections": [{"id": "human-subjects", "heading": "Human Subjects", "description": "", "size": 592}],
        },
    )
    events += [i, o]

    # 5. getD4D: one section
    i, o = _action_group_input_output(
        "/d4d",
        [
            {"name": "orgId", "type": "string", "value": "B2AI_ORG:116"},
            {"name": "section", "type": "string", "value": "human-subjects"},
        ],
        {
            "orgId": "B2AI_ORG:116", "section": "human-subjects",
            "text": "Does the dataset relate to people?\n\n**Human Subject Research**\nInvolves Human Subjects: False",
        },
    )
    events += [i, o]

    # 6. searchD4D: cross-GC (no orgId)
    i, o = _action_group_input_output(
        "/d4d-search",
        [{"name": "query", "type": "string", "value": "consent"}],
        {"query": "consent", "results": [
            {"orgId": "B2AI_ORG:114", "section": "human-subjects", "heading": "Human Subjects", "snippet": "...consent..."},
        ]},
    )
    events += [i, o]

    # `events` is already a flat list of {"orchestrationTrace": {...}} dicts,
    # each representing one event["trace"]["trace"] payload, in call order.
    return events


# ---------------------------------------------------------------------------
# extract_tool_calls
# ---------------------------------------------------------------------------

def test_extract_tool_calls_decodes_all_six(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    functions = [c["function"] for c in calls]
    assert functions == ["sqlQuery", "buildPortalUrl", "buildPortalUrl", "getD4D", "getD4D", "searchD4D"]


def test_extract_tool_calls_decodes_sql_params(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    sql_call = calls[0]
    assert sql_call["params"]["table"] == "standards"
    assert "FHIR" in sql_call["params"]["sql"]
    assert sql_call["output"]["rows"][0]["id"] == "B2AI_STANDARD:109"


def test_extract_tool_calls_decodes_array_facets_param(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    facets_call = calls[1]
    assert facets_call["params"]["resourceType"] == "search"
    assert isinstance(facets_call["params"]["facets"], list)
    assert facets_call["params"]["facets"][0]["columnName"] == "topic"


def test_extract_tool_calls_decodes_d4d_params(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    outline_call, section_call, search_call = calls[3], calls[4], calls[5]
    assert outline_call["params"]["orgId"] == "B2AI_ORG:116"
    assert "section" not in outline_call["params"]
    assert section_call["params"]["section"] == "human-subjects"
    assert search_call["params"]["query"] == "consent"
    assert "orgId" not in search_call["params"]


# ---------------------------------------------------------------------------
# Real (list-shaped) Bedrock trace requestBody.content -- Fix 1
# ---------------------------------------------------------------------------

def test_extract_tool_calls_real_trace_shape_sql_query(real_shape_trace_events):
    """A real InvocationInput trace's requestBody.content["application/json"]
    is a bare LIST of Parameter objects, not {"properties": [...]}. Decoding
    must still work identically to the dict-shaped fixture."""
    calls = ev.extract_tool_calls(real_shape_trace_events)
    sql_call = calls[0]
    assert sql_call["function"] == "sqlQuery"
    assert sql_call["params"]["table"] == "standards"
    assert "FHIR" in sql_call["params"]["sql"]
    assert sql_call["output"]["rows"][0]["id"] == "B2AI_STANDARD:109"


def test_extract_tool_calls_real_trace_shape_build_portal_url_facets(real_shape_trace_events):
    """`facets` (a JSON string, per Bedrock's always-stringified non-scalar
    property values) must still decode into a real Python list even when
    requestBody.content is list-shaped."""
    calls = ev.extract_tool_calls(real_shape_trace_events)
    facets_call = calls[1]
    assert facets_call["function"] == "buildPortalUrl"
    assert facets_call["params"]["resourceType"] == "search"
    assert isinstance(facets_call["params"]["facets"], list)
    assert facets_call["params"]["facets"][0] == {"columnName": "topic", "values": ["Image"]}


def test_normalize_request_body_handles_missing_and_empty():
    """A missing/empty/None requestBody must normalize to an empty content
    map, not raise -- this is the default until any observation arrives."""
    assert ev._normalize_request_body(None) == {"content": {}}
    assert ev._normalize_request_body({}) == {"content": {}}
    assert ev._normalize_request_body({"content": {}}) == {"content": {}}


def test_normalize_request_body_dict_shape_passthrough():
    """The dict/{"properties": [...]} shape (Lambda invocation event, and
    this benchmark's original fixtures) must pass through unchanged."""
    body = {"content": {"application/json": {"properties": [{"name": "x", "type": "string", "value": "y"}]}}}
    assert ev._normalize_request_body(body) == body


def test_normalize_request_body_list_shape_wrapped():
    """The real trace's list shape must be wrapped into {"properties": [...]}."""
    body = {"content": {"application/json": [{"name": "x", "type": "string", "value": "y"}]}}
    normalized = ev._normalize_request_body(body)
    assert normalized == {
        "content": {"application/json": {"properties": [{"name": "x", "type": "string", "value": "y"}]}}
    }


# ---------------------------------------------------------------------------
# qw0 decoding
# ---------------------------------------------------------------------------

def test_decode_qw0_roundtrip_matches_lambda_encoding():
    url = lf.build_portal_url({
        "resourceType": "search",
        "facets": [{"columnName": "topic", "values": ["Image", "Genome"]}],
    })["url"]
    decoded = ev.decode_qw0(url)
    assert decoded == {
        "selectedFacets": [
            {
                "concreteType": "org.sagebionetworks.repo.model.table.FacetColumnValuesRequest",
                "columnName": "topic",
                "facetValues": ["Image", "Genome"],
            }
        ]
    }


def test_decode_qw0_returns_none_without_qw0():
    assert ev.decode_qw0("/Search/Standards?SEARCH_TERM=FHIR") is None


def test_facets_as_dict():
    selected = [
        {"columnName": "topic", "facetValues": ["Genome", "Image"]},
        {"columnName": "category", "facetValues": ["Ontology or Vocabulary"]},
    ]
    assert ev.facets_as_dict(selected) == {
        "topic": ["Genome", "Image"],
        "category": ["Ontology or Vocabulary"],
    }


# ---------------------------------------------------------------------------
# Constraint checkers
# ---------------------------------------------------------------------------

def test_sql_query_and_columns_passes_with_and():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'"}
    failures = ev.check_sql_query_constraints(params, {
        "table": "standards", "and_columns": ["topic", "category"],
        "required_values": ["Image", "Ontology or Vocabulary"],
    })
    assert failures == []


def test_sql_query_and_columns_fails_without_and():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"topic\" HAS ('Image')"}
    failures = ev.check_sql_query_constraints(params, {
        "table": "standards", "and_columns": ["topic", "category"],
    })
    assert any("AND" in f or "missing" in f for f in failures)
    assert failures  # non-empty: this is the AND-vs-OR bug class the benchmark targets


def test_sql_query_forbid_values_catches_boolean_bug_class():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"usedInBridge2AI\" = true"}
    failures = ev.check_sql_query_constraints(params, {
        "table": "standards", "forbid_values": ["true", "false"],
    })
    assert any("forbidden value" in f for f in failures)


def test_sql_query_forbid_values_passes_on_correct_string_flag():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"usedInBridge2AI\" = 'Yes'"}
    failures = ev.check_sql_query_constraints(params, {
        "table": "standards", "forbid_values": ["true", "false"],
        "required_values": ["Yes"],
    })
    assert failures == []


def test_sql_query_or_column_passes_with_has():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"topic\" HAS ('Image', 'Genome')"}
    failures = ev.check_sql_query_constraints(params, {
        "or_column": "topic", "or_values": ["Image", "Genome"],
    })
    assert failures == []


def test_sql_query_requires_table_placeholder():
    """Lambda commit 22de2a2 rejects SQL that doesn't use the {table}
    placeholder -- an agent hardcoding a synId in FROM is now a hard error,
    not just a style nit, so grading must catch it too."""
    params = {"table": "standards", "sql": "SELECT * FROM syn65676531.99 WHERE \"acronym\" = 'FHIR'"}
    failures = ev.check_sql_query_constraints(params, {"table": "standards"})
    assert any("{table}" in f for f in failures)


def test_sql_query_placeholder_present_passes():
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"acronym\" = 'FHIR'"}
    failures = ev.check_sql_query_constraints(params, {"table": "standards"})
    assert failures == []


def test_sql_query_stray_syn_id_alongside_placeholder_fails():
    params = {
        "table": "standards",
        "sql": "SELECT * FROM {table} WHERE id IN (SELECT id FROM syn68258237.15 WHERE 1=1)",
    }
    failures = ev.check_sql_query_constraints(params, {"table": "standards"})
    assert any("syn68258237.15" in f for f in failures)


def test_sql_query_syn_id_inside_string_literal_is_not_flagged():
    # A synId appearing only inside a quoted string literal (e.g. as part of
    # a filtered value) is not a table reference and must not be flagged.
    params = {"table": "standards", "sql": "SELECT * FROM {table} WHERE \"URL\" = 'https://x/syn123'"}
    failures = ev.check_sql_query_constraints(params, {"table": "standards"})
    assert failures == []


def test_build_portal_url_facets_exact_match():
    output = {"url": lf.build_portal_url({
        "resourceType": "search",
        "facets": [{"columnName": "topic", "values": ["Image"]}, {"columnName": "category", "values": ["Ontology or Vocabulary"]}],
    })["url"]}
    params = {"resourceType": "search"}
    failures = ev.check_build_portal_url_constraints(params, output, {
        "required_facets": [
            {"columnName": "topic", "values": ["Image"]},
            {"columnName": "category", "values": ["Ontology or Vocabulary"]},
        ],
        "facet_match": "exact",
    })
    assert failures == []


def test_build_portal_url_facets_subset_allows_extra():
    output = {"url": lf.build_portal_url({
        "resourceType": "search",
        "facets": [
            {"columnName": "topic", "values": ["Image"]},
            {"columnName": "category", "values": ["Ontology or Vocabulary"]},
            {"columnName": "mature", "values": ["Is Mature"]},
        ],
    })["url"]}
    failures = ev.check_build_portal_url_constraints({"resourceType": "search"}, output, {
        "required_facets": [{"columnName": "topic", "values": ["Image"]}],
        "facet_match": "subset",
    })
    assert failures == []


def test_build_portal_url_facets_output_none_does_not_crash():
    """`output` is None until a matching observation arrives in the trace
    (extract_tool_calls's default) -- required_facets checking must not
    crash on that, and must record exactly one clear failure."""
    failures = ev.check_build_portal_url_constraints(
        {"resourceType": "search"}, None,
        {"required_facets": [{"columnName": "topic", "values": ["Image"]}]},
    )
    assert len(failures) == 1
    assert "no url" in failures[0]


def test_build_portal_url_facets_error_output_no_url_single_failure():
    """buildPortalUrl returning an error (no url key) must fail the
    required_facets check exactly once, with the error surfaced."""
    output = {"error": "Unknown resourceType 'bogus'"}
    failures = ev.check_build_portal_url_constraints(
        {"resourceType": "bogus"}, output,
        {"required_facets": [{"columnName": "topic", "values": ["Image"]}]},
    )
    assert len(failures) == 1
    assert "no url" in failures[0]
    assert "bogus" in failures[0]


def test_build_portal_url_facets_qw0_decode_failure_single_failure():
    """A qw0 that fails to decode must be counted once, not twice (the old
    code fell through to a second, differently-worded 'no qw0 param'
    failure for the same call)."""
    output = {"url": "/Search/Standards?qw0=not-valid-base64!!!"}
    failures = ev.check_build_portal_url_constraints(
        {"resourceType": "search"}, output,
        {"required_facets": [{"columnName": "topic", "values": ["Image"]}]},
    )
    assert len(failures) == 1
    assert "failed to decode" in failures[0]


def test_grade_answer_facts_redirect_output_none_does_not_crash():
    """A buildPortalUrl call whose output is still None (no observation yet)
    must not crash grade_answer_facts's redirect-category check."""
    item = {"category": "redirect", "expected_answer": {"url_path": "/Explore/Standard/DetailsPage?id=B2AI_STANDARD:109"}}
    tool_calls = [{"function": "buildPortalUrl", "params": {}, "output": None}]
    result = ev.grade_answer_facts(item, tool_calls, "here you go")
    assert result["verified"] is None  # no url found in any call -> falls through


def test_build_portal_url_forbidden_multiword_search_term():
    params = {"resourceType": "search", "searchTerm": "imaging genomics"}
    failures = ev.check_build_portal_url_constraints(params, {"url": ""}, {
        "search_term_forbidden_multi_word": True,
    })
    assert any("multiple words" in f for f in failures)


def test_build_portal_url_single_word_search_term_ok():
    params = {"resourceType": "search", "searchTerm": "FHIR"}
    failures = ev.check_build_portal_url_constraints(params, {"url": ""}, {
        "search_term_forbidden_multi_word": True,
        "search_term_exact": "FHIR",
    })
    assert failures == []


def test_get_d4d_outline_vs_section_constraint():
    assert ev.check_get_d4d_constraints({"orgId": "B2AI_ORG:114"}, {"orgId": "B2AI_ORG:114", "section": None}) == []
    failures = ev.check_get_d4d_constraints(
        {"orgId": "B2AI_ORG:114", "section": "motivation"}, {"orgId": "B2AI_ORG:114", "section": None}
    )
    assert failures  # expected outline, got a section call


def test_search_d4d_cross_gc_constraint():
    assert ev.check_search_d4d_constraints({"query": "consent"}, {"query_contains": "consent", "orgId_must_be_absent": True}) == []
    failures = ev.check_search_d4d_constraints(
        {"query": "consent", "orgId": "B2AI_ORG:114"}, {"query_contains": "consent", "orgId_must_be_absent": True}
    )
    assert failures


# ---------------------------------------------------------------------------
# grade_tool_calls (end to end against the synthetic trace)
# ---------------------------------------------------------------------------

def test_grade_tool_calls_correct_shape_scores_1(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    item = {
        "expected_tool_calls": [
            {"function": "sqlQuery", "constraints": {"table": "standards", "required_values": ["FHIR"]}},
        ]
    }
    result = ev.grade_tool_calls(item, calls)
    assert result["score"] == 1.0
    assert not result["any_required_missing"]


def test_grade_tool_calls_flags_forbidden_multiword(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    # Only the 2nd buildPortalUrl call (index 2) used the bad multi-word term;
    # grade_tool_calls picks the BEST-matching call among same-function
    # candidates, so pin it down by requiring the exact bad searchTerm via a
    # constraint that only that call can satisfy.
    item = {
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {"search_term_exact": "imaging genomics", "search_term_forbidden_multi_word": True},
            },
        ]
    }
    result = ev.grade_tool_calls(item, calls)
    assert result["score"] == 0.0
    entry = result["per_call"][0]
    assert entry["matched"]
    assert any("multiple words" in f for f in entry["failures"])


def test_grade_tool_calls_missing_required_call():
    item = {"expected_tool_calls": [{"function": "countByType", "constraints": {}}]}
    result = ev.grade_tool_calls(item, [])
    assert result["score"] == 0.0
    assert result["any_required_missing"]


def test_grade_tool_calls_optional_call_not_penalized(synthetic_trace_events):
    calls = ev.extract_tool_calls(synthetic_trace_events)
    item = {
        "expected_tool_calls": [
            {"function": "sqlQuery", "constraints": {"table": "standards"}},
            {"function": "listD4Ds", "required": False, "constraints": {}},
        ]
    }
    result = ev.grade_tool_calls(item, calls)
    assert result["score"] == 1.0  # listD4Ds not required, absence doesn't hurt
    assert not result["any_required_missing"]


# ---------------------------------------------------------------------------
# grade_answer_facts
# ---------------------------------------------------------------------------

def test_grade_answer_facts_keyword_lookup_verified():
    item = {
        "category": "keyword-lookup",
        "expected_answer": {"id": "B2AI_STANDARD:109", "name": "Fast Healthcare Interoperability Resources"},
    }
    text = "The FHIR standard (B2AI_STANDARD:109), Fast Healthcare Interoperability Resources, is..."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is True


def test_grade_answer_facts_keyword_lookup_fails_when_id_missing():
    item = {"category": "keyword-lookup", "expected_answer": {"id": "B2AI_STANDARD:109", "name": "Fast Healthcare Interoperability Resources"}}
    result = ev.grade_answer_facts(item, [], "FHIR is a healthcare data standard.")
    assert result["verified"] is False


def test_grade_answer_facts_count_item():
    item = {"category": "counts", "expected_answer": {"count": 67}}
    assert ev.grade_answer_facts(item, [], "There are 67 standards about Image.")["verified"] is True
    assert ev.grade_answer_facts(item, [], "There are many standards about Image.")["verified"] is False


def test_grade_answer_facts_redirect_uses_tool_output_not_text():
    item = {"category": "redirect", "expected_answer": {"url_path": "/Explore/Standard/DetailsPage?id=B2AI_STANDARD:109"}}
    tool_calls = [{"function": "buildPortalUrl", "params": {}, "output": {"url": "/Explore/Standard/DetailsPage?id=B2AI_STANDARD:109"}}]
    result = ev.grade_answer_facts(item, tool_calls, "here you go")
    assert result["verified"] is True


def test_grade_answer_facts_d4d_narrative_defers_to_judge():
    item = {"category": "d4d", "expected_answer": {"text_contains": "Involves Human Subjects: False"}}
    result = ev.grade_answer_facts(item, [], "No, CM4AI does not involve human subjects.")
    assert result["verified"] is None  # needs the LLM-judge fallback, not a deterministic verdict


# ---------------------------------------------------------------------------
# Boundary-aware id/count matching -- Fix 4
# ---------------------------------------------------------------------------

def test_id_in_text_does_not_match_as_prefix_of_longer_id():
    """B2AI_ORG:1 must not be considered 'present' just because the longer
    id B2AI_ORG:114 is in the text (plain substring containment would
    false-positive on this)."""
    assert ev._id_in_text("The relevant org is B2AI_ORG:114.", "B2AI_ORG:1") is False
    assert ev._id_in_text("The relevant org is B2AI_ORG:1.", "B2AI_ORG:1") is True


def test_count_in_text_does_not_match_inside_an_id():
    """A count of 3 must not match the '3' inside an unrelated id like
    B2AI_ORG:3."""
    assert ev._count_in_text("See organization B2AI_ORG:3 for details.", 3) is False
    assert ev._count_in_text("There are 3 registries.", 3) is True


def test_count_in_text_does_not_match_inside_a_longer_number():
    assert ev._count_in_text("There are 34 standards.", 3) is False


def test_count_in_text_tolerates_thousands_separator():
    assert ev._count_in_text("The catalog has 1,029 standards.", 1029) is True
    assert ev._count_in_text("The catalog has 1029 standards.", 1029) is True


def test_grade_answer_facts_keyword_lookup_id_boundary():
    """A keyword-lookup item expecting B2AI_STANDARD:1 must not be verified
    by an answer that only mentions the unrelated B2AI_STANDARD:114."""
    item = {
        "category": "keyword-lookup",
        "expected_answer": {"id": "B2AI_STANDARD:1", "name": "Foo"},
    }
    text = "Foo is described under B2AI_STANDARD:114, not what you're looking for."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is False


def test_grade_answer_facts_linked_resource_subject_id_only_fails():
    """An answer that only repeats the subject's OWN id (never any of the
    actual linked ids) must fail -- it proves nothing about whether the
    agent found the link."""
    item = {
        "category": "linked-resource",
        "expected_answer": {
            "org_id": "B2AI_ORG:3",
            "governed_standard_ids": ["B2AI_STANDARD:74", "B2AI_STANDARD:966"],
            "governed_standard_ids_contains": "B2AI_STANDARD:74",
        },
    }
    text = "The organization B2AI_ORG:3 (AMA) is a medical association."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is False


def test_grade_answer_facts_linked_resource_correct_answer_passes():
    item = {
        "category": "linked-resource",
        "expected_answer": {
            "org_id": "B2AI_ORG:3",
            "governed_standard_ids": ["B2AI_STANDARD:74", "B2AI_STANDARD:966"],
            "governed_standard_ids_contains": "B2AI_STANDARD:74",
        },
    }
    text = "The AMA (B2AI_ORG:3) governs CPT (B2AI_STANDARD:74) among other standards."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is True


def test_grade_answer_facts_linked_resource_ignores_subject_id_lookalike():
    """A near-miss on the subject id (e.g. B2AI_ORG:30 instead of the real
    linked id B2AI_ORG:3) must not be mistaken for a match via substring
    containment."""
    item = {
        "category": "linked-resource",
        "expected_answer": {"standard_id": "B2AI_STANDARD:1", "relevant_org_ids": ["B2AI_ORG:3"]},
    }
    text = "This is related to B2AI_ORG:30, a different organization."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is False


# ---------------------------------------------------------------------------
# Dataset / schema self-checks
# ---------------------------------------------------------------------------

def test_dataset_validates_against_schema():
    import jsonschema

    schema = json.loads((BENCH_DIR / "resource_search_schema.json").read_text())
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    jsonschema.validate(dataset, schema)


def test_dataset_item_ids_are_unique():
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    ids = [item["id"] for item in dataset["items"]]
    assert len(ids) == len(set(ids))


def test_dataset_covers_all_categories():
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    categories = {item["category"] for item in dataset["items"]}
    expected = {
        "keyword-lookup", "categorical-filter", "multi-filter-and", "multi-filter-or",
        "linked-resource", "redirect", "d4d", "counts",
    }
    assert expected.issubset(categories)


def test_dataset_item_count_in_expected_range():
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    assert 30 <= len(dataset["items"]) <= 45


def test_dataset_pinned_versions_match_live_lambda_tables():
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    assert dataset["pinned_versions"] == lf.TABLES


def test_rs_counts_by_type_item_restricted_to_four_asked_about_types():
    """The question only asks about standards/datasets/organizations/topics,
    not all 7 pinned tables -- expected_answer.counts must be restricted
    accordingly (build_ground_truth.py's rs_counts_by_type)."""
    dataset = json.loads((BENCH_DIR / "resource_search_dataset.json").read_text())
    item = next(it for it in dataset["items"] if it["id"] == "rs-counts-by-type")
    assert set(item["expected_answer"]["counts"]) == {"standards", "datasets", "organizations", "topics"}


def test_grade_answer_facts_counts_four_count_answer_passes():
    """A `counts` item with exactly the 4 asked-about types (not all 7 pinned
    tables) must verify correctly when the answer mentions all 4."""
    item = {
        "category": "counts",
        "expected_answer": {
            "counts": {"standards": 1029, "datasets": 116, "organizations": 130, "topics": 82}
        },
    }
    text = "The portal has 1,029 standards, 116 datasets, 130 organizations, and 82 topics."
    result = ev.grade_answer_facts(item, [], text)
    assert result["verified"] is True
