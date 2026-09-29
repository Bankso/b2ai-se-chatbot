#!/usr/bin/env python3
"""Check the b2aiSqlRag Lambda's pinned table versions against the live portal.

The b2ai.standards portal (Sage-Bionetworks/synapse-web-monorepo) pins each
Synapse table it queries to a specific version in
`apps/portals/b2ai.standards/src/config/resources.ts`. This Lambda
(`agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py`) mirrors those
same pins in its `TABLES` dict so its answers always match what a Detail
Page currently shows. If the portal bumps a pin (e.g. after a data refresh)
and this repo isn't re-pinned to match, answers can silently drift from the
live pages.

This script re-fetches resources.ts from the monorepo's `main` branch,
parses its `id: 'synNNN.V'` entries, and diffs them against `TABLES`.

Usage:
    python3 scripts/check_table_pins.py

Exit code 0: every table this Lambda tracks matches resources.ts on main.
Exit code 1: at least one pin has drifted (or resources.ts is missing an
    entry this Lambda expects) -- the printed diff says which alias/table
    and what the live value now is, so `TABLES` can be re-pinned by hand.
Exit code 2: resources.ts could not be fetched (network/GitHub issue) --
    this is a "couldn't check" outcome, not a confirmed drift.
"""

import importlib.util
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

RESOURCES_TS_URL = (
    "https://raw.githubusercontent.com/Sage-Bionetworks/synapse-web-monorepo/"
    "main/apps/portals/b2ai.standards/src/config/resources.ts"
)

# This Lambda's TABLES alias -> the corresponding key in resources.ts's
# `tableInfo` map. Confirmed against resources.ts on 2026-09-24.
ALIAS_TO_RESOURCES_TS_KEY = {
    "standards": "DST_denormalized",
    "datasets": "DataSet_denormalized",
    "organizations": "Organization_denormalized",
    "topics": "DataTopic_denormalized",
    "substrates": "DataSubstrate",
    "manifest": "Manifest",
    "d4d": "D4D_content",
}

LAMBDA_FUNCTION_PATH = (
    Path(__file__).resolve().parent.parent
    / "agents" / "b2ai-copilot" / "lambda" / "b2aiSqlRag" / "lambda_function.py"
)

# Matches a `tableInfo`-style object literal entry, e.g.:
#   DST_denormalized: {
#     name: 'DST_denormalized',
#     id: 'syn65676531.99', // current version of DST_denormalized
#   },
# `[^{}]*` deliberately doesn't allow nested braces, so it only matches flat
# (non-nested) object literals -- which is exactly the shape every
# `tableInfo` entry has.
_BLOCK_RE = re.compile(r"(\w+):\s*\{([^{}]*)\}", re.S)
_ID_RE = re.compile(r"""\bid:\s*['"](syn\w+(?:\.\d+)?)['"]""")


def _load_tables(path: Path) -> dict:
    """Import lambda_function.py by file path (it's a bare module, not a
    package) and return its TABLES dict."""
    spec = importlib.util.spec_from_file_location("b2ai_lambda_function", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TABLES


def _fetch_resources_ts(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "check_table_pins.py"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _parse_pins(resources_ts_text: str) -> dict:
    """Return {tableInfo key: 'synNNN.V'} scanned out of resources.ts."""
    pins = {}
    for m in _BLOCK_RE.finditer(resources_ts_text):
        key, body = m.group(1), m.group(2)
        id_match = _ID_RE.search(body)
        if id_match:
            pins[key] = id_match.group(1)
    return pins


def main() -> int:
    tables = _load_tables(LAMBDA_FUNCTION_PATH)

    try:
        resources_ts_text = _fetch_resources_ts(RESOURCES_TS_URL)
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: failed to fetch resources.ts: {e}", file=sys.stderr)
        return 2

    live_pins = _parse_pins(resources_ts_text)

    drift = []
    checked = []
    for alias, resources_key in ALIAS_TO_RESOURCES_TS_KEY.items():
        lambda_pin = tables.get(alias)
        live_pin = live_pins.get(resources_key)
        if lambda_pin is None:
            drift.append(f"  {alias}: this Lambda's TABLES has no entry for it")
            continue
        if live_pin is None:
            drift.append(
                f"  {alias} ({resources_key}): resources.ts on main has no "
                f"parseable id for this table -- can't verify"
            )
            continue
        checked.append((alias, resources_key, lambda_pin, live_pin))
        if lambda_pin != live_pin:
            drift.append(
                f"  {alias} ({resources_key}): Lambda pins {lambda_pin!r}, "
                f"resources.ts (main) now has {live_pin!r}"
            )

    print(f"Checked {len(checked)}/{len(ALIAS_TO_RESOURCES_TS_KEY)} tables against "
          f"resources.ts on main:")
    for alias, resources_key, lambda_pin, live_pin in checked:
        status = "OK" if lambda_pin == live_pin else "DRIFT"
        print(f"  [{status:5s}] {alias:14s} ({resources_key:26s}) {lambda_pin}")

    if drift:
        print("\nDrift detected -- re-pin TABLES in lambda_function.py:")
        print("\n".join(drift))
        return 1

    print("\nOK: all pinned table versions match resources.ts on main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
