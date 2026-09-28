#!/usr/bin/env python3
"""Build the pinned D4D (Datasheets for Datasets) data file for the
b2aiSqlRag Lambda.

Which D4Ds get bundled, and where their data comes from, is entirely
described by a config file (`scripts/d4d_sources.json` by default, override
with `--config`) -- this script has no hardcoded commit, repo paths, or org
list, so the bundled set can change without touching any code. See that
file's `orgs` map (project name -> B2AI org id) and optional `overrides`
(project name -> an explicit YAML path, for hand-pinning one project's run).

For each project in `orgs` (unless overridden), this script mirrors
`bridge2ai/data-sheets-schema`'s own `canonical_runs(runtime=...)`
(`src/data_sheets_schema/runs.py`, read fresh at the resolved commit every
run): it scans every `data/d4d_concatenated/**/*_provenance.yaml`, keeps the
ones with a top-level `canonical` block whose `run.project` is one of ours
and whose `model.agent_runtime` maps to the config's `runtime`, and takes
`outputs[<variant>].path`. Exactly one match per project is required --
zero or more than one is refused with a clear error rather than guessed.

There is no rendered portal HTML for a freshly-canonicalized run, so labels,
per-field sections, and each doc's title all come from actually *running*
the pinned commit's own renderer (`config["renderer"]`) against the fetched
YAML, then parsing its HTML output -- not from a hand-replicated copy of its
`_humanize_key`/`categorize_data` logic, which upstream has changed more
than once already. See `_load_upstream_renderer` for how the renderer
module is sandboxed (its two schema/description-loading `__init__` steps
are bypassed -- they need a local checkout and a `linkml` install, and
provably do not affect the HTML this script parses; see that function's
docstring). Every key's computed label is still verified 1:1 against the
labels the renderer actually printed, so any drift in that sandboxing (or
in the renderer itself) fails loudly rather than silently mislabeling a
field.

Usage:
    python3 scripts/build_d4d_data.py
    python3 scripts/build_d4d_data.py --config scripts/d4d_sources.json
    python3 scripts/build_d4d_data.py --extra-definitions /path/to/extra.json

Exit code 0: `d4d_data.json` was written successfully.
Exit code 1: a fetch failed, a project had zero or multiple canonical runs,
    or a verification assertion (label/section consistency) failed -- the
    printed message says which.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import re
import sys
import types
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import yaml

GITHUB_API_BASE = "https://api.github.com"

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "agents" / "b2ai-copilot" / "lambda" / "b2aiSqlRag" / "d4d_data.json"
)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "d4d_sources.json"

# Runtime string (as `model.agent_runtime` records it) -> the runtime key a
# config's `runtime` is expressed in. Copied verbatim from `RUNTIME_KEYS` in
# data-sheets-schema's `src/data_sheets_schema/runs.py`.
RUNTIME_KEYS = {
    "claude code": "agentic",
    "claude api (direct)": "api",
    "claude code (direct)": "direct",
}

# module (from config["schema"]'s directory, filename minus .yaml) -> the
# schema's `subsets:` entry it corresponds to, or None if the module has no
# matching D4D subset (its slots/classes are then "other"). `in_subset` is
# never actually populated anywhere in this schema (checked at both the old
# and the new pin), so this hand-maintained map is the only way to recover a
# schema section for a field. Verified against the schema at commit
# 0c9400fcfe8364be258634b309de994096fb7052: 17 modules total; the 2 new ones
# since the previous pin (D4D_Core, D4D_FileCollection -- the new
# file/collection-metadata model) have no subset of their own, same as
# D4D_Base_import/D4D_Human/D4D_Evaluation_Summary/D4D_Metadata/
# D4D_Minimal/D4D_Variables always have.
MODULE_TO_SUBSET: Dict[str, Optional[str]] = {
    "D4D_Motivation": "Motivation",
    "D4D_Composition": "Composition",
    "D4D_Collection": "Collection",
    "D4D_Preprocessing": "Preprocessing-Cleaning-Labeling",
    "D4D_Uses": "Uses",
    "D4D_Distribution": "Distribution",
    "D4D_Maintenance": "Maintenance",
    "D4D_Ethics": "Ethics",
    "D4D_Data_Governance": "DataGovernance",
    "D4D_Base_import": None,
    "D4D_Human": None,
    "D4D_Evaluation_Summary": None,
    "D4D_Metadata": None,
    "D4D_Minimal": None,
    "D4D_Variables": None,
    "D4D_Core": None,
    "D4D_FileCollection": None,
}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "build_d4d_data.py"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"ERROR: failed to fetch {url}: {e}")


def _fetch_json(url: str) -> Any:
    return json.loads(_fetch_url(url).decode("utf-8"))


def _raw_url(repo: str, sha: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def _fetch_raw(repo: str, sha: str, path: str) -> str:
    return _fetch_url(_raw_url(repo, sha, path)).decode("utf-8-sig")


def _fetch_yaml(repo: str, sha: str, path: str) -> Any:
    return yaml.safe_load(_fetch_raw(repo, sha, path))


def resolve_ref(repo: str, ref: str) -> str:
    """Full commit sha a ref (branch, tag, or sha) resolves to right now."""
    data = _fetch_json(f"{GITHUB_API_BASE}/repos/{repo}/commits/{ref}")
    sha = data.get("sha")
    if not sha:
        raise SystemExit(f"ERROR: couldn't resolve {repo}@{ref} to a commit sha: {data}")
    return sha


def fetch_git_tree(repo: str, sha: str) -> List[dict]:
    """Every path in the repo at `sha` (git Trees API, recursive)."""
    data = _fetch_json(f"{GITHUB_API_BASE}/repos/{repo}/git/trees/{sha}?recursive=1")
    if data.get("truncated"):
        raise SystemExit(
            f"ERROR: {repo}@{sha}'s tree listing was truncated by the GitHub API -- "
            f"too large to enumerate in one call. This script needs a paginated/"
            f"sparse-clone fallback to handle a repo this size."
        )
    return data.get("tree", [])


# ---------------------------------------------------------------------------
# Canonical run resolution -- mirrors `canonical_runs(runtime=...)` in
# data-sheets-schema's `src/data_sheets_schema/runs.py`.
# ---------------------------------------------------------------------------


def runtime_of(record: dict) -> Optional[str]:
    """`api`, `agentic`, `direct`, or None -- verbatim port of `runtime_of`."""
    model = record.get("model") if isinstance(record, dict) else None
    value = (model or {}).get("agent_runtime") if isinstance(model, dict) else None
    if not isinstance(value, str):
        return None
    return RUNTIME_KEYS.get(value.strip().lower())


def _resolve_artifact_path(entry_path: Optional[str], provenance_path: str) -> Optional[str]:
    """Port of `_canonical_artifact_path`/`artifact_root`, over repo-relative
    path strings instead of a local filesystem (there is no local checkout).

    A canonical provenance record lives at
    `data/d4d_concatenated/{method}_core/{label}/{file}_provenance.yaml`;
    `artifact_root` resolves a relative `outputs[variant].path` against the
    repo root in exactly that case (and only that case -- anything else, run
    locally, falls back to `Path.cwd()`, which has no meaning for a fetch-
    based resolver, so it's treated as unresolvable here instead of guessed).
    """
    if not entry_path:
        return None
    if PurePosixPath(entry_path).is_absolute():
        return entry_path
    parts = PurePosixPath(provenance_path).parts
    try:
        idx = next(
            i for i in range(len(parts) - 1)
            if parts[i] == "data" and parts[i + 1] == "d4d_concatenated"
        )
    except StopIteration:
        return None
    tail = parts[idx + 2:]
    if len(tail) == 3 and tail[0].endswith("_core") and tail[2].endswith("_provenance.yaml"):
        owner = parts[:idx]
        return str(PurePosixPath(*owner, entry_path)) if owner else entry_path
    return None


def _find_provenance_paths(tree: List[dict]) -> List[str]:
    return sorted(
        entry["path"] for entry in tree
        if entry.get("type") == "blob"
        and entry["path"].startswith("data/d4d_concatenated/")
        and entry["path"].endswith("_provenance.yaml")
    )


def resolve_canonical_runs(
    repo: str,
    sha: str,
    tree: List[dict],
    orgs: Dict[str, str],
    runtime: str,
    variant: str,
    overrides: Dict[str, str],
) -> Dict[str, dict]:
    """org id -> {"project", "label", "yamlPath", "provenancePath", "criterion"}.

    Refuses (raises SystemExit) if a non-overridden project has zero or more
    than one canonical mark under `runtime`.
    """
    runs: Dict[str, dict] = {}

    for project, override_path in overrides.items():
        if project not in orgs:
            continue
        runs[orgs[project]] = {
            "project": project,
            "label": None,
            "yamlPath": override_path,
            "provenancePath": None,
            "criterion": "manual override (d4d_sources.json 'overrides')",
        }

    remaining = {p: oid for p, oid in orgs.items() if p not in overrides}
    if not remaining:
        return runs

    prov_paths = _find_provenance_paths(tree)
    print(f"Scanning {len(prov_paths)} provenance files for canonical "
          f"{runtime!r}-runtime runs of {sorted(remaining)}...")

    def _check(path: str) -> Optional[dict]:
        try:
            data = yaml.safe_load(_fetch_raw(repo, sha, path))
        except Exception as e:
            print(f"  WARNING: couldn't parse {path}: {e}", file=sys.stderr)
            return None
        if not isinstance(data, dict) or "canonical" not in data:
            return None
        run = data.get("run") or {}
        project, label = run.get("project"), run.get("label")
        if not project or project not in remaining or not label:
            return None
        if runtime_of(data) != runtime:
            return None
        outputs = data.get("outputs") or {}
        entry = outputs.get(variant)
        yaml_path = _resolve_artifact_path((entry or {}).get("path"), path)
        if entry and not yaml_path:
            raise SystemExit(
                f"ERROR: {path}'s outputs[{variant!r}].path "
                f"({entry.get('path')!r}) isn't a resolvable artifact path "
                f"(record isn't under the expected "
                f"'{{method}}_core/{{label}}/{{file}}_provenance.yaml' layout)"
            )
        return {
            "project": project,
            "label": label,
            "yamlPath": yaml_path,
            "provenancePath": path,
            "criterion": (data["canonical"] or {}).get("criterion"),
        }

    seen: Dict[str, List[str]] = {}
    found: Dict[str, dict] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for path, result in zip(prov_paths, pool.map(_check, prov_paths)):
            done += 1
            if done % 50 == 0 or done == len(prov_paths):
                print(f"  ...{done}/{len(prov_paths)}")
            if result:
                seen.setdefault(result["project"], []).append(result["label"])
                found[result["project"]] = result

    ambiguous = {p: labels for p, labels in seen.items() if len(labels) > 1}
    if ambiguous:
        detail = "; ".join(f"{p}: {sorted(labels)}" for p, labels in ambiguous.items())
        raise SystemExit(
            f"ERROR: more than one canonical {runtime!r}-runtime run per "
            f"project -- refusing to guess ({detail})"
        )
    missing = sorted(set(remaining) - set(found))
    if missing:
        raise SystemExit(
            f"ERROR: no canonical {runtime!r}-runtime run found for "
            f"project(s) {missing} under {repo}@{sha}"
        )

    for project, org_id in remaining.items():
        result = dict(found[project])
        result.pop("project", None)
        result["project"] = project
        runs[org_id] = result

    return runs


# ---------------------------------------------------------------------------
# Sandboxed execution of the pinned commit's own renderer.
# ---------------------------------------------------------------------------


def _load_upstream_renderer(source: str):
    """Exec the fetched `human_readable_renderer.py` and return a usable
    `HumanReadableRenderer` instance -- running the pinned commit's actual
    rendering code (`categorize_data`, `_humanize_key`/`FIELD_LABEL_MAP`,
    `format_value`, `render_to_html`) rather than a hand-copied
    reimplementation, since that logic has already changed once between
    pins (a curated `FIELD_LABEL_MAP` was added) and would silently drift
    again otherwise.

    Only `HumanReadableRenderer.__init__`'s two side-loading steps are
    bypassed, via a subclass, because both need things this script
    deliberately doesn't set up (a local checkout for the D4D_*.yaml module
    files, and a `linkml`/`SchemaView` install) and neither affects a single
    byte of what render_to_html emits for the fields this script checks:
      - `_load_schema_info` (imports `data_sheets_schema.schema_view`) feeds
        `_is_required_field`, whose result the current template no longer
        renders at all -- every label's class is the literal string
        "item-label optional-field" regardless (confirmed by reading the
        fetched template).
      - `_populate_section_descriptions` only fills the `<p
        class="section-description">` subtitle text under each h2; it never
        touches the h2 heading text itself (that's the hardcoded `title` in
        `self.d4d_sections`, set directly in `__init__`) or any label.
    Every label this script derives is still verified 1:1 against what the
    resulting HTML actually contains (see `_verify_and_collect`), so if this
    reasoning is ever wrong for a future renderer version, the mismatch
    check fails loudly rather than silently mislabeling a field.
    """
    resources_stub = types.ModuleType("data_sheets_schema.resources")

    def _unavailable_resource_path(_path):
        raise NotImplementedError(
            "resource_path is stubbed out in this sandbox; only reachable "
            "from the two __init__ steps this script bypasses"
        )

    resources_stub.resource_path = _unavailable_resource_path
    pkg_stub = types.ModuleType("data_sheets_schema")
    pkg_stub.__path__ = []
    sys_modules_backup = {}
    import sys as _sys
    for name, mod in (("data_sheets_schema", pkg_stub),
                      ("data_sheets_schema.resources", resources_stub)):
        sys_modules_backup[name] = _sys.modules.get(name)
        _sys.modules[name] = mod

    ns: Dict[str, Any] = {"__name__": "upstream_human_readable_renderer"}
    exec(compile(source, "<pinned human_readable_renderer.py>", "exec"), ns)
    BaseRenderer = ns["HumanReadableRenderer"]

    class _SandboxRenderer(BaseRenderer):
        def _load_schema_info(self):
            return {}

        def _populate_section_descriptions(self):
            for meta in self.d4d_sections.values():
                meta.setdefault("description", "")

    return _SandboxRenderer()


# ---------------------------------------------------------------------------
# HTML parsing (same shape as before): <h1>, <h2 class="section-title">
# headings in order, and every <label class="item-label ...">.
# ---------------------------------------------------------------------------


class _D4DHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1: Optional[str] = None
        self.section_order: List[str] = []
        self.labels: List[Tuple[str, Optional[str]]] = []

        self._in_h1 = False
        self._h1_buf: List[str] = []
        self._in_h2 = False
        self._h2_buf: List[str] = []
        self._cur_section: Optional[str] = None
        self._in_label = False
        self._label_buf: List[str] = []
        self._in_required_span = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_map = dict(attrs)
        classes = (attr_map.get("class") or "").split()
        if tag == "h1":
            self._in_h1 = True
            self._h1_buf = []
        elif tag == "h2" and "section-title" in classes:
            self._in_h2 = True
            self._h2_buf = []
        elif tag == "label" and "item-label" in classes:
            self._in_label = True
            self._label_buf = []
        elif self._in_label and tag == "span" and "required-indicator" in classes:
            self._in_required_span = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_h1:
            self.h1 = "".join(self._h1_buf).strip()
            self._in_h1 = False
        elif tag == "h2" and self._in_h2:
            self._cur_section = "".join(self._h2_buf).strip()
            self.section_order.append(self._cur_section)
            self._in_h2 = False
        elif tag == "span" and self._in_required_span:
            self._in_required_span = False
        elif tag == "label" and self._in_label:
            text = "".join(self._label_buf).strip()
            self.labels.append((text, self._cur_section))
            self._in_label = False

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self._h1_buf.append(data)
        elif self._in_h2:
            self._h2_buf.append(data)
        elif self._in_label and not self._in_required_span:
            self._label_buf.append(data)


def _parse_html(html_text: str) -> _D4DHtmlParser:
    parser = _D4DHtmlParser()
    parser.feed(html_text)
    return parser


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------


def _load_schema_resolution(
    repo: str, sha: str, schema_path: str, tree: List[dict]
) -> Tuple[dict, Dict[str, dict], Dict[str, str]]:
    """Returns (schema_dict, dataset_slot_info, slot_or_class_to_module).

    `dataset_slot_info`: {slot_name: {"range", "description"}} for every
    slot/attribute of class Dataset.
    `slot_or_class_to_module`: {name: module_stem}, discovered from every
    `D4D_*.yaml` file that actually exists (at this sha) beside the schema
    -- not a hardcoded file list, so new/removed modules are picked up
    automatically.
    """
    schema = _fetch_yaml(repo, sha, schema_path)
    classes = schema.get("classes", {})
    dataset_cls = classes.get("Dataset")
    if not dataset_cls:
        raise SystemExit(f"ERROR: {schema_path}@{sha} has no 'Dataset' class")

    own_slot_names = list(dataset_cls.get("slots") or [])
    attributes = dict(dataset_cls.get("attributes") or {})
    top_slots = schema.get("slots") or {}

    dataset_slot_info: Dict[str, dict] = {}
    for name in own_slot_names:
        slot_def = top_slots.get(name) or {}
        dataset_slot_info[name] = {
            "range": slot_def.get("range"),
            "description": slot_def.get("description"),
        }
    for name, attr_def in attributes.items():
        dataset_slot_info[name] = {
            "range": attr_def.get("range"),
            "description": attr_def.get("description"),
        }

    schema_dir = str(PurePosixPath(schema_path).parent) + "/"
    module_paths = sorted(
        entry["path"] for entry in tree
        if entry.get("type") == "blob"
        and entry["path"].startswith(schema_dir)
        and re.match(r"D4D_.*\.yaml$", PurePosixPath(entry["path"]).name)
    )

    slot_or_class_to_module: Dict[str, str] = {}
    unmapped_modules = []
    for path in module_paths:
        module_stem = PurePosixPath(path).stem
        if module_stem not in MODULE_TO_SUBSET:
            unmapped_modules.append(module_stem)
        module_data = _fetch_yaml(repo, sha, path)
        for slot_name in (module_data.get("slots") or {}).keys():
            slot_or_class_to_module.setdefault(slot_name, module_stem)
        for class_name in (module_data.get("classes") or {}).keys():
            slot_or_class_to_module.setdefault(class_name, module_stem)

    if unmapped_modules:
        print(
            f"WARNING: {sorted(unmapped_modules)} are schema modules at "
            f"{sha} not in this script's MODULE_TO_SUBSET map -- treating "
            f"as 'other'. Add them to MODULE_TO_SUBSET if they should map "
            f"to a real subset.",
            file=sys.stderr,
        )

    return schema, dataset_slot_info, slot_or_class_to_module


def _resolve_schema_section(
    key: str,
    dataset_slot_info: Dict[str, dict],
    slot_or_class_to_module: Dict[str, str],
) -> str:
    info = dataset_slot_info.get(key)
    if info is None:
        return "other"
    module = slot_or_class_to_module.get(key)
    if module is None:
        rng = info.get("range")
        if rng:
            module = slot_or_class_to_module.get(rng)
    if module is None:
        return "other"
    subset = MODULE_TO_SUBSET.get(module)
    return _slugify(subset) if subset else "other"


# ---------------------------------------------------------------------------
# JSON-safety
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------


def _load_extra_definitions(path: Optional[str]) -> Dict[str, dict]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: --extra-definitions {path} must be a JSON object")
    return data


def build(config_path: str, extra_definitions_path: Optional[str]) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    repo = config["repo"]
    ref = config["ref"]
    runtime = config["runtime"]
    variant = config["variant"]
    synapse_table = config["synapseTable"]
    schema_path = config["schema"]
    renderer_path = config["renderer"]
    orgs: Dict[str, str] = config["orgs"]
    overrides: Dict[str, str] = config.get("overrides") or {}

    extra_definitions = _load_extra_definitions(extra_definitions_path)

    print(f"Resolving {repo}@{ref}...")
    sha = resolve_ref(repo, ref)
    print(f"  -> {sha}")

    print("Fetching repo tree...")
    tree = fetch_git_tree(repo, sha)
    print(f"  {len(tree)} entries")

    runs = resolve_canonical_runs(repo, sha, tree, orgs, runtime, variant, overrides)
    for org_id, run in sorted(runs.items()):
        print(f"  {org_id} ({run['project']}): label={run['label']!r} "
              f"yamlPath={run['yamlPath']!r}")

    print("Loading schema resolution...")
    schema, dataset_slot_info, slot_or_class_to_module = _load_schema_resolution(
        repo, sha, schema_path, tree
    )

    subsets = schema.get("subsets") or {}
    sections = []
    for name, subset_def in subsets.items():
        sections.append(
            {
                "id": _slugify(name),
                "heading": name,
                "description": subset_def.get("description"),
            }
        )
    sections.append(
        {
            "id": "other",
            "heading": "Other",
            "description": "Fields the schema does not assign to a D4D section.",
        }
    )

    print("Loading and sandboxing the pinned renderer...")
    renderer_source = _fetch_raw(repo, sha, renderer_path)
    renderer = _load_upstream_renderer(renderer_source)

    docs: Dict[str, dict] = {}
    yaml_datas: Dict[str, dict] = {}
    parsed_htmls: Dict[str, _D4DHtmlParser] = {}

    for org_id, run in runs.items():
        yaml_path = run["yamlPath"]
        print(f"Fetching + rendering {yaml_path}...")
        yaml_text = _fetch_raw(repo, sha, yaml_path)
        yaml_data = yaml.safe_load(yaml_text)
        if not isinstance(yaml_data, dict):
            raise SystemExit(f"ERROR: {yaml_path} did not parse to a YAML mapping")
        yaml_datas[org_id] = yaml_data

        title_basis = PurePosixPath(yaml_path).stem  # matches render_yaml_file's base_name
        html_text = renderer.render_to_html(yaml_data, title_basis)
        parsed = _parse_html(html_text)
        parsed_htmls[org_id] = parsed
        if not parsed.h1:
            raise SystemExit(f"ERROR: renderer produced no <h1> for {yaml_path}")

        docs[org_id] = {
            "gc": run["project"].replace("_", "-"),
            # The renderer's <h1> comes from the file stem ("AI READI d4d"), so
            # the agent-facing title is the record's own; portalTitle keeps the
            # <h1> for check_d4d_pin.py.
            "title": yaml_data.get("title") or yaml_data.get("name") or parsed.h1,
            "portalTitle": parsed.h1,
            "data": _json_safe(yaml_data),
        }

    # --- verify labels + collect portalSection per (org, key) ---
    field_portal_section: Dict[str, Dict[str, Optional[str]]] = {}
    for org_id, run in runs.items():
        yaml_data = yaml_datas[org_id]
        parsed = parsed_htmls[org_id]
        yaml_path = run["yamlPath"]

        key_to_label = {k: renderer._humanize_key(k) for k in yaml_data.keys() if k}
        label_to_section: Dict[str, Optional[str]] = {}
        label_seen_sections: Dict[str, set] = {}
        for text, section in parsed.labels:
            label_seen_sections.setdefault(text, set()).add(section)
            label_to_section[text] = section

        ambiguous = {t: s for t, s in label_seen_sections.items() if len(s) > 1}
        if ambiguous:
            raise SystemExit(
                f"ERROR: rendering {yaml_path} put the same label under "
                f"multiple sections (can't assign a single portalSection): "
                f"{ambiguous}"
            )

        html_label_set = set(label_to_section.keys())
        computed_label_set = set(key_to_label.values())

        missing_in_html = computed_label_set - html_label_set
        if missing_in_html:
            raise SystemExit(
                f"ERROR: rendering {yaml_path} produced no label for "
                f"computed label(s) {sorted(missing_in_html)} -- "
                f"the renderer's _humanize_key/FIELD_LABEL_MAP may not be "
                f"deterministic, or a non-empty top-level key isn't "
                f"reaching the HTML"
            )
        extra_in_html = html_label_set - computed_label_set
        if extra_in_html:
            raise SystemExit(
                f"ERROR: rendering {yaml_path} produced label(s) "
                f"{sorted(extra_in_html)} that don't map back to any "
                f"top-level YAML key -- the renderer may be flattening "
                f"nested structures (e.g. a top-level 'DatasetCollection' "
                f"key) into extra labels for this doc; this script doesn't "
                f"handle that case"
            )
        if len(key_to_label) != len(parsed.labels):
            raise SystemExit(
                f"ERROR: rendering {yaml_path} produced {len(parsed.labels)} "
                f"labels but the YAML has {len(key_to_label)} non-empty "
                f"top-level keys -- expected an exact 1:1 mapping"
            )

        for key, label in key_to_label.items():
            section = label_to_section.get(label)
            field_portal_section.setdefault(key, {})[org_id] = section

    final_portal_section: Dict[str, Optional[str]] = {}
    for key, by_org in field_portal_section.items():
        distinct = set(by_org.values())
        if len(distinct) > 1:
            raise SystemExit(
                f"ERROR: field {key!r} maps to different rendered sections "
                f"across docs: {by_org}"
            )
        final_portal_section[key] = next(iter(distinct))

    # --- build fields, in deterministic first-seen order across docs ---
    fields: Dict[str, dict] = {}
    for org_id in runs:
        for key in yaml_datas[org_id].keys():
            if key in fields:
                continue
            in_schema = key in dataset_slot_info
            if in_schema:
                schema_section = _resolve_schema_section(
                    key, dataset_slot_info, slot_or_class_to_module
                )
                schema_description = dataset_slot_info[key].get("description")
            else:
                schema_section = "other"
                schema_description = None

            description = None
            description_source = None
            extra_source = None
            if schema_description:
                description = schema_description
                description_source = "schema"
            elif key in extra_definitions:
                extra = extra_definitions[key]
                description = extra.get("description")
                description_source = "extra"
                extra_source = extra.get("source")

            fields[key] = {
                "label": renderer._humanize_key(key),
                "description": description,
                "descriptionSource": description_source,
                "extraSource": extra_source,
                "schemaSection": schema_section,
                "portalSection": final_portal_section.get(key),
                "inSchema": in_schema,
            }

    result = {
        "source": {
            "repo": repo,
            "ref": ref,
            "commit": sha,
            "runtime": runtime,
            "variant": variant,
            "synapseTable": synapse_table,
            "schema": schema_path,
            "renderer": renderer_path,
            "runs": runs,
        },
        "sections": sections,
        "fields": fields,
        "docs": docs,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Source config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--extra-definitions",
        metavar="PATH",
        default=None,
        help=(
            "Optional JSON file shaped "
            '{"<top-level key>": {"description": str, "source": "<path>@<sha>"}} '
            "used to fill descriptions for fields the pinned schema doesn't define."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=str(OUTPUT_PATH),
        help=f"Output path (default: {OUTPUT_PATH})",
    )
    args = parser.parse_args()

    result = build(args.config, args.extra_definitions)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(f"Wrote {output_path} ({len(result['fields'])} fields, "
          f"{len(result['sections'])} sections, {len(result['docs'])} docs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
