#!/usr/bin/env python3
"""Submit one SPARQL query to a SageBrain-style async query endpoint and
poll for its result — a minimal, dependency-free connectivity/smoke test.

Not part of the deployed agent; this is a developer tool for verifying
endpoint access and query results directly (see `make sparql-test` in the
repo root Makefile), independent of the Lambda/CloudFormation stack.

Reads all configuration from environment variables so the query text never
has to survive shell quoting:
    SPARQL_ENDPOINT   Full URL of the POST /query endpoint (required)
    SPARQL_QUERY      The SPARQL query text (required)
    POLL_INTERVAL     Seconds between polls (default: 3, matching the
                       vendor doc's own "reasonable client" guidance)
    POLL_TIMEOUT      Total seconds to poll before giving up (default: 90)

The Synapse PAT is resolved by resolve_pat() below, in order: the
SPARQL_PAT env var, then ~/.sagebrain-pat, then ~/.synapseConfig's
`authtoken` field. Deliberately NOT documented as a plain env var read
inline in main() — see resolve_pat()'s docstring for why it matters that
this resolution happens inside the script rather than as a Makefile
variable.

Exit codes: 0 on a "complete" result, 1 on any other outcome (timeout,
"error" status, HTTP failure, missing config) — so `make` reports failure
correctly.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # some macOS python.org installs lack a working
    # local CA bundle; `pip install certifi` fixes this without touching
    # anything AWS/SageBrain-related.


def fail(message: str) -> None:
    print(f"FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def resolve_pat() -> str:
    """SPARQL_PAT env var, then ~/.sagebrain-pat, then ~/.synapseConfig's
    `authtoken` field — resolved entirely inside this process so the value
    never has to be substituted into a Makefile recipe line. `make -n`
    (dry-run) prints fully-substituted recipe text regardless of `@`
    silencing, so a Make variable holding a secret is genuinely unsafe to
    reference in a recipe at all, not just a style preference."""
    env_pat = os.environ.get("SPARQL_PAT", "").strip()
    if env_pat:
        return env_pat

    sagebrain_pat_file = Path.home() / ".sagebrain-pat"
    if sagebrain_pat_file.is_file():
        return sagebrain_pat_file.read_text().strip()

    synapse_config = Path.home() / ".synapseConfig"
    if synapse_config.is_file():
        match = re.search(r"^authtoken\s*=\s*(\S+)", synapse_config.read_text(), re.MULTILINE)
        if match:
            return match.group(1).strip()

    return ""


def http_json(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Source", "cckp-copilot-makefile-test")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (json.JSONDecodeError, TypeError):
            return e.code, {"error": f"HTTP {e.code} (unparseable body)"}


def main() -> None:
    endpoint = os.environ.get("SPARQL_ENDPOINT", "").strip()
    pat = resolve_pat()
    query = os.environ.get("SPARQL_QUERY", "").strip()
    poll_interval = float(os.environ.get("POLL_INTERVAL", "3"))
    poll_timeout = float(os.environ.get("POLL_TIMEOUT", "90"))

    if not endpoint:
        fail("SPARQL_ENDPOINT is not set")
    if not pat:
        fail("SPARQL_PAT is not set (checked ~/.sagebrain-pat and ~/.synapseConfig — see Makefile)")
    if not query:
        fail("SPARQL_QUERY is not set")

    print(f"Endpoint: {endpoint}")
    print(f"Query:    {query}")
    print()

    status, submit_body = http_json("POST", endpoint, pat, {"query": query})
    if status != 202:
        fail(f"submit returned HTTP {status}: {submit_body}")

    job_id = submit_body.get("job_id")
    if not job_id:
        fail(f"submit response had no job_id: {submit_body}")
    print(f"Submitted. job_id={job_id}, status={submit_body.get('status')}")

    poll_url = f"{endpoint.rstrip('/')}/{job_id}"
    deadline = time.monotonic() + poll_timeout
    attempt = 0

    while True:
        attempt += 1
        time.sleep(poll_interval)
        elapsed = poll_timeout - (deadline - time.monotonic())
        status, poll_body = http_json("GET", poll_url, pat)
        job_status = poll_body.get("status", "<no status field>")
        print(f"  poll #{attempt} (t={elapsed:.1f}s): HTTP {status}, status={job_status!r}")

        if job_status == "complete":
            print()
            print("COMPLETE. Raw response:")
            print(json.dumps(poll_body, indent=2))
            results_raw = poll_body.get("results")
            if isinstance(results_raw, str):
                try:
                    parsed = json.loads(results_raw)
                    print()
                    print("Parsed `results` (SPARQL 1.1 Query Results JSON):")
                    print(json.dumps(parsed, indent=2))
                except json.JSONDecodeError:
                    print("\n(note: `results` did not parse as JSON — unexpected shape)")
            sys.exit(0)

        if job_status == "error":
            fail(f"job returned status=error: {poll_body.get('error', poll_body)}")

        if job_status not in ("pending", "running"):
            fail(f"unrecognized status {job_status!r}: {poll_body}")

        if time.monotonic() >= deadline:
            fail(f"gave up after {poll_timeout}s — job was still {job_status!r} (job_id={job_id}, may still complete server-side)")


if __name__ == "__main__":
    main()
