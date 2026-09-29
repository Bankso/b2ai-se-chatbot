#!/usr/bin/env python3
"""Upload benchmark log/result files to Synapse for provenance.

Generic uploader for gitignored log or result files (e.g. redteam
transcripts, which can contain content an attack successfully extracted
from the agent, or other benchmark outputs) that still need a durable,
permissioned home so a report citing their numbers stays independently
reproducible.

All eval results share one Synapse parent project, with a subfolder per eval
type (`--folder`) so results stay organized as more benchmarks start
uploading — e.g. `redteam`, `kb-routing`, `general-help`. The subfolder is
created under `--parent-id` if it doesn't already exist.

No B2AI eval-results Synapse project has been designated yet — DEFAULT_PARENT_ID
below is a placeholder. Pass a real project ID via `--parent-id` until one is
chosen (the NF Portal Copilot's equivalent project is syn76878333, for
reference on the expected layout — not reusable for B2AI's own results).

Requires a Synapse account with upload access to that project and login
credentials available to synapseclient (~/.synapseConfig or the
SYNAPSE_AUTH_TOKEN environment variable).

Usage:
    # redteam runs -> <parent-id>/redteam
    python upload_logs_to_synapse.py --dir benchmark/redteam --folder redteam \\
        --parent-id syn00000000 \\
        --pattern 'redteam_eval_results_*.json' --pattern 'redteam_aggregate_results.json'

    # explicit files -> <parent-id>/kb-routing
    python upload_logs_to_synapse.py --folder kb-routing --parent-id syn00000000 \\
        --file benchmark/kb-routing/routing_eval_results_20260620T001142Z.json

    python upload_logs_to_synapse.py --dir benchmark/redteam --folder redteam \\
        --parent-id syn00000000 --pattern 'redteam_eval_results_*.json' --dry-run
"""

from __future__ import annotations

import argparse
from pathlib import Path

import synapseclient
from synapseclient import File, Folder

DEFAULT_PARENT_ID = "REPLACE_ME_B2AI_EVAL_RESULTS_PROJECT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dir", default=".", help="Directory to search for files matching --pattern (default: cwd)"
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=None,
        help="Glob pattern (relative to --dir) to match files; repeatable.",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=None,
        help="Explicit file path to upload; repeatable. Combines with --pattern.",
    )
    parser.add_argument(
        "--parent-id",
        default=DEFAULT_PARENT_ID,
        help=(
            "Synapse project all eval results live under (default: "
            f"{DEFAULT_PARENT_ID} — a placeholder; no B2AI project is designated yet)"
        ),
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Eval-type subfolder name under --parent-id, e.g. 'redteam', 'kb-routing'. Created if missing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the files that would be uploaded without uploading them",
    )
    return parser.parse_args()


def find_files(directory: Path, patterns: list[str] | None, explicit: list[str] | None) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns or []:
        files.extend(sorted(directory.glob(pattern)))
    for f in explicit or []:
        path = Path(f)
        if not path.exists():
            raise SystemExit(f"File not found: {path}")
        files.append(path)
    if not files:
        raise SystemExit("No files to upload — pass --pattern and/or --file")
    return files


def get_or_create_folder(syn: synapseclient.Synapse, name: str, parent_id: str) -> str:
    existing = syn.findEntityId(name, parent=parent_id)
    if existing:
        return existing
    return syn.store(Folder(name=name, parent=parent_id)).id


def main() -> None:
    args = parse_args()
    files = find_files(Path(args.dir), args.pattern, args.file)

    print(f"{len(files)} file(s) to upload to {args.parent_id}/{args.folder}:")
    for f in files:
        print(f"  {f}")
    if args.dry_run:
        print("\n--dry-run: nothing uploaded")
        return

    syn = synapseclient.login()
    folder_id = get_or_create_folder(syn, args.folder, args.parent_id)
    print(f"\nUsing folder {args.folder} ({folder_id})")
    for f in files:
        entity = syn.store(File(str(f), parent=folder_id))
        print(f"  {f.name} -> {entity.id} (version {entity.versionNumber})")


if __name__ == "__main__":
    main()
