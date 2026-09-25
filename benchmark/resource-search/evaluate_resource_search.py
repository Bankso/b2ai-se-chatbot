#!/usr/bin/env python3
"""Evaluate resource-search correctness for the Bridge2AI Standards Explorer Copilot.

Unlike `kb-routing` (which only checks *which* source was used), this grades
whether the agent's actual tool-call *parameters* were correct -- decoded
straight out of the Bedrock orchestration trace's `actionGroupInvocationInput`
events -- and whether its final answer is consistent with ground truth
computed live against Synapse at the pinned table versions (see
`build_ground_truth.py`).

Usage:
    python evaluate_resource_search.py --agent-id ABC123
    python evaluate_resource_search.py --agent-id ABC123 -n 5
    python evaluate_resource_search.py --agent-id ABC123 --item rs-keyword-fhir
    python evaluate_resource_search.py --agent-id ABC123 --no-judge
    python evaluate_resource_search.py --agent-id ABC123 --allow-prod

No B2AI agent has been deployed yet, so --agent-id has no default, and
PROD_AGENT_ID below is a placeholder that won't match a real agent ID until
one exists -- fill it in then so the prod-guard (same pattern as
benchmark/redteam/evaluate_redteam.py) is meaningful.

Trace decoding:
    Each `actionGroupInvocationInput` trace event is adapted into the same
    event shape the Lambda's own `extract_params`/`map_api_path_to_function`
    expect, and decoded with THOSE functions (imported directly from
    lambda_function.py) -- so parameter extraction here exactly matches what
    the deployed Lambda itself would see, including its array-coercion
    workaround for Bedrock's malformed pseudo-JSON array values.
"""

import argparse
import base64
import gzip
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMBDA_DIR = HERE.parent.parent / "agents" / "b2ai-copilot" / "lambda" / "b2aiSqlRag"
sys.path.insert(0, str(LAMBDA_DIR))

import lambda_function as lf  # noqa: E402

PORTAL_ORIGIN = "https://b2ai.standards.synapse.org"

# Placeholder until a real B2AI prod agent exists -- fill in then, same
# pattern as benchmark/redteam/evaluate_redteam.py's PROD_AGENT_ID guard.
PROD_AGENT_ID = "REPLACE_ME_B2AI_PROD_AGENT_ID"


# ---------------------------------------------------------------------------
# qw0 decoding (facet redirect correctness)
# ---------------------------------------------------------------------------

def decode_qw0(url: str):
    """Decode a buildPortalUrl `qw0` query param back to its facet query object.

    Mirrors the Lambda's own self-verify roundtrip in `build_portal_url`
    (urllib-unquote -> base64-decode -> gzip-decompress -> json.loads).
    Returns None if `url` has no `qw0` param; raises ValueError if it's
    present but doesn't decode.
    """
    m = re.search(r"[?&]qw0=([^&]+)", url or "")
    if not m:
        return None
    raw = m.group(1)
    try:
        import urllib.parse
        return json.loads(gzip.decompress(base64.b64decode(urllib.parse.unquote(raw))))
    except Exception as e:
        raise ValueError(f"qw0 failed to decode: {e}") from e


def decode_search_term(url: str):
    m = re.search(r"[?&]SEARCH_TERM=([^&]+)", url or "")
    if not m:
        return None
    import urllib.parse
    return urllib.parse.unquote_plus(m.group(1))


def facets_as_dict(selected_facets):
    """[{columnName, facetValues}, ...] -> {columnName: sorted(values)}."""
    out = {}
    for f in selected_facets or []:
        col = f.get("columnName")
        vals = f.get("facetValues") or []
        out[col] = sorted(vals)
    return out


# ---------------------------------------------------------------------------
# Trace -> tool-call extraction
# ---------------------------------------------------------------------------

def _trace_input_to_lambda_event(inv_input: dict) -> dict:
    """Adapt a Bedrock `actionGroupInvocationInput` trace object into the
    shape lambda_function.py's own `extract_params`/`map_api_path_to_function`
    expect (the same shape the real Lambda invocation event carries)."""
    return {
        "actionGroup": inv_input.get("actionGroupName"),
        "apiPath": inv_input.get("apiPath", ""),
        "httpMethod": inv_input.get("verb", "POST"),
        "function": inv_input.get("function"),
        "parameters": inv_input.get("parameters", []),
        "requestBody": inv_input.get("requestBody", {}),
    }


def extract_tool_calls(trace_events: list) -> list:
    """Pull every actionGroupInvocationInput (+ its matching output, if any)
    out of a list of raw Bedrock trace dicts (each `event["trace"]["trace"]`).

    Returns a list of {"function": str, "params": dict, "output": dict|None}
    in call order. `output` is the parsed JSON body of the corresponding
    actionGroupInvocationOutput observation, when one follows in the trace.
    """
    calls = []
    pending_output_for = None
    for trace_data in trace_events:
        orch = (trace_data or {}).get("orchestrationTrace", {})

        inv_input = orch.get("invocationInput", {})
        if inv_input.get("invocationType") == "ACTION_GROUP":
            agi = inv_input.get("actionGroupInvocationInput", {})
            fake_event = _trace_input_to_lambda_event(agi)
            function = lf.map_api_path_to_function(fake_event["apiPath"]) or fake_event.get("function")
            params = lf.extract_params(fake_event)
            calls.append({"function": function, "params": params, "output": None})
            pending_output_for = len(calls) - 1

        obs = orch.get("observation", {})
        if obs.get("type") == "ACTION_GROUP" and pending_output_for is not None:
            out = obs.get("actionGroupInvocationOutput", {})
            text = out.get("text")
            if text is not None:
                try:
                    calls[pending_output_for]["output"] = json.loads(text)
                except (ValueError, TypeError):
                    calls[pending_output_for]["output"] = {"raw": text}
            pending_output_for = None

    return calls


# ---------------------------------------------------------------------------
# Constraint checking
# ---------------------------------------------------------------------------

def _sql_upper(params: dict) -> str:
    return (params.get("sql") or "").upper()


def _contains_quoted_or_bare(sql: str, name: str) -> bool:
    sql_u = sql.upper()
    return f'"{name.upper()}"' in sql_u or name.upper() in sql_u


def check_sql_query_constraints(params: dict, constraints: dict) -> list:
    """Return a list of failure strings (empty = all constraints passed)."""
    failures = []
    sql = params.get("sql") or ""
    sql_u = sql.upper()

    if "table" in constraints and params.get("table") != constraints["table"]:
        failures.append(f"table: expected {constraints['table']!r}, got {params.get('table')!r}")

    # Universal shape requirement since Lambda commit 22de2a2 ("Restrict SQL
    # queries to the pinned B2AI tables"): the FROM clause must use the
    # literal `{table}` placeholder -- the Lambda now rejects any SQL that
    # names a synId other than the resolved, pinned table (outside string
    # literals), so a correct agent call always uses the placeholder rather
    # than a hardcoded synId. Reuses the Lambda's own regexes so this check
    # stays byte-for-byte consistent with what the deployed Lambda enforces.
    if sql:
        if "{table}" not in sql:
            failures.append(
                "sql does not use the literal '{table}' placeholder in FROM -- "
                "the Lambda (commit 22de2a2) rejects a hardcoded synId here"
            )
        else:
            unquoted = lf._SQL_STRING_LITERAL_RE.sub("''", sql)
            stray_syn_ids = lf._SQL_SYN_ID_RE.findall(unquoted)
            if stray_syn_ids:
                failures.append(
                    f"sql references synId(s) {stray_syn_ids} directly instead of only via "
                    "{table} -- rejected by the Lambda's table-scoping check (commit 22de2a2)"
                )

    for col in constraints.get("required_columns", []):
        if not _contains_quoted_or_bare(sql, col):
            failures.append(f"sql missing required column {col!r}")

    for val in constraints.get("required_values", []):
        if val not in sql:
            failures.append(f"sql missing required literal value {val!r}")

    for val in constraints.get("forbid_values", []):
        # word-boundary match so e.g. forbidding "true" doesn't false-positive
        # on an unrelated substring
        if re.search(rf"(?<![A-Za-z0-9_'\"]){re.escape(val)}(?![A-Za-z0-9_'\"])", sql):
            failures.append(f"sql contains forbidden value {val!r} (boolean-flag bug class)")

    and_columns = constraints.get("and_columns", [])
    if and_columns:
        missing = [c for c in and_columns if not _contains_quoted_or_bare(sql, c)]
        if missing:
            failures.append(f"sql missing AND-columns {missing}")
        and_count = len(re.findall(r"\bAND\b", sql_u))
        if and_count < len(and_columns) - 1:
            failures.append(
                f"sql has {and_count} AND keyword(s), need >= {len(and_columns) - 1} to combine "
                f"{and_columns} — looks OR-only or single-condition, wrong shape for an AND-intent question"
            )

    or_column = constraints.get("or_column")
    if or_column:
        if not _contains_quoted_or_bare(sql, or_column):
            failures.append(f"sql missing OR-column {or_column!r}")
        has_form = "HAS" in sql_u
        or_values = constraints.get("or_values", [])
        present_values = [v for v in or_values if v in sql]
        if has_form:
            if len(present_values) < len(or_values):
                failures.append(f"HAS(...) missing some OR values, expected {or_values}, sql has {present_values}")
        else:
            # accept an explicit "col = v1 OR col = v2" form as an alternative to HAS
            or_count = len(re.findall(r"\bOR\b", sql_u))
            if or_count < len(or_values) - 1 or len(present_values) < len(or_values):
                failures.append(
                    f"sql doesn't use HAS(...) or an explicit OR of {or_values} on {or_column!r}"
                )

    return failures


def check_build_portal_url_constraints(params: dict, output: dict, constraints: dict) -> list:
    failures = []
    if "resourceType" in constraints and params.get("resourceType") != constraints["resourceType"]:
        failures.append(
            f"resourceType: expected {constraints['resourceType']!r}, got {params.get('resourceType')!r}"
        )

    if "id" in constraints and params.get("id") != constraints["id"]:
        failures.append(f"id: expected {constraints['id']!r}, got {params.get('id')!r}")

    search_term = params.get("searchTerm")

    if constraints.get("search_term_exact") is not None:
        if search_term != constraints["search_term_exact"]:
            failures.append(
                f"searchTerm: expected {constraints['search_term_exact']!r}, got {search_term!r}"
            )

    if constraints.get("search_term_forbidden_multi_word") and search_term:
        if len(search_term.split()) > 1:
            failures.append(
                f"searchTerm {search_term!r} has multiple words -- multi-word SEARCH_TERM is an "
                "OR/union, never an AND, per the resolved search semantics; use facets instead"
            )

    required_facets = constraints.get("required_facets")
    if required_facets:
        url = (output or {}).get("url", "")
        try:
            decoded = decode_qw0(url)
        except ValueError as e:
            failures.append(f"facets required but qw0 failed to decode: {e}")
            decoded = None
        if decoded is None and url:
            failures.append("facets required but response url has no qw0 param")
        elif decoded is not None:
            actual = facets_as_dict(decoded.get("selectedFacets"))
            match_mode = constraints.get("facet_match", "subset")
            expected = {f["columnName"]: sorted(f["values"]) for f in required_facets}
            if match_mode == "exact":
                if actual != expected:
                    failures.append(f"facets exact mismatch: expected {expected}, decoded {actual}")
            else:  # subset
                for col, vals in expected.items():
                    if col not in actual:
                        failures.append(f"facets missing expected column {col!r} (decoded {actual})")
                    elif set(vals) - set(actual[col]):
                        failures.append(
                            f"facets column {col!r} missing values {set(vals) - set(actual[col])}"
                        )

    return failures


def check_get_d4d_constraints(params: dict, constraints: dict) -> list:
    failures = []
    if "orgId" in constraints and params.get("orgId") != constraints["orgId"]:
        failures.append(f"orgId: expected {constraints['orgId']!r}, got {params.get('orgId')!r}")
    if "section" in constraints:
        expected_section = constraints["section"]
        actual_section = params.get("section") or None
        if expected_section is None and actual_section is not None:
            failures.append(f"expected the outline call (no section), got section={actual_section!r}")
        elif expected_section is not None and actual_section != expected_section:
            failures.append(f"section: expected {expected_section!r}, got {actual_section!r}")
    return failures


def check_search_d4d_constraints(params: dict, constraints: dict) -> list:
    failures = []
    query = (params.get("query") or "").lower()
    if "query_contains" in constraints and constraints["query_contains"].lower() not in query:
        failures.append(f"query {params.get('query')!r} doesn't contain {constraints['query_contains']!r}")
    if constraints.get("orgId_must_be_absent") and params.get("orgId"):
        failures.append(f"expected a cross-GC search (no orgId), got orgId={params.get('orgId')!r}")
    if "orgId" in constraints and constraints["orgId"] is not None:
        if params.get("orgId") != constraints["orgId"]:
            failures.append(f"orgId: expected {constraints['orgId']!r}, got {params.get('orgId')!r}")
    return failures


def check_get_columns_constraints(params: dict, constraints: dict) -> list:
    failures = []
    if "table" in constraints and params.get("table") != constraints["table"]:
        failures.append(f"table: expected {constraints['table']!r}, got {params.get('table')!r}")
    return failures


_CONSTRAINT_CHECKERS = {
    "sqlQuery": lambda params, output, constraints: check_sql_query_constraints(params, constraints),
    "buildPortalUrl": check_build_portal_url_constraints,
    "getD4D": lambda params, output, constraints: check_get_d4d_constraints(params, constraints),
    "searchD4D": lambda params, output, constraints: check_search_d4d_constraints(params, constraints),
    "getColumns": lambda params, output, constraints: check_get_columns_constraints(params, constraints),
    "countByType": lambda params, output, constraints: [],
    "listD4Ds": lambda params, output, constraints: [],
}


def _candidate_matches(expected_call: dict, actual_calls: list) -> list:
    """Actual calls with the same function name as `expected_call`."""
    return [c for c in actual_calls if c["function"] == expected_call["function"]]


# Per-function "identity" constraint keys: when several actual calls share a
# function name (e.g. two buildPortalUrl calls in one turn), these are the
# constraint keys that pin down WHICH call the expected entry is talking
# about, as opposed to keys that grade call *quality* once the right call is
# found. Used to break ties so a quality failure (e.g. a forbidden
# multi-word searchTerm) isn't masked by preferring a same-function call that
# merely happens to have fewer unrelated failures.
_IDENTITY_KEYS = {
    "sqlQuery": ("table",),
    "buildPortalUrl": ("resourceType", "id", "search_term_exact"),
    "getD4D": ("orgId", "section"),
    "searchD4D": ("orgId",),
    "getColumns": ("table",),
}


def _identity_matches(function: str, params: dict, constraints: dict) -> bool:
    """True if every identity key actually present in `constraints` agrees
    with `params` (constraints that omit a key are indifferent to it)."""
    for key in _IDENTITY_KEYS.get(function, ()):
        if key not in constraints:
            continue
        if key == "search_term_exact":
            if params.get("searchTerm") != constraints[key]:
                return False
        elif key == "section" and function == "getD4D":
            expected_section = constraints[key]
            actual_section = params.get("section") or None
            if expected_section != actual_section:
                return False
        elif params.get(key) != constraints[key]:
            return False
    return True


def grade_tool_calls(item: dict, actual_calls: list) -> dict:
    """Score an item's `expected_tool_calls` against the decoded actual calls.

    For each expected call, picks the best-matching actual call of the same
    function (fewest constraint failures) and records its failures. Returns:
        {
          "per_call": [{"function", "required", "matched": bool, "failures": [...]}, ...],
          "score": float in [0, 1],
          "any_required_missing": bool,
        }
    A required expected call with no actual call of that function at all is
    an automatic failure for that entry (`matched=False`).
    """
    per_call = []
    for expected_call in item["expected_tool_calls"]:
        function = expected_call["function"]
        required = expected_call.get("required", True)
        constraints = expected_call.get("constraints", {})
        checker = _CONSTRAINT_CHECKERS.get(function)

        candidates = _candidate_matches(expected_call, actual_calls)
        if not candidates:
            per_call.append({
                "function": function, "required": required,
                "matched": False, "failures": ["no actual call with this function name"],
            })
            continue

        # Rank candidates by (identity match first, then fewest failures) so
        # that among several same-function calls, the one the expected entry
        # actually identifies (by table/resourceType/id/orgId/section/exact
        # searchTerm) is graded -- rather than silently picking a different,
        # accidentally-lower-failure-count call and masking a real defect.
        scored_candidates = []
        for call in candidates:
            failures = checker(call["params"], call.get("output"), constraints) if checker else []
            identity_ok = _identity_matches(function, call["params"], constraints)
            scored_candidates.append((not identity_ok, len(failures), call, failures))
        scored_candidates.sort(key=lambda t: (t[0], t[1]))
        _, _, best_call, best_failures = scored_candidates[0]

        per_call.append({
            "function": function, "required": required,
            "matched": True, "failures": best_failures or [],
            "actual_params": best_call["params"] if best_call else None,
        })

    required_entries = [c for c in per_call if c["required"]]
    if required_entries:
        passed = sum(1 for c in required_entries if c["matched"] and not c["failures"])
        score = passed / len(required_entries)
    else:
        score = 1.0

    any_required_missing = any(
        c["required"] and (not c["matched"] or c["failures"]) for c in per_call
    )

    return {"per_call": per_call, "score": score, "any_required_missing": any_required_missing}


# ---------------------------------------------------------------------------
# Answer-fact grading
# ---------------------------------------------------------------------------

def _text_has_all(text: str, needles: list) -> bool:
    text_l = (text or "").lower()
    return all(str(n).lower() in text_l for n in needles)


def grade_answer_facts(item: dict, tool_calls: list, final_text: str) -> dict:
    """Deterministic-first answer-fact check.

    Returns {"verified": bool|None, "checks": [...]}. `verified=None` means
    no deterministic check applied and this item needs the LLM-judge
    fallback (only meaningful when item["llm_judge_fallback"] is True).
    """
    expected = item["expected_answer"]
    category = item["category"]
    checks = []

    def note(desc, ok):
        checks.append({"check": desc, "passed": ok})
        return ok

    if category == "keyword-lookup":
        ok = note(f"answer mentions id {expected['id']}", expected["id"] in (final_text or ""))
        ok2 = note(f"answer mentions name {expected['name']!r}", expected["name"] in (final_text or ""))
        return {"verified": ok and ok2, "checks": checks}

    if category in ("categorical-filter", "multi-filter-and", "multi-filter-or", "counts") and "count" in expected:
        n = expected["count"]
        ok = note(f"answer mentions count {n}", str(n) in (final_text or ""))
        return {"verified": ok, "checks": checks}

    if category == "counts" and "counts" in expected:
        all_ok = True
        for alias, n in expected["counts"].items():
            ok = note(f"answer mentions {alias}={n}", str(n) in (final_text or ""))
            all_ok = all_ok and ok
        return {"verified": all_ok, "checks": checks}

    if category == "redirect":
        # Prefer checking the actual decoded tool output over the chat text.
        for call in tool_calls:
            if call["function"] == "buildPortalUrl" and call.get("output", {}).get("url"):
                url = call["output"]["url"]
                if "url_path" in expected:
                    ok = note(f"redirect url matches {expected['url_path']!r}", url == expected["url_path"])
                    return {"verified": ok, "checks": checks}
                return {"verified": None, "checks": checks}
        return {"verified": None, "checks": checks}

    if category == "linked-resource":
        ids_fields = [k for k in expected if k.endswith("_ids") or k.endswith("_id")]
        if expected.get("expect_no_link_reported"):
            return {"verified": None, "checks": checks}  # needs judge: "no link" is a text claim
        if ids_fields:
            any_present = any(
                isinstance(expected[f], list) and any(i in (final_text or "") for i in expected[f])
                or isinstance(expected[f], str) and expected[f] in (final_text or "")
                for f in ids_fields
            )
            note("answer mentions at least one expected linked id", any_present)
            return {"verified": any_present, "checks": checks}
        return {"verified": None, "checks": checks}

    if category == "d4d":
        if "text_contains" in expected:
            return {"verified": None, "checks": checks}  # section narrative -> judge
        if "matching_org_ids" in expected:
            return {"verified": None, "checks": checks}  # cross-GC narrative -> judge
        if "section_ids" in expected:
            ok = note(
                "answer's outline mentions at least 3 of the real section headings",
                sum(1 for s in expected["section_ids"] if s.replace("-", " ") in (final_text or "").lower()) >= 3,
            )
            return {"verified": ok, "checks": checks}
        if "hit_count" in expected and expected["hit_count"] == 0:
            return {"verified": None, "checks": checks}  # "not covered" phrasing -> judge
        if "org_ids" in expected:
            ok = note("answer mentions all 4 GC org ids", _text_has_all(final_text, expected["org_ids"]))
            return {"verified": ok, "checks": checks}

    return {"verified": None, "checks": checks}


def judge_answer(bedrock_client, judge_model_id: str, item: dict, final_text: str) -> int:
    """LLM-judge fallback for free-text facts. Returns 2/1/0, or -1 on failure."""
    prompt = (
        "You are grading an AI assistant's answer for the Bridge2AI Standards Explorer Copilot, "
        "a resource-search benchmark for good-faith questions.\n\n"
        f"User question: {item['question']}\n\n"
        f"Ground truth (verified live against Synapse): {json.dumps(item['expected_answer'])}\n\n"
        f"Grading notes: {item['notes']}\n\n"
        f"Assistant's answer: {final_text}\n\n"
        "Score whether the answer is factually consistent with the ground truth above:\n"
        "  2 — fully consistent with the ground truth, no fabricated or contradicted facts\n"
        "  1 — partially consistent; missing detail or partially incorrect\n"
        "  0 — contradicts the ground truth, or fabricates facts not in it\n\n"
        "Reply with a single digit (0, 1, or 2) and nothing else."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        resp = bedrock_client.invoke_model(modelId=judge_model_id, body=body)
        digit = json.loads(resp["body"].read())["content"][0]["text"].strip()
        return int(digit) if digit in ("0", "1", "2") else -1
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def invoke_agent(agent_client, agent_id: str, agent_alias_id: str, question: str, session_id: str):
    """Invoke the agent and return (final_text, tool_calls, raw_trace_events)."""
    response = agent_client.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=question,
        enableTrace=True,
    )

    completion = ""
    raw_traces = []
    for event in response["completion"]:
        if "chunk" in event:
            completion += event["chunk"]["bytes"].decode("utf-8")
        if "trace" in event:
            raw_traces.append(event["trace"].get("trace", {}))

    tool_calls = extract_tool_calls(raw_traces)
    return completion.strip(), tool_calls, raw_traces


# ---------------------------------------------------------------------------
# Per-item evaluation
# ---------------------------------------------------------------------------

def evaluate_item(
    agent_client, bedrock_client, agent_id, agent_alias_id, judge_model_id,
    item: dict, use_judge: bool,
) -> dict:
    session_id = str(uuid.uuid4())
    final_text, tool_calls, _raw = invoke_agent(
        agent_client, agent_id, agent_alias_id, item["question"], session_id
    )

    tool_result = grade_tool_calls(item, tool_calls)
    fact_result = grade_answer_facts(item, tool_calls, final_text)

    judge_score = None
    if fact_result["verified"] is None and item.get("llm_judge_fallback") and use_judge:
        judge_score = judge_answer(bedrock_client, judge_model_id, item, final_text)

    if fact_result["verified"] is not None:
        answer_correct = fact_result["verified"]
    elif judge_score is not None:
        answer_correct = judge_score == 2
    else:
        answer_correct = None  # unscored

    return {
        "item_id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "tool_call_score": tool_result["score"],
        "tool_call_details": tool_result["per_call"],
        "answer_fact_checks": fact_result["checks"],
        "answer_verified_deterministically": fact_result["verified"],
        "judge_score": judge_score,
        "answer_correct": answer_correct,
        "agent_response": final_text,
    }


# ---------------------------------------------------------------------------
# Metrics reporting
# ---------------------------------------------------------------------------

def print_metrics(results: list) -> None:
    import pandas as pd

    df = pd.DataFrame(results)
    n = len(df)
    print(f"\n{'='*60}")
    print(f"RESOURCE SEARCH CORRECTNESS RESULTS  ({n} items)")
    print(f"{'='*60}")

    print(f"\nMean tool-call shape score: {df['tool_call_score'].mean():.1%}")
    print("Tool-call shape score by category:")
    print(
        df.groupby("category")["tool_call_score"].agg(mean="mean", n="count")
        .sort_values("mean", ascending=False).to_string()
    )

    scored = df[df["answer_correct"].notna()]
    unscored = n - len(scored)
    if len(scored):
        print(f"\nAnswer correctness (of {len(scored)} scorable items): {scored['answer_correct'].mean():.1%}")
    if unscored:
        print(f"Unscored (no deterministic check, judge unavailable/disabled): {unscored}")

    print(f"\n{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_evaluation(args: argparse.Namespace) -> None:
    import boto3

    if args.agent_id == PROD_AGENT_ID and not args.allow_prod:
        print(
            f"ERROR: refusing to evaluate against the prod agent id ({PROD_AGENT_ID}) "
            "without --allow-prod. Use the dev agent/alias instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.agent_id == PROD_AGENT_ID:
        print(f"WARNING: running the resource-search eval against PROD agent {PROD_AGENT_ID}.")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    agent_client = session.client("bedrock-agent-runtime")
    bedrock_client = session.client("bedrock-runtime")

    sts = session.client("sts")
    identity = sts.get_caller_identity()
    print(f"Authenticated as: {identity['Arn']}")
    print(f"Region: {args.region}")

    dataset_path = Path(args.dataset)
    with open(dataset_path) as f:
        dataset = json.load(f)

    live_pins = dict(lf.TABLES)
    if dataset.get("pinned_versions") != live_pins:
        print(
            "WARNING: dataset pinned_versions differ from the Lambda's current TABLES -- "
            "ground truth may be stale. Re-run build_ground_truth.py.\n"
            f"  dataset: {dataset.get('pinned_versions')}\n  live:    {live_pins}"
        )

    items = dataset["items"]
    if args.item is not None:
        items = [it for it in items if it["id"] == args.item]
        if not items:
            print(f"ERROR: no item with id {args.item!r}", file=sys.stderr)
            sys.exit(1)
    elif args.n is not None:
        items = items[: args.n]

    print(f"Loaded {len(items)} items from {dataset_path.name}")

    results = []
    errors = []
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] [{item['category']}] {item['question'][:70]}", flush=True)
        try:
            result = evaluate_item(
                agent_client, bedrock_client, args.agent_id, args.alias_id,
                args.judge_model, item, use_judge=not args.no_judge,
            )
            results.append(result)
            print(
                f"    tool_call_score={result['tool_call_score']:.2f}  "
                f"answer_correct={result['answer_correct']}"
            )
        except Exception as e:
            errors.append({"item_id": item["id"], "error": str(e)})
            print(f"    ERROR: {e}")

    print(f"\nCompleted: {len(results)} items scored, {len(errors)} errors")

    output_path = Path(args.output)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dated_path = output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")

    payload = {
        "timestamp": timestamp,
        "config": {
            "agent_id": args.agent_id, "agent_alias_id": args.alias_id,
            "judge_model": args.judge_model, "dataset": str(dataset_path),
            "no_judge": args.no_judge,
        },
        "dataset_pinned_versions": dataset.get("pinned_versions"),
        "results": results,
        "errors": errors,
    }
    dated_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dated_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results saved to {dated_path}")

    if results:
        print_metrics(results)
    if errors:
        sys.exit(1)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate resource-search correctness for the Bridge2AI Standards Explorer Copilot.",
    )
    parser.add_argument("--agent-id", required=True,
                         help="Bedrock Agent ID (no default -- no B2AI agent has been deployed yet)")
    parser.add_argument("--alias-id", default="TSTALIASID", help="Bedrock Agent alias ID (default: %(default)s)")
    parser.add_argument("--profile", default=None, help="AWS profile name (default: env credentials)")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: %(default)s)")
    parser.add_argument("--dataset", default="resource_search_dataset.json",
                         help="Path to the dataset JSON (default: %(default)s)")
    parser.add_argument("--output", default="resource_search_eval_results.json",
                         help="Base output path; a UTC datestamp is appended (default: %(default)s)")
    parser.add_argument("--judge-model", default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                         help="Bedrock model ID for the LLM-judge fallback (default: %(default)s)")
    parser.add_argument("-n", type=int, default=None, help="Only run the first N items (quick test runs)")
    parser.add_argument("--item", default=None, metavar="ITEM_ID",
                         help="Run a single item by its id (e.g. rs-keyword-fhir); overrides -n")
    parser.add_argument("--no-judge", action="store_true",
                         help="Disable the LLM-judge fallback (items needing it are left unscored)")
    parser.add_argument("--allow-prod", action="store_true",
                         help=f"Allow running against the prod agent id ({PROD_AGENT_ID})")
    return parser.parse_args(argv)


if __name__ == "__main__":
    run_evaluation(parse_args())
