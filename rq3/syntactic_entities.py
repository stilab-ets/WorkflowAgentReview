"""Extract normalized RQ3 syntactic changes with GAWD.

Read modified workflow pairs from ``diff_workflow_files.csv`` and write one
row for each retained YAML operation to ``workflow_syntactic_entities.csv``.
"""

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT_CSV = ROOT / "datasets/processed/rq3/diff_workflow_files.csv"
OUTPUT_CSV = ROOT / "datasets/processed/rq3/workflow_syntactic_entities.csv"

# Use UTF-8 when GAWD reads workflow snapshots.
if not sys.flags.utf8_mode:
    raise SystemExit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

# Fallback for the legacy package environment.
try:
    from gawd import diff_workflow_files
except ModuleNotFoundError:
    sys.path.append(str(ROOT / ".gha-workflows/Lib/site-packages"))
    from gawd import diff_workflow_files


def full_path(path):
    path = Path(path)
    if path.is_absolute():
        return str(path)
    return str(ROOT / path)


def workflow_name(row):
    """Get the repository-relative filename from an archived path."""
    path = row["workflow_file"] or row["old_workflow_file"]
    path = path.replace("\\", "/")
    for marker in ("/current/", "/before/"):
        if marker in path:
            return path.split(marker, 1)[1]
    return Path(path).name


def change_state(kind):
    """Keep the RQ3 operation states and rename ``changed`` to ``modified``."""
    if kind == "added":
        return "added"
    if kind == "removed":
        return "removed"
    if kind == "changed":
        return "modified"
    return None


def generic_token(token, placeholder):
    suffix = token[token.index("[") :] if "[" in token else ""
    return placeholder + suffix


def replace_child_key(parts, parent, placeholder, skip=()):
    if parent not in parts:
        return
    child_index = parts.index(parent) + 1
    if child_index >= len(parts):
        return
    child = parts[child_index].split("[", 1)[0]
    if child not in skip:
        parts[child_index] = generic_token(parts[child_index], placeholder)


def normalize_path(path):
    """Replace repository-specific job, environment, and list identifiers."""
    parts = path.split(".")

    # Job IDs and environment variable names differ across repositories.
    if len(parts) > 1 and parts[0] == "jobs":
        parts[1] = "<job_id>"

    replace_child_key(parts, "env", "<env_name>")

    # Replace numeric list positions after normalizing named identifiers.
    return ".".join(
        re.sub(r"\[\d+\]", "[<index>]", "<Number>" if part.isdigit() else part)
        for part in parts
    )


def keyword_level(path):
    return len(path.split(".")) if path else 0


def step_kind(keyword, value):
    """Classify complete steps as action or shell-command steps."""
    if keyword != "jobs.<job_id>.steps[<index>]" or not isinstance(value, dict):
        return ""
    if "uses" in value:
        return "action"
    if "run" in value:
        return "run"
    return ""


def build_rows(input_csv):
    """Return one normalized row for each retained GAWD operation."""
    rows = []

    with input_csv.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            # RQ3 compares files with both before and after versions.
            if row["file_modification_state"] != "modified":
                continue

            old_file = full_path(row["old_workflow_file"])
            new_file = full_path(row["workflow_file"])

            try:
                diffs = diff_workflow_files(old_file, new_file)
            except Exception as error:
                print(f"skip {workflow_name(row)}: {error}")
                continue

            for kind, old_path, old_value, new_path, new_value in diffs:
                state = change_state(kind)
                if not state:
                    continue

                keyword = normalize_path(new_path or old_path)
                if not keyword:
                    continue

                out = {
                    "repo": row["repo"],
                    "pr": row["pr"],
                    "commit_url": row["commit_url"],
                    "workflow_file_name": workflow_name(row),
                    "file_modification_state": row["file_modification_state"],
                    "pr_type": row["pr_type"],
                    "agent": row["agent"],
                    "keyword": keyword,
                    "keyword_level": keyword_level(keyword),
                    "keyword_change_state": state,
                    "step_kind": step_kind(
                        keyword, new_value if new_value is not None else old_value
                    ),
                }

                # Keep occurrences; the notebook groups them later.
                rows.append(out)

    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Extract normalized RQ3 syntactic changes with GAWD."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=INPUT_CSV,
        help=f"before/after workflow manifest (default: {INPUT_CSV})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_CSV,
        help=f"syntactic-change CSV to write (default: {OUTPUT_CSV})",
    )
    args = parser.parse_args()

    rows = build_rows(args.input.resolve())

    fields = [
        "repo",
        "pr",
        "commit_url",
        "workflow_file_name",
        "file_modification_state",
        "pr_type",
        "agent",
        "keyword",
        "keyword_level",
        "keyword_change_state",
        "step_kind",
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.resolve().open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"saved {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
