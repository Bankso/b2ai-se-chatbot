#!/usr/bin/env python3
"""Check whether the live portal now shows what `d4d_data.json` bundles.

`scripts/build_d4d_data.py` writes
`agents/b2ai-copilot/lambda/b2aiSqlRag/d4d_data.json` by resolving each
project's *canonical* run straight from `bridge2ai/data-sheets-schema` and
running its own renderer -- there is no portal HTML for these runs yet. The
live b2ai.standards portal's `D4D_content` Synapse table
(`source.synapseTable`) still serves the *older* generation this Lambda
used to bundle, and will be updated separately once that pipeline catches
up. This script's job is narrow and literal: for each bundled org, does the
live row's `<h1>` and set of field labels match what `d4d_data.json` says
they should be? It also reports whether the Lambda's own `TABLES["d4d"]`
pin (read as plain text, not imported -- see below) still points at
`source.synapseTable`.

This intentionally does *not* import `agents/b2ai-copilot/lambda/b2aiSqlRag/
lambda_function.py` -- another agent is mid-edit on that file, so importing
and executing it here could fail (or succeed) on an unrelated in-flight bug
that has nothing to do with what this script checks. The `TABLES["d4d"]`
value is instead read with a regex over the file's raw text, and the live
table is queried directly (same anonymous Synapse table-query REST API the
Lambda itself uses, reimplemented here as a few lines rather than imported).

Usage:
    python3 scripts/check_d4d_pin.py

Exit code 0: the Lambda's `TABLES["d4d"]` pin matches `d4d_data.json`'s
    `source.synapseTable`, and every bundled org's live `content_text` has
    the same `<h1>` and the same set of field labels as `d4d_data.json`.
Exit code 1: at least one mismatch (table pin drift, a missing live row,
    a title mismatch, or a label-set difference) -- the printed diff says
    which. Expected right now: the live table still serves an older
    generation than what's bundled.
Exit code 2: the live table couldn't be queried at all (network/Synapse
    issue) -- a "couldn't check" outcome, not a confirmed drift.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_d4d_data import _D4DHtmlParser  # noqa: E402  (reuse the same HTML parsing)

LAMBDA_DIR = (
    Path(__file__).resolve().parent.parent
    / "agents" / "b2ai-copilot" / "lambda" / "b2aiSqlRag"
)
LAMBDA_FUNCTION_PATH = LAMBDA_DIR / "lambda_function.py"
D4D_DATA_PATH = LAMBDA_DIR / "d4d_data.json"

SYNAPSE_BASE_URL = "https://repo-prod.prod.sagebase.org"
PART_RESULTS = 0x1
QUERY_TIMEOUT = 30

_TABLES_D4D_RE = re.compile(r"""["']d4d["']\s*:\s*["']([^"']+)["']""")


def _bare_id(syn_id: str) -> str:
    return syn_id.split(".", 1)[0]


def _read_lambda_d4d_pin(path: Path) -> Optional[str]:
    """Regex-extract TABLES["d4d"]'s value from the Lambda's raw source,
    without importing (and therefore executing) the module -- see module
    docstring for why."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = _TABLES_D4D_RE.search(text)
    return m.group(1) if m else None


def _request(method: str, url: str, body: Optional[dict] = None) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", errors="replace") or "{}")


def _run_query(syn_id: str, sql: str, limit: int, part_mask: int) -> dict:
    """Minimal, self-contained port of the Lambda's own `_run_query` --
    start an async table query, poll, return the raw bundle."""
    start_url = f"{SYNAPSE_BASE_URL}/repo/v1/entity/{syn_id}/table/query/async/start"
    body = {
        "concreteType": "org.sagebionetworks.repo.model.table.QueryBundleRequest",
        "entityId": syn_id,
        "query": {"sql": sql, "limit": limit},
        "partMask": part_mask,
    }
    status, resp = _request("POST", start_url, body)
    if status not in (200, 201) or "token" not in resp:
        raise Exception(f"query start failed (HTTP {status}): {resp}")
    token = resp["token"]

    get_url = f"{SYNAPSE_BASE_URL}/repo/v1/entity/{syn_id}/table/query/async/get/{token}"
    deadline = time.monotonic() + QUERY_TIMEOUT
    while True:
        status, resp = _request("GET", get_url)
        if status in (200, 201):
            return resp
        if status == 202:
            if time.monotonic() >= deadline:
                raise TimeoutError("Synapse query is still running")
            time.sleep(1.0)
            continue
        raise Exception(f"query failed (HTTP {status}): {resp}")


def _parse_bundle(bundle: dict) -> list[dict]:
    qr = ((bundle or {}).get("queryResult") or {}).get("queryResults") or {}
    headers = [h.get("name") for h in qr.get("headers", [])]
    return [dict(zip(headers, row.get("values", []))) for row in qr.get("rows", [])]


def main() -> int:
    if not D4D_DATA_PATH.exists():
        print(
            f"ERROR: {D4D_DATA_PATH} does not exist -- run "
            f"scripts/build_d4d_data.py first",
            file=sys.stderr,
        )
        return 2

    with open(D4D_DATA_PATH, "r", encoding="utf-8") as f:
        d4d_data = json.load(f)

    source = d4d_data.get("source", {})
    pinned_synapse_table = source.get("synapseTable")
    docs: Dict[str, Any] = d4d_data.get("docs", {})
    fields: Dict[str, Any] = d4d_data.get("fields", {})

    drift = []

    # --- 1. Lambda's TABLES["d4d"] pin vs. d4d_data.json's source.synapseTable ---
    lambda_pin = _read_lambda_d4d_pin(LAMBDA_FUNCTION_PATH)
    if lambda_pin is None:
        print(
            f"WARNING: couldn't find TABLES['d4d'] in {LAMBDA_FUNCTION_PATH} "
            f"(file missing, or another agent's in-flight edit doesn't match "
            f"the expected 'd4d': '<synId>' shape) -- skipping this sub-check",
            file=sys.stderr,
        )
    elif lambda_pin != pinned_synapse_table:
        drift.append(
            f"  TABLES['d4d'] in lambda_function.py is {lambda_pin!r}, but "
            f"d4d_data.json's source.synapseTable is {pinned_synapse_table!r}"
        )
    else:
        print(f"[OK] Lambda TABLES['d4d'] pin matches d4d_data.json: {lambda_pin!r}")

    # --- 2. Query the live D4D_content table ---
    try:
        bare_id = _bare_id(pinned_synapse_table)
        bundle = _run_query(
            bare_id,
            f"SELECT content_id, content_text FROM {bare_id}",
            10,
            PART_RESULTS,
        )
        live_rows = _parse_bundle(bundle)
    except Exception as e:
        print(f"ERROR: failed to query {pinned_synapse_table}: {e}", file=sys.stderr)
        return 2

    live_by_org = {
        row["content_id"]: row.get("content_text")
        for row in live_rows
        if row.get("content_id")
    }

    # --- 3. Compare each bundled org's live doc against d4d_data.json ---
    checked = 0
    for org_id, doc in docs.items():
        gc = doc.get("gc", org_id)
        live_html = live_by_org.get(org_id)
        if live_html is None:
            drift.append(f"  {org_id} ({gc}): no live D4D_content row found")
            continue

        parsed = _D4DHtmlParser()
        parsed.feed(live_html)
        checked += 1

        bundled_title = doc.get("portalTitle", doc.get("title"))
        if parsed.h1 != bundled_title:
            drift.append(
                f"  {org_id} ({gc}): title mismatch -- live <h1> is "
                f"{parsed.h1!r}, d4d_data.json has {bundled_title!r}"
            )

        live_label_set = {text for text, _section in parsed.labels}
        bundled_label_set = {
            fields[key]["label"]
            for key in doc.get("data", {}).keys()
            if key in fields
        }
        missing_live = bundled_label_set - live_label_set
        extra_live = live_label_set - bundled_label_set
        if missing_live or extra_live:
            detail = []
            if missing_live:
                detail.append(f"bundled but not live: {sorted(missing_live)}")
            if extra_live:
                detail.append(f"live but not bundled: {sorted(extra_live)}")
            drift.append(f"  {org_id} ({gc}): label set differs -- " + "; ".join(detail))

        if parsed.h1 == bundled_title and not (missing_live or extra_live):
            print(f"[OK] {org_id} ({gc}): live matches bundled title + label set")
        else:
            print(f"[DRIFT] {org_id} ({gc}): see below")

    print(f"\nChecked {checked}/{len(docs)} bundled docs against the live "
          f"{pinned_synapse_table} table.")

    if drift:
        print("\nDrift detected:")
        print("\n".join(drift))
        return 1

    print("\nOK: Lambda table pin and all live D4D rows match d4d_data.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
