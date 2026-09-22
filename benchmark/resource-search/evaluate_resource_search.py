#!/usr/bin/env python3
"""Evaluate resource-backend search/retrieval CORRECTNESS for the CCKP Copilot.

Unlike benchmark/kb-routing (which only checks *which* knowledge source was
consulted), this script decodes the actual tool-call parameters the agent
sent to the SQL resource-backend action group (sqlQuery, buildExploreUrl,
getDatasetFiles, getFileDetails, checkRestriction) from the Bedrock trace,
and grades them against an item's expected_shape / known_facts / gold_query —
i.e. whether the agent asked the right question of the backend and reported
the right answer, not just whether it used the backend at all.

Usage:
    python evaluate_resource_search.py --agent-id ABC123
    python evaluate_resource_search.py --agent-id ABC123 --judge   # also LLM-judge claim-level checks
    python evaluate_resource_search.py --agent-id ABC123 -n 5      # quick test: first 5 items
    python evaluate_resource_search.py --agent-id ABC123 --item do-restricted-download-claim
    python evaluate_resource_search.py --agent-id ABC123 --no-live-gold  # skip live Synapse gold queries

No CCKP agent has been deployed yet, so --agent-id has no default.

The default alias TSTALIASID always points to the DRAFT version. If you've
updated the agent (instructions, model, action groups) without preparing it,
run `aws bedrock-agent prepare-agent --agent-id <ID>` first — otherwise the
eval will test the previous prepared version, not your latest changes.

Grading, per category — see resource_search_schema.json / README.md for the
full expected_shape conventions:
    keyword-search / redirect (simple):  decoded tool-call text must contain
                                          at least one of expected_shape's
                                          must_include_any terms
    multi-filter / redirect (AND/OR):    decoded facets/searchExpressions
                                          must satisfy expected_shape's
                                          facets (AND, all required) and
                                          or_group (>=1 of the listed values)
    linked-resources:                    decoded sql must reference an
                                          acceptable join direction; a
                                          gold_query confirms expect_match
    dataset-operations:                  decoded id must match known_facts;
                                          a live restrictionInformation check
                                          confirms the ground truth is still
                                          current
    judge_check items (claim-level):     an LLM judge answers the item's
                                          specific yes/no correctness
                                          question against the agent's full
                                          response text (only run with
                                          --judge; these items' automatic
                                          score is advisory without it)

Live gold queries run directly against Synapse's public View-table REST API
(the same async table-query endpoint agents/cckp-copilot/lambda/cckpSqlRag's
sql_query() itself calls) — no Synapse credentials needed for CCKP's public
Dataset/Publication tables specifically. A future retarget whose backend
tables are NOT publicly queryable will need to add auth here; see README.
"""

import argparse
import base64
import gzip
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # falls back to the system default; some macOS
    # python.org installs lack a working local CA bundle, in which case
    # `pip install certifi` fixes live-gold queries without touching AWS.

SYNAPSE_BASE_URL = "https://repo-prod.prod.sagebase.org"
KNOWN_TABLES = {
    "datasets": "syn21897968",
    "publications": "syn21868591",
    "tools": "syn26127427",
    "grants": "syn21918972",
    "education": "syn51497305",
}


# ---------------------------------------------------------------------------
# Live Synapse queries (public View tables — no auth needed for CCKP)
# ---------------------------------------------------------------------------

def _http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"errorMessage": f"HTTP {e.code}"}


def run_synapse_query(sql: str, timeout_s: float = 20.0) -> dict:
    """Run a SQL query against a public Synapse table, same mechanism the
    SQL Lambda's own sql_query() uses. Returns {"headers": [...], "rows":
    [...]}, or {"error": "..."}. No credentials — relies on CCKP's View
    tables being public-readable, which is true today but is a real
    assumption; a retarget with private backend tables needs to add auth
    here (see README's "Live gold queries" section)."""
    table_match = re.search(r"FROM\s+(syn\d+)", sql, re.IGNORECASE)
    if not table_match:
        return {"error": f"could not find a synId table reference in: {sql!r}"}
    table_id = table_match.group(1)

    start = _http_json(
        "POST",
        f"{SYNAPSE_BASE_URL}/repo/v1/entity/{table_id}/table/query/async/start",
        {
            "concreteType": "org.sagebionetworks.repo.model.table.QueryBundleRequest",
            "query": {"sql": sql},
            "partMask": 0x1,
        },
    )
    token = start.get("token")
    if not token:
        return {"error": f"failed to start query: {start}"}

    deadline = time.monotonic() + timeout_s
    get_url = f"{SYNAPSE_BASE_URL}/repo/v1/entity/{table_id}/table/query/async/get/{token}"
    while time.monotonic() < deadline:
        time.sleep(1.5)
        result = _http_json("GET", get_url)
        if "queryResult" in result:
            rs = result["queryResult"]["queryResults"]
            headers = [h["name"] for h in rs["headers"]]
            rows = [row["values"] for row in rs["rows"]]
            return {"headers": headers, "rows": rows}
        if "reason" in result or "errorMessage" in result:
            return {"error": result.get("reason", result.get("errorMessage"))}
    return {"error": "timed out waiting for query result"}


def run_restriction_check(entity_id: str) -> dict:
    """Live restriction status for one entity, same public endpoint
    checkRestriction() itself calls. Returns {"restriction": "open"|
    "restricted", "canDownload": bool} or {"error": "..."}."""
    resp = _http_json(
        "POST",
        f"{SYNAPSE_BASE_URL}/repo/v1/restrictionInformation/batch",
        {"restrictableObjectType": "ENTITY", "objectIds": [entity_id]},
    )
    info_list = resp.get("restrictionInformation")
    if not info_list:
        return {"error": f"no restriction info returned: {resp}"}
    info = info_list[0]
    restriction = "restricted" if info.get("hasUnmetAccessRequirement") else "open"
    return {
        "restriction": restriction,
        "canDownload": info.get("userEntityPermissions", {}).get("canDownload"),
    }


# ---------------------------------------------------------------------------
# Bedrock trace parsing — decode actual tool-call parameters, not just
# "was an action group invoked"
# ---------------------------------------------------------------------------

def _coerce_value(raw):
    """A requestBody property's value often arrives as a JSON-encoded
    string for array/object-typed parameters. Try to parse it; fall back to
    the raw string for plain scalars."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _extract_params(action_group_invocation_input: dict) -> dict:
    """Flatten a Bedrock actionGroupInvocationInput's requestBody properties
    into a plain {name: value} dict, decoding JSON-string values where
    present. Returns {} if there's no requestBody (e.g. a no-arg call)."""
    params = {}
    request_body = action_group_invocation_input.get("requestBody", {})
    for media_type, content in request_body.get("content", {}).items():
        for prop in content.get("properties", []):
            name = prop.get("name")
            if name:
                params[name] = _coerce_value(prop.get("value"))
    return params


def invoke_agent(
    agent_client,
    agent_id: str,
    agent_alias_id: str,
    question: str,
    session_id: str,
) -> tuple[str, list[dict]]:
    """Invoke the agent and return (response_text, tool_calls).

    tool_calls is an ordered list of {"function": str, "params": dict,
    "response_text": str | None} — one entry per action-group invocation
    detected in the trace, paired with its (best-effort) matching
    observation output.
    """
    response = agent_client.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=question,
        enableTrace=True,
    )

    completion = ""
    tool_calls: list[dict] = []

    for event in response["completion"]:
        if "chunk" in event:
            completion += event["chunk"]["bytes"].decode("utf-8")

        if "trace" in event:
            orch = event["trace"].get("trace", {}).get("orchestrationTrace", {})

            inv_input = orch.get("invocationInput", {})
            if inv_input.get("invocationType") == "ACTION_GROUP":
                agi = inv_input.get("actionGroupInvocationInput", {})
                function = agi.get("function") or agi.get("apiPath", "").strip("/").split("/")[-1]
                tool_calls.append({
                    "function": function,
                    "api_path": agi.get("apiPath"),
                    "params": _extract_params(agi),
                    "response_text": None,
                })

            obs = orch.get("observation", {})
            if obs.get("type") == "ACTION_GROUP":
                text = obs.get("actionGroupInvocationOutput", {}).get("text")
                # Pair with the most recent call still missing a response —
                # orchestration traces are a single ordered sequence per
                # turn (no concurrent tool calls in this agent shape), so
                # sequential pairing is reliable here.
                for call in reversed(tool_calls):
                    if call["response_text"] is None:
                        call["response_text"] = text
                        break

    return completion.strip(), tool_calls


def decode_explore_url(url: str) -> dict | None:
    """Decode a buildExploreUrl response's qw0 param back to the query JSON
    it encodes (base64 -> gzip -> JSON) — the same self-verification
    build_explore_url() itself does before returning. Used here as a
    cross-check on top of the invocation-input params, in case a future
    version of the tool derives the final filter shape internally rather
    than taking it verbatim from the input."""
    match = re.search(r"[?&]qw0=([^&]+)", url or "")
    if not match:
        return None
    try:
        import urllib.parse
        raw = urllib.parse.unquote(match.group(1))
        return json.loads(gzip.decompress(base64.b64decode(raw)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shape scoring — decoded params vs. expected_shape
# ---------------------------------------------------------------------------

def _searchable_text(params: dict) -> str:
    """Everything in a tool call's params that free-text terms might live
    in (sql string, searchExpressions entries), lowercased for matching."""
    parts = []
    if isinstance(params.get("sql"), str):
        parts.append(params["sql"])
    se = params.get("searchExpressions")
    if isinstance(se, list):
        parts.extend(str(x) for x in se)
    elif isinstance(se, str):
        parts.append(se)
    return " ".join(parts).lower()


def _facet_values(params: dict, column_name: str) -> set[str]:
    """Values selected for one facet column across a call's `facets` param
    (list of {columnName, values}) — case-insensitive."""
    values: set[str] = set()
    for facet in params.get("facets") or []:
        if isinstance(facet, dict) and str(facet.get("columnName", "")).lower() == column_name.lower():
            for v in facet.get("values") or []:
                values.add(str(v).lower())
    return values


def score_shape(item: dict, tool_calls: list[dict]) -> dict:
    """Score the decoded tool-call shape against item['expected_shape'].

    Returns {"shape_score": 0|1|2, "shape_notes": str, "matched_call": dict|None}
        2 — fully correct shape
        1 — right tool used, partially-correct shape (e.g. missing an
            AND-required facet, or free-text term not found)
        0 — wrong/no tool called, or shape contradicts expected intent
    """
    expected_tool = item["expected_tool"]
    shape = item["expected_shape"]

    candidates = [
        c for c in tool_calls
        if expected_tool == "any"
        and c["function"] in ("sqlQuery", "buildExploreUrl")
        or c["function"] == expected_tool
    ]
    if not candidates:
        called = sorted({c["function"] for c in tool_calls}) or ["(none)"]
        return {"shape_score": 0, "shape_notes": f"expected {expected_tool!r}, got {called}", "matched_call": None}

    call = candidates[-1]  # last matching call — most likely the "final" query after any refinement
    params = call["params"]
    notes = []
    score = 2

    if "must_include_any" in shape:
        text = _searchable_text(params)
        if not any(term.lower() in text for term in shape["must_include_any"]):
            score = 0
            notes.append(f"none of {shape['must_include_any']} found in call params")

    if "facets" in shape:
        for facet in shape["facets"]:
            expected_values = {str(v).lower() for v in facet["values"]}
            actual_values = _facet_values(params, facet["columnName"])
            if not (expected_values & actual_values):
                # tolerate an equivalent AND expressed as a sql WHERE clause
                text = _searchable_text(params)
                if not any(v in text for v in expected_values):
                    score = min(score, 1 if score else 0)
                    notes.append(f"required facet {facet['columnName']}={facet['values']} not found")

    if "or_group" in shape:
        og = shape["or_group"]
        expected_values = {str(v).lower() for v in og["values"]}
        actual_values = _facet_values(params, og["columnName"])
        text = _searchable_text(params)
        hit = bool(expected_values & actual_values) or any(v in text for v in expected_values)
        if not hit:
            score = 0
            notes.append(f"none of or_group {og['columnName']}={og['values']} found")

    if "join" in shape:
        text = _searchable_text(params)
        directions_hit = []
        for direction in shape["join"]["acceptable_directions"]:
            cols = re.findall(r"\.(\w+)", direction)
            if any(c.lower() in text for c in cols):
                directions_hit.append(direction)
        if not directions_hit:
            score = 0
            notes.append(f"no acceptable join direction referenced in sql: {shape['join']['acceptable_directions']}")

    if "id" in shape:
        if str(params.get("id", "")).strip() != shape["id"]:
            score = 0
            notes.append(f"expected id={shape['id']!r}, got id={params.get('id')!r}")

    if shape.get("known_trap") and call["function"] == "buildExploreUrl":
        se = params.get("searchExpressions")
        if isinstance(se, list) and len(se) >= 2:
            notes.append("FLAG: two+ searchExpressions entries on a known non-facetable AND item — this unions, doesn't intersect; see judge_check for the actual correctness call")

    return {
        "shape_score": score,
        "shape_notes": "; ".join(notes) if notes else "ok",
        "matched_call": {"function": call["function"], "params": call["params"]},
    }


# ---------------------------------------------------------------------------
# Live gold-answer comparison
# ---------------------------------------------------------------------------

def score_gold(item: dict, tool_calls: list[dict], skip_live: bool) -> dict:
    """Where item has a gold_query, run it live and report it alongside
    whatever the agent's own sqlQuery call(s) returned — informational, not
    pass/fail on its own (portal data drifts), but useful for spotting an
    agent answer that's wildly off from the live ground truth."""
    if "gold_query" not in item or skip_live:
        return {"gold_result": None}
    result = run_synapse_query(item["gold_query"])
    return {"gold_result": result}


def score_known_facts(item: dict, skip_live: bool) -> dict:
    """For dataset-operations items, re-check known_facts live so a stale
    fixture (portal content can change) is visible in results rather than
    silently assumed correct."""
    if "known_facts" not in item or skip_live:
        return {"known_facts_live": None}
    live = run_restriction_check(item["known_facts"]["id"])
    matches = live.get("restriction") == item["known_facts"].get("restriction")
    return {"known_facts_live": live, "known_facts_still_valid": matches}


# ---------------------------------------------------------------------------
# LLM judge for claim-level (judge_check) items
# ---------------------------------------------------------------------------

def judge_claim(bedrock_client, judge_model_id: str, question: str, agent_response: str, judge_check: str) -> int:
    """Answer a specific yes/no correctness question about the agent's
    response text. Returns 1 (correct/no-failure), 0 (failure), or -1
    (judge couldn't determine)."""
    prompt = (
        "You are evaluating a single specific correctness claim about an AI assistant's response "
        "for the Cancer Complexity Knowledge Portal (CCKP).\n\n"
        f"User question: {question}\n\n"
        f"Assistant's response: {agent_response}\n\n"
        f"Correctness check: {judge_check}\n\n"
        "Answer whether the response has the FAILURE described in the correctness check:\n"
        "  0 — yes, the response has this failure (incorrect)\n"
        "  1 — no, the response does not have this failure (correct)\n\n"
        "Reply with a single digit (0 or 1) and nothing else."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp = bedrock_client.invoke_model(modelId=judge_model_id, body=body)
    digit = json.loads(resp["body"].read())["content"][0]["text"].strip()
    return int(digit) if digit in ("0", "1") else -1


# ---------------------------------------------------------------------------
# Per-item evaluation
# ---------------------------------------------------------------------------

def evaluate_item(
    agent_client,
    bedrock_client,
    agent_id: str,
    agent_alias_id: str,
    judge_model_id: str,
    item: dict,
    run_judge: bool,
    skip_live: bool,
) -> dict:
    session_id = str(uuid.uuid4())
    agent_response, tool_calls = invoke_agent(
        agent_client, agent_id, agent_alias_id, item["question"], session_id
    )

    shape_result = score_shape(item, tool_calls)
    gold_result = score_gold(item, tool_calls, skip_live)
    facts_result = score_known_facts(item, skip_live)

    judge_score = None
    if "judge_check" in item:
        judge_score = judge_claim(bedrock_client, judge_model_id, item["question"], agent_response, item["judge_check"]) if run_judge else -1

    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected_tool": item["expected_tool"],
        "tool_calls": [{"function": c["function"], "params": c["params"]} for c in tool_calls],
        "shape_score": shape_result["shape_score"],
        "shape_notes": shape_result["shape_notes"],
        **gold_result,
        **facts_result,
        "judge_score": judge_score,
        "agent_response": agent_response,
    }


# ---------------------------------------------------------------------------
# Metrics reporting
# ---------------------------------------------------------------------------

def print_metrics(results: list[dict]) -> None:
    df = pd.DataFrame(results)
    n = len(df)
    print(f"\n{'='*60}")
    print(f"RESOURCE SEARCH CORRECTNESS RESULTS  ({n} items)")
    print(f"{'='*60}")

    print("\nShape correctness (0=wrong, 1=partial, 2=fully correct):")
    print(f"  Fully correct (2): {(df['shape_score'] == 2).mean():.1%}  ({(df['shape_score']==2).sum()}/{n})")
    print(f"  Partial (1):       {(df['shape_score'] == 1).mean():.1%}  ({(df['shape_score']==1).sum()}/{n})")
    print(f"  Wrong (0):         {(df['shape_score'] == 0).mean():.1%}  ({(df['shape_score']==0).sum()}/{n})")

    print("\nBy category:")
    print(f"  {'Category':<20}  {'Fully correct':>13}  {'n':>4}")
    for cat in df["category"].unique():
        sub = df[df["category"] == cat]
        print(f"  {cat:<20}  {(sub['shape_score']==2).mean():>12.1%}   {len(sub):>4}")

    facts_df = df[df["known_facts_still_valid"].notna()]
    if len(facts_df) > 0:
        stale = (~facts_df["known_facts_still_valid"]).sum()
        if stale:
            print(f"\nWARNING: {stale} dataset-operations item(s) have known_facts that no longer match live Synapse data — fix the fixture before trusting those results.")

    judged = df[df["judge_score"].notna() & (df["judge_score"] >= 0)]
    if len(judged) > 0:
        print(f"\nJudge-assisted claim checks: {(judged['judge_score']==1).mean():.1%} correct  ({(judged['judge_score']==1).sum()}/{len(judged)})")
        for _, row in judged[judged["judge_score"] == 0].iterrows():
            print(f"  FAILED: {row['id']}")

    print(f"\n{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_evaluation(args: argparse.Namespace) -> None:
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

    if args.item is not None:
        dataset = [i for i in dataset if i["id"] == args.item]
        if not dataset:
            print(f"ERROR: no item with id {args.item!r}", file=sys.stderr)
            sys.exit(1)
    elif args.n is not None:
        dataset = dataset[:args.n]
    print(f"Loaded {len(dataset)} items from {dataset_path.name}")

    results: list[dict] = []
    errors: list[dict] = []

    for ii, item in enumerate(dataset, 1):
        print(f"\n[{ii}/{len(dataset)}] [{item['category']}] {item['question'][:70]}", flush=True)
        try:
            result = evaluate_item(
                agent_client, bedrock_client, args.agent_id, args.alias_id,
                args.judge_model, item, run_judge=args.judge, skip_live=args.no_live_gold,
            )
            results.append(result)
            shape_status = {2: "OK", 1: "PARTIAL", 0: "WRONG"}[result["shape_score"]]
            line = f"    Shape: {shape_status}  ({result['shape_notes']})"
            if result["judge_score"] is not None and result["judge_score"] >= 0:
                line += f"  |  Judge: {'PASS' if result['judge_score'] == 1 else 'FAIL'}"
            print(line)
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
            "agent_id": args.agent_id,
            "agent_alias_id": args.alias_id,
            "judge_model": args.judge_model,
            "dataset": str(dataset_path),
            "judge_enabled": args.judge,
            "live_gold_enabled": not args.no_live_gold,
        },
        "results": results,
        "errors": errors,
    }
    dated_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dated_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results saved to {dated_path}")

    if results:
        print_metrics(results)
    else:
        print("No results to report.")

    if errors:
        sys.exit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate resource-backend search/retrieval correctness for the CCKP Copilot.",
    )
    parser.add_argument("--agent-id", required=True, help="Bedrock Agent ID (no default — no CCKP agent has been deployed yet)")
    parser.add_argument("--alias-id", default="TSTALIASID", help="Bedrock Agent alias ID (default: %(default)s)")
    parser.add_argument("--profile", default=None, help="AWS profile name (default: env credentials)")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: %(default)s)")
    parser.add_argument("--dataset", default="resource_search_dataset.json", help="Path to the dataset JSON (default: %(default)s)")
    parser.add_argument("--output", default="resource_search_eval_results.json", help="Base output path; a UTC datestamp is appended (default: %(default)s)")
    parser.add_argument("--judge-model", default="us.anthropic.claude-haiku-4-5-20251001-v1:0", help="Bedrock model ID for the LLM judge (default: %(default)s)")
    parser.add_argument("-n", type=int, default=None, help="Only run the first N items (for quick test runs)")
    parser.add_argument("--item", default=None, metavar="ITEM_ID", help="Run a single item by its id; overrides -n")
    parser.add_argument("--judge", action="store_true", help="Enable LLM judge scoring for judge_check items (off by default)")
    parser.add_argument("--no-live-gold", action="store_true", help="Skip live Synapse gold_query/known_facts re-checks (faster, offline-safe)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    run_evaluation(parse_args())
