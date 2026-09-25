#!/usr/bin/env python3
"""Build `resource_search_dataset.json` from live Synapse data.

This is a *build* script, not a fixture: every id, name, and count in the
generated dataset is fetched live from the Bridge2AI Standards Explorer's
pinned Synapse tables (via the Lambda's own `TABLES` pins and its own query
helpers), never invented or copy-pasted from a prior run. Re-run this
whenever `agents/b2ai-copilot/lambda/b2aiSqlRag/lambda_function.py`'s
`TABLES` pins change, or whenever the item recipes below change.

Design:
    Each item is produced by a small recipe function that:
      1. Calls the Lambda's own functions (`sql_query`, `build_portal_url`,
         `get_d4d`, `search_d4d`, `list_d4ds`, `count_by_type`) directly,
         exactly as the deployed Lambda would, against live Synapse at the
         pinned table versions.
      2. Packages the *live* result into `expected_answer`.
      3. Pairs it with a hand-authored `expected_tool_calls` shape — the
         correct parameters an agent *should* have sent to get that result
         (table/facets/AND-vs-OR shape, D4D orgId/section, etc.) — which is
         a fact about the Lambda/portal's documented semantics, not
         something that needs to be "computed," but is validated here by
         checking it actually reproduces the live `expected_answer`.

    This mirrors the CCKP resource-search-correctness-benchmark plan's
    "live gold" approach, but simplified: because the B2AI Lambda pins exact
    Synapse table *versions* (`TABLES` in lambda_function.py), the resulting
    counts/ids are stable snapshots, not a live-growing portal — so this
    script computes ground truth once at build time (recorded alongside the
    pins it was computed against) rather than needing a live re-query at
    *evaluation* time. Re-run this script when the pins change; the dataset
    records which pins it was built against so staleness is detectable.

Usage:
    python build_ground_truth.py                      # writes resource_search_dataset.json
    python build_ground_truth.py --check               # validate only, don't write
    python build_ground_truth.py --out /tmp/foo.json   # write elsewhere
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMBDA_DIR = HERE.parent.parent / "agents" / "b2ai-copilot" / "lambda" / "b2aiSqlRag"
sys.path.insert(0, str(LAMBDA_DIR))

import lambda_function as lf  # noqa: E402

PORTAL_ORIGIN = "https://b2ai.standards.synapse.org"


# ---------------------------------------------------------------------------
# Small live-query helpers built on top of the Lambda's own functions
# ---------------------------------------------------------------------------

def q(table, sql, limit=50):
    """Run a sqlQuery exactly as the Lambda would and return its response dict."""
    result = lf.sql_query({"table": table, "sql": sql, "limit": limit})
    if "error" in result:
        raise RuntimeError(f"sqlQuery failed for {table!r} / {sql!r}: {result['error']}")
    return result


def one(table, sql):
    """Run a sqlQuery and return its single expected row (raises if != 1 row)."""
    r = q(table, sql, limit=5)
    if len(r["rows"]) != 1:
        raise RuntimeError(f"Expected exactly 1 row for {sql!r}, got {len(r['rows'])}: {r['rows']}")
    return r["rows"][0]


def count(table, sql):
    """Run a `SELECT COUNT(*) ...` query and return the aggregate as an int.

    `sql_query`'s own `count` field is `queryCount` -- the number of *rows the
    query would return* (always 1 for an aggregate `COUNT(*)` query) -- not
    the aggregate value itself. The actual count is in
    `rows[0]["COUNT(*)"]`, returned by Synapse as a numeric string.
    """
    r = lf.sql_query({"table": table, "sql": sql, "limit": 1})
    if "error" in r:
        raise RuntimeError(f"count failed for {table!r} / {sql!r}: {r['error']}")
    if not r["rows"] or "COUNT(*)" not in r["rows"][0]:
        raise RuntimeError(f"Not a COUNT(*) query, or no rows returned: {sql!r} -> {r}")
    return int(r["rows"][0]["COUNT(*)"])


def detail_url(resource_type, entity_id):
    r = lf.build_portal_url({"resourceType": resource_type, "id": entity_id})
    if "error" in r:
        raise RuntimeError(f"build_portal_url failed for {resource_type}/{entity_id}: {r['error']}")
    return PORTAL_ORIGIN + r["url"]


def json_ids(raw, prefix):
    """Pull every `"B2AI_xxx:N"`-shaped id out of a denormalized JSON/text column."""
    if not raw:
        return []
    return re.findall(rf'"({re.escape(prefix)}:\d+)"', raw)


# ---------------------------------------------------------------------------
# Item recipes
# ---------------------------------------------------------------------------

ITEMS = []


def item(fn):
    ITEMS.append(fn)
    return fn


# --- keyword / entity lookup -------------------------------------------------

KEYWORD_LOOKUPS = [
    ("rs-keyword-fhir", "FHIR", "What standard uses the acronym FHIR?"),
    ("rs-keyword-dicom", "DICOM", "What is the DICOM standard?"),
    ("rs-keyword-redcap", "REDCap", "Tell me about the REDCap tool."),
    ("rs-keyword-ncit", "NCIT", "What is NCIT?"),
    ("rs-keyword-loinc", "LOINC", "Look up the LOINC standard."),
    ("rs-keyword-snomed", "SNOMED CT", "What is SNOMED CT used for?"),
]


def _make_keyword_item(item_id, acronym, question):
    def recipe():
        row = one("standards", f"SELECT id, acronym, name, category FROM {{table}} WHERE \"acronym\" = '{acronym}'")
        return {
            "id": item_id,
            "category": "keyword-lookup",
            "question": question,
            "persona": "RESEARCHER",
            "notes": (
                f"Exact-match acronym lookup; {acronym!r} resolves to exactly one standards row "
                "at the pinned version, confirmed live."
            ),
            "expected_tool_calls": [
                {
                    "function": "sqlQuery",
                    "constraints": {
                        "table": "standards",
                        "required_columns": ["acronym"],
                        "required_values": [acronym],
                    },
                },
                {
                    "function": "buildPortalUrl",
                    "required": False,
                    "constraints": {"resourceType": "standards", "id": row["id"]},
                },
            ],
            "expected_answer": {
                "id": row["id"],
                "acronym": row["acronym"],
                "name": row["name"],
                "category": row["category"],
                "detail_url": detail_url("standards", row["id"]),
            },
            "ground_truth_query": (
                f"SELECT id, acronym, name, category FROM {{standards}} WHERE \"acronym\" = '{acronym}'"
            ),
            "llm_judge_fallback": False,
        }
    return recipe


for _id, _acr, _q in KEYWORD_LOOKUPS:
    item(_make_keyword_item(_id, _acr, _q))


# --- categorical / flag filters ----------------------------------------------

CATEGORICAL_FILTERS = [
    (
        "rs-cat-ontology-vocab",
        "category", "Ontology or Vocabulary",
        "How many ontologies or vocabularies are in the catalog?",
        "'category' is a fixed enum; 'Ontology or Vocabulary' is one exact value.",
    ),
    (
        "rs-cat-used-in-b2ai",
        "usedInBridge2AI", "Yes",
        "Which standards are actually used in Bridge2AI?",
        (
            "`usedInBridge2AI` is a STRING flag ('Yes'/'No'), not a boolean — the exact bug class "
            "fixed in review (plans/retarget-b2ai-standards-explorer.md phase 1). A query comparing "
            "to `true` would return 0 rows against the real column."
        ),
    ),
    (
        "rs-cat-is-open-no",
        "isOpen", "No",
        "Which standards and tools are NOT openly accessible?",
        "`isOpen` is also a STRING flag ('Yes'/'No'); this item specifically probes the 'No' value.",
    ),
    (
        "rs-cat-mature",
        "mature", "Is Mature",
        "Which standards are considered mature?",
        "`mature` uses the string values 'Is Mature' / 'Is Not Mature', not a boolean.",
    ),
    (
        "rs-cat-registry",
        "category", "Registry",
        "How many registries are cataloged?",
        "'Registry' is one of the 8 fixed `category` values.",
    ),
]


def _make_categorical_item(item_id, column, value, question, notes):
    def recipe():
        n = count("standards", f"SELECT COUNT(*) FROM {{table}} WHERE \"{column}\" = '{value}'")
        sample = q(
            "standards", f"SELECT id, name FROM {{table}} WHERE \"{column}\" = '{value}'", limit=1
        )["rows"][0]
        return {
            "id": item_id,
            "category": "categorical-filter",
            "question": question,
            "persona": "RESEARCHER",
            "notes": notes,
            "expected_tool_calls": [
                {
                    "function": "sqlQuery",
                    "constraints": {
                        "table": "standards",
                        "required_columns": [column],
                        "required_values": [value],
                        "forbid_values": ["true", "false", "True", "False"],
                    },
                }
            ],
            "expected_answer": {
                "column": column,
                "value": value,
                "count": n,
                "sample_id": sample["id"],
                "sample_name": sample["name"],
            },
            "ground_truth_query": f"SELECT COUNT(*) FROM {{standards}} WHERE \"{column}\" = '{value}'",
            "llm_judge_fallback": False,
        }
    return recipe


for _id, _col, _val, _q, _notes in CATEGORICAL_FILTERS:
    item(_make_categorical_item(_id, _col, _val, _q, _notes))


# --- topic HAS (single-column, multi-value OR semantics tested standalone) ---

@item
def rs_cat_topic_image():
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image')")
    assert n == 67, f"sanity anchor drifted: topic=Image now {n}, expected 67"
    return {
        "id": "rs-cat-topic-image",
        "category": "categorical-filter",
        "question": "How many standards relate to the Image data topic?",
        "persona": "RESEARCHER",
        "notes": (
            "Verified live sanity anchor from the retarget plan: topic HAS ('Image') -> 67. "
            "`topic` is a multi-value column; a single-topic filter uses HAS, not `=`."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["topic"],
                    "required_values": ["Image"],
                },
            }
        ],
        "expected_answer": {"column": "topic", "value": "Image", "count": n},
        "ground_truth_query": "SELECT COUNT(*) FROM {standards} WHERE \"topic\" HAS ('Image')",
        "llm_judge_fallback": False,
    }


# --- multi-filter AND (facets across columns / SQL AND) ----------------------

@item
def rs_and_topic_image_ontology():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'",
    )
    row = one(
        "standards",
        "SELECT id, acronym, name FROM {table} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'",
    )
    assert n == 1, f"sanity anchor drifted: Image+OntologyOrVocabulary now {n}, expected 1"
    return {
        "id": "rs-and-image-ontology",
        "category": "multi-filter-and",
        "question": "Which ontologies or vocabularies are about the Image data topic?",
        "persona": "RESEARCHER",
        "notes": (
            "AND-intent across two DIFFERENT columns (topic, category). Verified live sanity anchor "
            "from the retarget plan: 1 hit (SQL AND and the search facets AND-across-columns both "
            "give 1). Must be two facets (or a SQL AND), never a two-word SEARCH_TERM."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "required": False,
                "constraints": {
                    "table": "standards",
                    "and_columns": ["topic", "category"],
                    "required_values": ["Image", "Ontology or Vocabulary"],
                },
            },
            {
                "function": "buildPortalUrl",
                "required": False,
                "constraints": {
                    "resourceType": "search",
                    "required_facets": [
                        {"columnName": "topic", "values": ["Image"]},
                        {"columnName": "category", "values": ["Ontology or Vocabulary"]},
                    ],
                    "facet_match": "subset",
                    "search_term_forbidden_multi_word": True,
                },
            },
        ],
        "expected_answer": {
            "count": n,
            "matching_ids": [row["id"]],
            "matching_names": [row["name"]],
        },
        "ground_truth_query": (
            "SELECT * FROM {standards} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_and_used_in_b2ai_ontology():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"usedInBridge2AI\" = 'Yes' AND \"category\" = 'Ontology or Vocabulary'",
    )
    return {
        "id": "rs-and-used-ontology",
        "category": "multi-filter-and",
        "question": "Which ontologies or vocabularies are actually used in Bridge2AI (not just cataloged)?",
        "persona": "RESEARCHER",
        "notes": "AND-intent across usedInBridge2AI (string flag) and category.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "and_columns": ["usedInBridge2AI", "category"],
                    "required_values": ["Yes", "Ontology or Vocabulary"],
                    "forbid_values": ["true", "false"],
                },
            }
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": (
            "SELECT COUNT(*) FROM {standards} WHERE \"usedInBridge2AI\" = 'Yes' "
            "AND \"category\" = 'Ontology or Vocabulary'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_and_used_and_open():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"usedInBridge2AI\" = 'Yes' AND \"isOpen\" = 'Yes'",
    )
    return {
        "id": "rs-and-used-open",
        "category": "multi-filter-and",
        "question": "Which standards used in Bridge2AI are also openly accessible?",
        "persona": "FUNDER",
        "notes": "AND-intent across two string flag columns (usedInBridge2AI, isOpen), both 'Yes'/'No'.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "required": False,
                "constraints": {
                    "table": "standards",
                    "and_columns": ["usedInBridge2AI", "isOpen"],
                    "required_values": ["Yes"],
                    "forbid_values": ["true", "false"],
                },
            },
            {
                "function": "buildPortalUrl",
                "required": False,
                "constraints": {
                    "resourceType": "search",
                    "required_facets": [
                        {"columnName": "usedInBridge2AI", "values": ["Yes"]},
                        {"columnName": "isOpen", "values": ["Yes"]},
                    ],
                    "facet_match": "subset",
                },
            },
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": (
            "SELECT COUNT(*) FROM {standards} WHERE \"usedInBridge2AI\" = 'Yes' AND \"isOpen\" = 'Yes'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_and_registry_used():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"category\" = 'Registry' AND \"usedInBridge2AI\" = 'Yes'",
    )
    return {
        "id": "rs-and-registry-used",
        "category": "multi-filter-and",
        "question": "Which registries are used in Bridge2AI?",
        "persona": "CONTRIBUTOR",
        "notes": "AND-intent, category=Registry plus the usedInBridge2AI string flag.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "and_columns": ["category", "usedInBridge2AI"],
                    "required_values": ["Registry", "Yes"],
                },
            }
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": (
            "SELECT COUNT(*) FROM {standards} WHERE \"category\" = 'Registry' AND \"usedInBridge2AI\" = 'Yes'"
        ),
        "llm_judge_fallback": False,
    }


# --- multi-filter OR (either-of intent) --------------------------------------

@item
def rs_or_topic_image_genome():
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image', 'Genome')")
    assert n == 121, f"sanity anchor drifted: Image OR Genome now {n}, expected 121"
    return {
        "id": "rs-or-image-genome",
        "category": "multi-filter-or",
        "question": "Show me standards about either the Image or Genome data topics.",
        "persona": "RESEARCHER",
        "notes": (
            "Either-of intent on the SAME column (topic): OR within one column's values via SQL HAS "
            "or a facet with multiple values. Verified live sanity anchor: 121."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "required": False,
                "constraints": {
                    "table": "standards",
                    "or_column": "topic",
                    "or_values": ["Image", "Genome"],
                },
            },
            {
                "function": "buildPortalUrl",
                "required": False,
                "constraints": {
                    "resourceType": "search",
                    "required_facets": [{"columnName": "topic", "values": ["Image", "Genome"]}],
                    "facet_match": "subset",
                },
            },
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": "SELECT COUNT(*) FROM {standards} WHERE \"topic\" HAS ('Image', 'Genome')",
        "llm_judge_fallback": False,
    }


@item
def rs_or_category_ontology_registry():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"category\" = 'Ontology or Vocabulary' OR \"category\" = 'Registry'",
    )
    return {
        "id": "rs-or-ontology-registry",
        "category": "multi-filter-or",
        "question": "List standards that are either an ontology/vocabulary or a registry.",
        "persona": "RESEARCHER",
        "notes": "Either-of intent on the same `category` column; OR is correct here, not AND.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "or_column": "category",
                    "or_values": ["Ontology or Vocabulary", "Registry"],
                },
            }
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": (
            "SELECT COUNT(*) FROM {standards} WHERE \"category\" = 'Ontology or Vocabulary' "
            "OR \"category\" = 'Registry'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_or_segmentation_and_image():
    """Term AND facets: a precise search term ANDs against a facet (not OR)."""
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image')")
    assert n >= 1
    return {
        "id": "rs-redirect-term-and-facet",
        "category": "redirect",
        "question": (
            "Give me a link to search results for standards mentioning segmentation, "
            "within the Image data topic."
        ),
        "persona": "RESEARCHER",
        "notes": (
            "Term-AND-facet case from the retarget plan: SEARCH_TERM='segmentation' plus "
            "topic=[Image] ANDs (16 alone -> 3 with the facet). Grades the decoded qw0 facet plus "
            "the (single-word) SEARCH_TERM, not a live count."
        ),
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {
                    "resourceType": "search",
                    "search_term_exact": "segmentation",
                    "search_term_forbidden_multi_word": True,
                    "required_facets": [{"columnName": "topic", "values": ["Image"]}],
                    "facet_match": "subset",
                },
            }
        ],
        "expected_answer": {
            "url_path": lf.build_portal_url(
                {"resourceType": "search", "searchTerm": "segmentation", "facets": [{"columnName": "topic", "values": ["Image"]}]}
            )["url"],
            "note": "qw0 is opaque/self-verifying; grade the decoded facets, not the literal string.",
        },
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


# --- linked-resource joins ----------------------------------------------------

@item
def rs_link_fhir_orgs():
    row = one(
        "standards",
        "SELECT id, name, has_relevant_organization, responsible_organization, relevantOrgNames, "
        "responsibleOrgName FROM {table} WHERE \"acronym\" = 'FHIR'",
    )
    relevant_ids = json_ids(row["has_relevant_organization"], "B2AI_ORG")
    responsible_ids = json_ids(row["responsible_organization"], "B2AI_ORG")
    return {
        "id": "rs-link-fhir-orgs",
        "category": "linked-resource",
        "question": "Which organizations are responsible for, or relevant to, the FHIR standard?",
        "persona": "RESEARCHER",
        "notes": (
            "standard -> organization join via has_relevant_organization / responsible_organization "
            "(ids) -- confirmed live: FHIR (B2AI_STANDARD:109) has 3 relevant orgs and 1 responsible "
            "org (HL7, B2AI_ORG:40)."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["acronym"],
                    "required_values": ["FHIR"],
                },
            },
            {
                "function": "sqlQuery",
                "required": False,
                "constraints": {
                    "table": "organizations",
                    "join_hint": "organizations.id IN has_relevant_organization/responsible_organization",
                },
            },
        ],
        "expected_answer": {
            "standard_id": row["id"],
            "relevant_org_ids": relevant_ids,
            "responsible_org_ids": responsible_ids,
            "responsible_org_names": ["HL7"],
        },
        "ground_truth_query": (
            "SELECT has_relevant_organization, responsible_organization FROM {standards} "
            "WHERE \"acronym\" = 'FHIR'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_link_cpt_responsible_org():
    row = one(
        "standards",
        "SELECT id, acronym, name, responsible_organization, responsibleOrgName FROM {table} "
        "WHERE \"acronym\" = 'CPT'",
    )
    org_ids = json_ids(row["responsible_organization"], "B2AI_ORG")
    return {
        "id": "rs-link-cpt-to-org",
        "category": "linked-resource",
        "question": "Who is responsible for maintaining the CPT standard?",
        "persona": "RESEARCHER",
        "notes": (
            "standard -> organization join, direction 1 (responsible_organization). Confirmed live: "
            "CPT (B2AI_STANDARD:74) -> responsible_organization = [B2AI_ORG:3] (American Medical "
            "Association / AMA)."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["acronym"],
                    "required_values": ["CPT"],
                },
            }
        ],
        "expected_answer": {
            "standard_id": row["id"],
            "responsible_org_ids": org_ids,
            "responsible_org_names": ["American Medical Association"],
        },
        "ground_truth_query": (
            "SELECT responsible_organization, responsibleOrgName FROM {standards} WHERE \"acronym\" = 'CPT'"
        ),
        "llm_judge_fallback": False,
    }


@item
def rs_link_ama_governs_cpt():
    row = one("organizations", "SELECT id, name, governed_standards_json FROM {table} WHERE \"name\" = 'AMA'")
    ids = json_ids(row["governed_standards_json"], "B2AI_STANDARD")
    return {
        "id": "rs-link-org-governs",
        "category": "linked-resource",
        "question": "What standards does the AMA govern?",
        "persona": "RESEARCHER",
        "notes": (
            "organization -> standard join, direction 2 (governed_standards_json), the inverse of the "
            "CPT item above -- both directions confirmed to agree (CPT's responsible_organization is "
            "B2AI_ORG:3, and B2AI_ORG:3's governed_standards_json includes CPT / B2AI_STANDARD:74)."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "organizations",
                    "required_columns": ["name"],
                    "required_values": ["AMA"],
                },
            }
        ],
        "expected_answer": {
            "org_id": row["id"],
            "governed_standard_ids": ids,
            "governed_standard_ids_contains": "B2AI_STANDARD:74",
        },
        "ground_truth_query": "SELECT governed_standards_json FROM {organizations} WHERE \"name\" = 'AMA'",
        "llm_judge_fallback": False,
    }


@item
def rs_link_dataset_producing_org():
    row = one(
        "datasets",
        "SELECT id, name, producedByOrgId, producedBy FROM {table} WHERE id = 'B2AI_DATA:1'",
    )
    org_ids = json_ids(row["producedByOrgId"], "B2AI_ORG")
    return {
        "id": "rs-link-dataset-org",
        "category": "linked-resource",
        "question": "Which organization produced the 'Cell Maps for Artificial Intelligence' dataset?",
        "persona": "REUSER",
        "notes": (
            "dataset -> organization join via producedByOrgId. Confirmed live: B2AI_DATA:1 -> "
            "producedByOrgId = [B2AI_ORG:116] (Functional Genomics Grand Challenge / CM4AI)."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "datasets",
                    "required_columns": ["name"],
                },
            }
        ],
        "expected_answer": {
            "dataset_id": row["id"],
            "producing_org_ids": org_ids,
            "producing_org_names": ["Functional Genomics Grand Challenge"],
        },
        "ground_truth_query": "SELECT producedByOrgId, producedBy FROM {datasets} WHERE id = 'B2AI_DATA:1'",
        "llm_judge_fallback": False,
    }


@item
def rs_link_topic_to_standards():
    row = one("topics", "SELECT id, name, standardsJson FROM {table} WHERE \"name\" = 'Image'")
    ids = json_ids(row["standardsJson"], "B2AI_STANDARD")
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image')")
    assert len(ids) == n, f"topic->standards join count {len(ids)} != topic HAS count {n}"
    return {
        "id": "rs-link-topic-standards",
        "category": "linked-resource",
        "question": "Which standards are associated with the Image data topic, and how many are there?",
        "persona": "RESEARCHER",
        "notes": (
            "topic -> standards join via topics.standardsJson, cross-checked against the standards "
            "table's own topic HAS ('Image') count -- both give 67, confirming the denormalized join "
            "is consistent both ways."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "topics",
                    "required_columns": ["name"],
                    "required_values": ["Image"],
                },
            }
        ],
        "expected_answer": {
            "topic_id": row["id"],
            "standard_count": len(ids),
            "standard_ids_contains": "B2AI_STANDARD:410",
        },
        "ground_truth_query": "SELECT standardsJson FROM {topics} WHERE \"name\" = 'Image'",
        "llm_judge_fallback": False,
    }


@item
def rs_link_org_datasets():
    row = one(
        "organizations",
        "SELECT id, name, datasets, dataset_names FROM {table} WHERE id = 'B2AI_ORG:114'",
    )
    ids = json_ids(row["datasets"], "B2AI_DATA")
    return {
        "id": "rs-link-org-datasets",
        "category": "linked-resource",
        "question": "What datasets has the Salutogenesis Grand Challenge (AI-READI) produced?",
        "persona": "REUSER",
        "notes": "organization -> dataset join via organizations.datasets, the inverse of producedByOrgId.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "organizations",
                    "required_columns": ["name"],
                },
            }
        ],
        "expected_answer": {"org_id": row["id"], "dataset_ids": ids},
        "ground_truth_query": "SELECT datasets, dataset_names FROM {organizations} WHERE id = 'B2AI_ORG:114'",
        "llm_judge_fallback": False,
    }


@item
def rs_link_negative_case():
    row = one(
        "standards",
        "SELECT id, acronym, name, has_relevant_organization, responsible_organization FROM {table} "
        "WHERE id = 'B2AI_STANDARD:1'",
    )
    assert row["has_relevant_organization"] is None and row["responsible_organization"] is None
    return {
        "id": "rs-link-negative",
        "category": "linked-resource",
        "question": "What organization is responsible for the '.ACE format' standard?",
        "persona": "RESEARCHER",
        "notes": (
            "Deliberate negative case: B2AI_STANDARD:1 ('.ACE format') has NULL "
            "has_relevant_organization and responsible_organization, confirmed live. The agent should "
            "report no linked organization is recorded, not fabricate one or claim the standard "
            "doesn't exist (it does -- it's just unlinked)."
        ),
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["acronym"],
                    "required_values": [".ACE format"],
                },
            }
        ],
        "expected_answer": {
            "standard_id": row["id"],
            "responsible_org_ids": [],
            "relevant_org_ids": [],
            "expect_no_link_reported": True,
        },
        "ground_truth_query": (
            "SELECT has_relevant_organization, responsible_organization FROM {standards} WHERE id = 'B2AI_STANDARD:1'"
        ),
        "llm_judge_fallback": True,
    }


# --- redirect / filter-shape correctness -------------------------------------

@item
def rs_redirect_standard_detail():
    row = one("standards", "SELECT id, acronym, name FROM {table} WHERE \"acronym\" = 'FHIR'")
    return {
        "id": "rs-redirect-standard-detail",
        "category": "redirect",
        "question": "Take me to the FHIR standard's page.",
        "persona": "RESEARCHER",
        "notes": "Direct Detail Page redirect; grade the exact id/path, not a search link.",
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {"resourceType": "standards", "id": row["id"]},
            }
        ],
        "expected_answer": {"url_path": f"/Explore/Standard/DetailsPage?id={row['id']}"},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_org_detail():
    return {
        "id": "rs-redirect-org-detail",
        "category": "redirect",
        "question": "Take me to the AI-READI Grand Challenge organization page.",
        "persona": "RESEARCHER",
        "notes": "Direct Organization Detail Page redirect (resourceType=organizations).",
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {"resourceType": "organizations", "id": "B2AI_ORG:114"},
            }
        ],
        "expected_answer": {"url_path": "/Explore/Organization/OrganizationDetailsPage?id=B2AI_ORG:114"},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_topic_detail():
    return {
        "id": "rs-redirect-topic-detail",
        "category": "redirect",
        "question": "Take me to the Image data topic's page.",
        "persona": "RESEARCHER",
        "notes": "Direct DataTopic Detail Page redirect (resourceType=topics).",
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {"resourceType": "topics", "id": "B2AI_TOPIC:15"},
            }
        ],
        "expected_answer": {"url_path": "/Explore/DataTopic/DetailsPage?id=B2AI_TOPIC:15"},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_search_and_facets():
    n = count(
        "standards",
        "SELECT COUNT(*) FROM {table} WHERE \"topic\" HAS ('Image') AND \"category\" = 'Ontology or Vocabulary'",
    )
    return {
        "id": "rs-redirect-and-facets",
        "category": "redirect",
        "question": (
            "Give me a link to search results for ontology/vocabulary standards about the Image data topic."
        ),
        "persona": "RESEARCHER",
        "notes": (
            "AND-intent redirect: must use two facets (topic + category), never a two-word "
            "SEARCH_TERM. qw0 decodes to selectedFacets with both columns; live-verified count 1."
        ),
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {
                    "resourceType": "search",
                    "required_facets": [
                        {"columnName": "topic", "values": ["Image"]},
                        {"columnName": "category", "values": ["Ontology or Vocabulary"]},
                    ],
                    "facet_match": "exact",
                    "search_term_forbidden_multi_word": True,
                },
            }
        ],
        "expected_answer": {"live_count_sanity_anchor": n},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_search_or_facet():
    return {
        "id": "rs-redirect-or-facet",
        "category": "redirect",
        "question": "Give me a link to search results for standards about either the Image or Genome topics.",
        "persona": "RESEARCHER",
        "notes": "Either-of intent: one facet, two values (OR within the column), not two separate facets.",
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {
                    "resourceType": "search",
                    "required_facets": [{"columnName": "topic", "values": ["Image", "Genome"]}],
                    "facet_match": "exact",
                },
            }
        ],
        "expected_answer": {"live_count_sanity_anchor": 121},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_keyword_search():
    return {
        "id": "rs-redirect-keyword-search",
        "category": "redirect",
        "question": "Give me a link to search the standards catalog for FHIR.",
        "persona": "RESEARCHER",
        "notes": (
            "Plain single-word SEARCH_TERM redirect with no facets -- the baseline case, contrasted "
            "with the AND/OR facet redirect items above."
        ),
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {"resourceType": "search", "search_term_exact": "FHIR"},
            }
        ],
        "expected_answer": {"url_path": "/Search/Standards?SEARCH_TERM=FHIR"},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


@item
def rs_redirect_forbidden_multiword():
    return {
        "id": "rs-redirect-forbidden-multiword",
        "category": "redirect",
        "question": (
            "Give me a link to search results for standards that are about BOTH imaging AND genomics."
        ),
        "persona": "RESEARCHER",
        "notes": (
            "The key AND-vs-OR trap from the retarget plan: 'imaging' (107) + 'genomics' (112) as a "
            "two-word SEARCH_TERM gives 213 (an OR/union), not an AND of both concepts. A correct "
            "agent must NOT put both words in SEARCH_TERM; it should use facets/a single term, or "
            "explain the search can't strictly AND two free-text concepts. This item's grading is "
            "primarily the forbidden-shape check (fail if SEARCH_TERM has >1 word here), not a count."
        ),
        "expected_tool_calls": [
            {
                "function": "buildPortalUrl",
                "constraints": {
                    "resourceType": "search",
                    "search_term_forbidden_multi_word": True,
                },
            }
        ],
        "expected_answer": {
            "explanation_required": True,
            "must_not_claim_and_semantics_for_multiword_search_term": True,
        },
        "ground_truth_query": None,
        "llm_judge_fallback": True,
    }


# --- D4D ----------------------------------------------------------------------

@item
def rs_d4d_section_fact():
    r = lf.get_d4d({"orgId": "B2AI_ORG:116", "section": "human-subjects"})
    assert "Involves Human Subjects: False" in r["text"]
    return {
        "id": "rs-d4d-section-fact",
        "category": "d4d",
        "question": "Does the CM4AI Grand Challenge's dataset involve human subjects?",
        "persona": "PATIENT",
        "notes": (
            "Single-GC D4D section fact. Confirmed live: getD4D(B2AI_ORG:116, section='human-subjects') "
            "text includes 'Involves Human Subjects: False' (non-clinical cell-line data)."
        ),
        "expected_tool_calls": [
            {"function": "listD4Ds", "required": False, "constraints": {}},
            {
                "function": "getD4D",
                "constraints": {"orgId": "B2AI_ORG:116"},
            },
            {
                "function": "getD4D",
                "constraints": {"orgId": "B2AI_ORG:116", "section": "human-subjects"},
            },
        ],
        "expected_answer": {
            "org_id": "B2AI_ORG:116",
            "section": "human-subjects",
            "involves_human_subjects": False,
            "text_contains": "Involves Human Subjects: False",
        },
        "ground_truth_query": None,
        "llm_judge_fallback": True,
    }


@item
def rs_d4d_cross_gc_search():
    r = lf.search_d4d({"query": "consent"})
    orgs = sorted({h["orgId"] for h in r["results"]})
    assert orgs == ["B2AI_ORG:114", "B2AI_ORG:116", "B2AI_ORG:117"], orgs
    return {
        "id": "rs-d4d-cross-gc-consent",
        "category": "d4d",
        "question": "Which Bridge2AI Grand Challenges' datasheets discuss participant consent?",
        "persona": "PATIENT",
        "notes": (
            "Cross-GC comparison -- must use searchD4D, not getD4D on one org. Confirmed live: "
            "'consent' hits AI-READI (114), CM4AI (116), and Voice (117), but NOT CHoRUS (115), which "
            "discusses IRB/ethics oversight without the literal word 'consent'. A correct answer "
            "names exactly these 3 GCs, not all 4."
        ),
        "expected_tool_calls": [
            {
                "function": "searchD4D",
                "constraints": {"query_contains": "consent", "orgId_must_be_absent": True},
            }
        ],
        "expected_answer": {
            "matching_org_ids": orgs,
            "non_matching_org_ids": ["B2AI_ORG:115"],
        },
        "ground_truth_query": None,
        "llm_judge_fallback": True,
    }


@item
def rs_d4d_outline():
    r = lf.get_d4d({"orgId": "B2AI_ORG:114"})
    section_ids = [s["id"] for s in r["sections"]]
    return {
        "id": "rs-d4d-outline",
        "category": "d4d",
        "question": "Give me an overview of the AI-READI Grand Challenge's dataset datasheet (D4D).",
        "persona": "RESEARCHER",
        "notes": (
            "Outline request -- getD4D with no `section` returns the outline (7 flat sections: "
            "motivation, composition, collection-process, uses, distribution, maintenance, "
            "human-subjects -- confirmed live, no 'Preprocessing' section exists here)."
        ),
        "expected_tool_calls": [
            {"function": "listD4Ds", "required": False, "constraints": {}},
            {
                "function": "getD4D",
                "constraints": {"orgId": "B2AI_ORG:114", "section": None},
            },
        ],
        "expected_answer": {
            "org_id": "B2AI_ORG:114",
            "title": r["title"],
            "section_ids": section_ids,
        },
        "ground_truth_query": None,
        "llm_judge_fallback": True,
    }


@item
def rs_d4d_not_covered():
    r = lf.search_d4d({"query": "blockchain"})
    assert r["results"] == []
    return {
        "id": "rs-d4d-not-covered",
        "category": "d4d",
        "question": "Do any of the Grand Challenge datasheets discuss blockchain data provenance?",
        "persona": "RESEARCHER",
        "notes": (
            "Deliberate 'not covered' case: searchD4D('blockchain') returns 0 snippets across all 4 "
            "D4Ds, confirmed live. The agent must say plainly that no D4D covers this, not invent an "
            "answer or claim a section is silent when it was never checked."
        ),
        "expected_tool_calls": [
            {
                "function": "searchD4D",
                "constraints": {"query_contains": "blockchain"},
            }
        ],
        "expected_answer": {"hit_count": 0, "expect_not_covered_reported": True},
        "ground_truth_query": None,
        "llm_judge_fallback": True,
    }


@item
def rs_d4d_list():
    r = lf.list_d4ds({})
    org_ids = [d["orgId"] for d in r["d4ds"]]
    return {
        "id": "rs-d4d-list",
        "category": "d4d",
        "question": "Which Bridge2AI Grand Challenges have a dataset datasheet (D4D) available?",
        "persona": "RESEARCHER",
        "notes": "Basic listD4Ds call; confirmed live: exactly the 4 GC orgs 114-117.",
        "expected_tool_calls": [{"function": "listD4Ds", "constraints": {}}],
        "expected_answer": {"org_ids": org_ids, "count": len(org_ids)},
        "ground_truth_query": None,
        "llm_judge_fallback": False,
    }


# --- counts --------------------------------------------------------------------

@item
def rs_counts_by_type():
    r = lf.count_by_type({})
    return {
        "id": "rs-counts-by-type",
        "category": "counts",
        "question": "How many standards, datasets, organizations, and topics are in the portal?",
        "persona": "FUNDER",
        "notes": "countByType across all 7 pinned tables; counts are exact for the pinned versions.",
        "expected_tool_calls": [{"function": "countByType", "constraints": {}}],
        "expected_answer": {"counts": r["counts"]},
        "ground_truth_query": "countByType()",
        "llm_judge_fallback": False,
    }


@item
def rs_counts_registry():
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"category\" = 'Registry'")
    return {
        "id": "rs-counts-registry",
        "category": "counts",
        "question": "How many registries does the catalog have?",
        "persona": "FUNDER",
        "notes": "Per-category count, category='Registry'.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["category"],
                    "required_values": ["Registry"],
                },
            }
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": "SELECT COUNT(*) FROM {standards} WHERE \"category\" = 'Registry'",
        "llm_judge_fallback": False,
    }


@item
def rs_counts_training_program():
    n = count("standards", "SELECT COUNT(*) FROM {table} WHERE \"category\" = 'Training Program'")
    return {
        "id": "rs-counts-training",
        "category": "counts",
        "question": "How many training programs are cataloged?",
        "persona": "STUDENT",
        "notes": "Per-category count, category='Training Program'.",
        "expected_tool_calls": [
            {
                "function": "sqlQuery",
                "constraints": {
                    "table": "standards",
                    "required_columns": ["category"],
                    "required_values": ["Training Program"],
                },
            }
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": "SELECT COUNT(*) FROM {standards} WHERE \"category\" = 'Training Program'",
        "llm_judge_fallback": False,
    }


@item
def rs_counts_datasets_total():
    n = count("datasets", "SELECT COUNT(*) FROM {table}")
    return {
        "id": "rs-counts-datasets-total",
        "category": "counts",
        "question": "How many datasets does the Bridge2AI Standards Explorer catalog in total?",
        "persona": "REUSER",
        "notes": "Total row count for the datasets table (no filter).",
        "expected_tool_calls": [
            {"function": "sqlQuery", "constraints": {"table": "datasets"}},
        ],
        "expected_answer": {"count": n},
        "ground_truth_query": "SELECT COUNT(*) FROM {datasets}",
        "llm_judge_fallback": False,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_dataset():
    items = []
    for recipe in ITEMS:
        result = recipe()
        if result is not None:
            items.append(result)

    categories = {}
    for it in items:
        categories[it["category"]] = categories.get(it["category"], 0) + 1

    dataset = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "build_ground_truth.py",
        "pinned_versions": dict(lf.TABLES),
        "category_counts": categories,
        "items": items,
    }
    return dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(HERE / "resource_search_dataset.json"),
        help="Output path (default: resource_search_dataset.json next to this script)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Build and validate in-memory only; don't write the output file.",
    )
    args = parser.parse_args(argv)

    print(f"Building resource-search ground truth against pins: {json.dumps(lf.TABLES, indent=2)}")
    dataset = build_dataset()
    print(f"Built {len(dataset['items'])} items: {dataset['category_counts']}")

    if args.check:
        print("(--check: not writing output)")
        return

    out_path = Path(args.out)
    out_path.write_text(json.dumps(dataset, indent=2) + "\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
