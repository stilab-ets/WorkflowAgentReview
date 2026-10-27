"""Prepare RQ3 workflow snapshots and their manifest."""

import ast
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]

# Final RQ1 study sample.
SAMPLE_CSV = ROOT / "datasets/processed/combined_metrics.csv"

# Eligible PRs and their first commits.
AI_PRS_CSV = ROOT / "datasets/processed/ai_eligible_prs.csv"
HUMAN_PRS_CSV = ROOT / "datasets/processed/human_eligible_prs.csv"

# Parent SHAs for first commits.
AI_COMMITS_CSV = ROOT / "datasets/processed/ai_eligible_fetched_commits.csv"
HUMAN_COMMITS_CSV = ROOT / "datasets/processed/human_eligible_commits.csv"

# Changed files and patches for first commits.
AI_DETAILS_CSV = ROOT / "datasets/processed/ai_eligible_commit_details.csv"
HUMAN_DETAILS_CSV = ROOT / "datasets/processed/human_eligible_commit_details.csv"

FILES_DIR = ROOT / "datasets/processed/rq3/diff_files"
OUTPUT_CSV = ROOT / "datasets/processed/rq3/diff_workflow_files.csv"
OUTPUT_FIELDS = (
    "current_commit",
    "workflow_file",
    "parent_commit",
    "old_workflow_file",
    "pr",
    "repo",
    "commit_url",
    "pr_type",
    "file_modification_state",
    "agent",
)
VALID_STATES = {"added", "modified", "removed", "renamed"}


def csv_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as file:
        yield from csv.DictReader(file)


def fetch(url):
    """Fetch a GitHub URL, retrying temporary errors."""

    headers = {"User-Agent": "workflow-change-project"}
    if token := os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=60) as response:
                return response.read()
        except HTTPError as error:
            if error.code == 404:
                raise FileNotFoundError(f"GitHub resource not found: {url}") from error
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def raw_url(repo, sha, filename):
    parts = [quote(part, safe="") for part in filename.replace("\\", "/").split("/")]
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{'/'.join(parts)}"


def local_path(row, side, filename):
    parts = [
        part
        for part in filename.replace("\\", "/").lstrip("/").split("/")
        if part not in ("", ".", "..")
    ]
    return (
        FILES_DIR
        / row["repo"].replace("/", "__")
        / row["current_commit"]
        / side
        / Path(*parts)
    )


def relative(path):
    return path.resolve().relative_to(ROOT).as_posix()


def save_if_missing(path, content):
    """Preserve an existing snapshot; otherwise write the fetched content."""

    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def add_workflow_rows(rows, detail_csv, pr_url_for_row, selected, first_commit_by_pr, parents):
    """Collect supported non-comment workflow changes from a detail CSV."""

    comment_only = 0
    for detail in csv_rows(detail_csv):
        pr_url = pr_url_for_row(detail)
        if not pr_url or pr_url not in selected:
            continue
        if detail["sha"] != first_commit_by_pr[pr_url]:
            continue

        filename = detail["filename"]
        state = detail["status"]
        normalized = filename.replace("\\", "/")
        if (
            not normalized.startswith(".github/workflows/")
            or not normalized.lower().endswith((".yml", ".yaml"))
            or state not in VALID_STATES
        ):
            continue

        # Comment-only patches have no YAML entity change.
        non_comment_change = False
        for line in (detail["patch"] or "").splitlines():
            if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
                continue
            value = line[1:].strip()
            if value and not value.startswith("#"):
                non_comment_change = True
                break
        if not non_comment_change:
            comment_only += 1
            continue

        parent = parents.get((pr_url, detail["sha"]), "")
        if state != "added" and not parent:
            raise ValueError(f"missing parent commit for {pr_url} at {detail['sha']}")

        metadata = selected[pr_url]
        repo = pr_url.split("github.com/", 1)[1].split("/pull/", 1)[0]
        rows.append(
            {
                "current_commit": detail["sha"],
                "parent_commit": parent,
                "pr": pr_url.rsplit("/", 1)[1],
                "repo": repo,
                "commit_url": f"https://github.com/{repo}/commit/{detail['sha']}",
                "pr_type": metadata["pr_type"],
                "agent": metadata["agent"],
                "file_modification_state": state,
                "current_workflow_path": filename,
                "old_workflow_path": filename,
            }
        )
    return comment_only


def row_key(row):
    return row["repo"], row["current_commit"], row["current_workflow_path"]


def main():
    # Commit details contain patches larger than the default CSV field limit.
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            break
        except OverflowError:
            limit //= 10

    # -----------------------------------------------------------------------
    # Step 1: build workflow records from the source datasets.
    print("1/4 build workflow-file dataset")

    # Restrict the eligible PRs to the final study sample.
    selected = {}
    for row in csv_rows(SAMPLE_CSV):
        selected[row["pr_url"]] = {
            "pr_type": row["pr_type"],
            "agent": "human" if row["pr_type"] == "human" else row["agent"],
        }

    # AI details use PR IDs; map them to PR URLs while reading first commits.
    first_commit_by_pr = {}
    ai_pr_urls = {}
    for row in csv_rows(AI_PRS_CSV):
        if row["html_url"] in selected:
            first_commit_by_pr[row["html_url"]] = row["first_commit_sha"]
            ai_pr_urls[row["pr_id"]] = row["html_url"]
    for row in csv_rows(HUMAN_PRS_CSV):
        if row["html_url"] in selected:
            first_commit_by_pr[row["html_url"]] = row["first_commit_sha"]

    missing = sorted(set(selected) - set(first_commit_by_pr))
    if missing:
        raise ValueError(f"missing first commits for {len(missing)} sampled PRs")

    # The first parent identifies the before version.
    parents = {}
    for path in (AI_COMMITS_CSV, HUMAN_COMMITS_CSV):
        for row in csv_rows(path):
            pr_url = row["pr_url"]
            sha = row["sha"]
            if pr_url not in selected or first_commit_by_pr.get(pr_url) != sha:
                continue
            try:
                stored_parents = json.loads(row["parents"])
            except json.JSONDecodeError:
                stored_parents = ast.literal_eval(row["parents"])
            parents[(pr_url, sha)] = (
                stored_parents[0].get("sha", "") if stored_parents else ""
            )

    rows = []
    ai_comment_only = add_workflow_rows(
        rows,
        AI_DETAILS_CSV,
        lambda row: ai_pr_urls.get(row["pr_id"]),
        selected,
        first_commit_by_pr,
        parents,
    )
    human_comment_only = add_workflow_rows(
        rows,
        HUMAN_DETAILS_CSV,
        lambda row: row["pr_url"],
        selected,
        first_commit_by_pr,
        parents,
    )

    record_keys = {
        (row["repo"], row["pr"], row["current_commit"], row["current_workflow_path"])
        for row in rows
    }
    if len(record_keys) != len(rows):
        raise ValueError("duplicate workflow-file records found")
    rows.sort(
        key=lambda row: (
            row["repo"],
            int(row["pr"]),
            row["current_commit"],
            row["current_workflow_path"],
        )
    )
    print(
        f"built {len(rows)} workflow-file records "
        f"({ai_comment_only + human_comment_only} comment-only edits excluded)"
    )

    # -----------------------------------------------------------------------
    # Step 2: fetch missing after versions at the first commit.
    print("2/4 fetch after workflow versions")

    after_content = {}
    existing_after = fetched_after = 0
    for index, row in enumerate(rows, start=1):
        if row["file_modification_state"] != "removed":
            target = local_path(row, "current", row["current_workflow_path"])
            if target.exists():
                existing_after += 1
            else:
                after_content[row_key(row)] = fetch(
                    raw_url(row["repo"], row["current_commit"], row["current_workflow_path"])
                )
                fetched_after += 1
        if index == 1 or index % 100 == 0:
            print(f"checked {index}/{len(rows)} after snapshots")
    print(f"after snapshots: {existing_after} existing, {fetched_after} fetched")

    # -----------------------------------------------------------------------
    # Step 3: save missing before and after snapshots.
    print("3/4 save before/after workflow snapshots")

    # Archived paths can identify a renamed workflow's old filename.
    known_before_paths = {}
    for row in rows:
        if row["file_modification_state"] not in {"added", "renamed"}:
            key = row["repo"], row["current_commit"]
            known_before_paths.setdefault(key, set()).add(row["current_workflow_path"])

    existing_pairs = fetched_pairs = 0
    for index, row in enumerate(rows, start=1):
        state = row["file_modification_state"]

        if state != "removed":
            current = local_path(row, "current", row["current_workflow_path"])
            if current.exists():
                existing_pairs += 1
            elif save_if_missing(current, after_content[row_key(row)]):
                fetched_pairs += 1
            else:
                existing_pairs += 1
            if not current.exists():
                raise FileNotFoundError(f"missing after snapshot: {current}")
            row["workflow_file"] = relative(current)
        else:
            row["workflow_file"] = ""

        if state != "added":
            if state == "renamed":
                before_root = local_path(row, "before", "")
                existing_paths = [
                    path.relative_to(before_root).as_posix()
                    for path in before_root.rglob("*")
                    if path.is_file()
                ]
                candidates = [
                    path
                    for path in existing_paths
                    if path
                    not in known_before_paths.get(
                        (row["repo"], row["current_commit"]), set()
                    )
                ]
                if len(candidates) == 1:
                    row["old_workflow_path"] = candidates[0]
                else:
                    api_url = (
                        f"https://api.github.com/repos/{row['repo']}/commits/"
                        f"{row['current_commit']}"
                    )
                    commit = json.loads(fetch(api_url).decode("utf-8"))
                    for changed_file in commit.get("files", []):
                        if changed_file.get("filename") == row["current_workflow_path"]:
                            previous = changed_file.get("previous_filename")
                            if previous:
                                row["old_workflow_path"] = previous
                                break
                    else:
                        raise ValueError(
                            "cannot find the previous filename for renamed workflow: "
                            f"{row['commit_url']}"
                        )

            before = local_path(row, "before", row["old_workflow_path"])
            if before.exists():
                existing_pairs += 1
            else:
                content = fetch(
                    raw_url(row["repo"], row["parent_commit"], row["old_workflow_path"])
                )
                if save_if_missing(before, content):
                    fetched_pairs += 1
                else:
                    existing_pairs += 1
            if not before.exists():
                raise FileNotFoundError(f"missing before snapshot: {before}")
            row["old_workflow_file"] = relative(before)
        else:
            row["old_workflow_file"] = ""

        if index == 1 or index % 100 == 0:
            print(f"saved {index}/{len(rows)} workflow-file pairs")
    print(f"all snapshots: {existing_pairs} existing, {fetched_pairs} fetched")

    # -----------------------------------------------------------------------
    # Step 4: write the manifest with metadata and local paths.
    print("4/4 build workflow-file manifest")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in OUTPUT_FIELDS})
    print(f"wrote {len(rows)} rows to {relative(OUTPUT_CSV)}")


if __name__ == "__main__":
    main()
