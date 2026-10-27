# Replication Package: GitHub Actions Workflow Changes

## 1. Installation

### 1.1 Install packages

Use Python 3.13. Run from the repository root:

```bash
python -m pip install -r requirements.txt
```

### 1.2 Required AIDev dataset

Use the study's AIDev-Pop snapshot from [AIDev](https://huggingface.co/datasets/hao-li/AIDev/blob/main/README.md). Place the seven Parquet files listed below in `datasets/aidev/`. The exact upstream revision used by this study is not recorded; the latest release may differ. The notebooks expect the study filename `pr_review_comments_v2.parquet`.

Large Parquet, JSONL, and processed CSV files are excluded from Git and must be obtained separately.

### 1.3 Generate processed data

Create the directories and start Jupyter:

```bash
mkdir -p datasets/aidev datasets/raw datasets/processed/rq1 datasets/processed/rq2 datasets/processed/rq3 figures/rq4
jupyter notebook
```

Follow one execution flow in Section 4. RQ1 notebooks 2–6 generate the processed CSVs; AIDev files alone are insufficient because the notebooks also read GitHub JSONL data and archived intermediate files.

## 2. Datasets

**AIDev inputs — `datasets/aidev/`**

| File | Description |
|---|---|
| `pull_request.parquet` | AI-agent pull-request metadata. |
| `pr_commits.parquet` | Commits associated with pull requests. |
| `pr_commit_details.parquet` | Changed files and patches for each commit. |
| `pr_reviews.parquet` | Pull-request reviews. |
| `pr_review_comments_v2.parquet` | Inline review comments. |
| `pr_comments.parquet` | Pull-request discussion comments. |
| `issue.parquet` | GitHub issue records. |

**Collected raw data — `datasets/raw/`**

| File | Description |
|---|---|
| `eligible_ai_pop_commits_metadata.jsonl` | GitHub metadata for the selected AI commits. |
| `eligible_human_pop_prs_metadata.jsonl` | Human PR metadata, commits, changed files, reviews, and comments. |
| `eligible_commits_check_runs.jsonl` | CI check runs for AI and human commits. |

**Processed data — paths relative to `datasets/processed/`**

| Files | Description |
|---|---|
| `fetched_eligible_pop_commits.csv` | Fetched AI commit records; also the input list for AI commit recollection. |
| `ai_eligible_prs.csv`, `human_eligible_prs.csv` | Eligible pull-request metadata. |
| `ai_eligible_fetched_commits.csv`, `human_eligible_commits.csv` | Commits belonging to eligible PRs. |
| `ai_eligible_commit_details.csv`, `human_eligible_commit_details.csv` | File-level commit changes. |
| `ai_eligible_reviews.csv`, `human_eligible_reviews.csv` | Review records. |
| `ai_eligible_review_comments.csv`, `human_eligible_inline_reviews.csv` | Inline review comments. |
| `ai_eligible_comments.csv`, `human_eligible_comments.csv` | Discussion comments. |
| `eligible_pr_failure_rates.csv` | PR-level CI failure metrics. |
| `rq1/all_eligible_prs.csv`, `rq1/all_eligible_commits.csv` | Sampled study PRs and their commits. |
| `combined_metrics.csv` | Review-effort and acceptance metrics for the study sample. |
| `rq1/shapiro_wilk_normality_tests.csv`, `rq1/review_effort_statistics.csv` | RQ1 normality tests and statistical summaries. |
| `rq2/agent_median_metrics.csv` | Per-agent metric summaries. |
| `rq3/diff_workflow_files.csv` | Manifest of workflow changes and before/after file paths. |
| `rq3/diff_files/` | Archived workflow YAML snapshots, included in Git. |
| `rq3/workflow_syntactic_entities.csv` | Syntactic entity changes extracted with GAWD. |
| `rq3/rq3_entity_change_table.csv` | Grouped workflow-entity change frequencies. |

**Other inputs included in Git**

| Files | Description |
|---|---|
| `data_collection_scripts/outputs/repos_with_status_code.json` | Historical repository availability checks used by RQ1 notebook 1. |
| `rq4/annotation/rater1.csv`, `rq4/annotation/rater2.csv` | Independent annotation records. |
| `rq4/annotation/disagreements.csv` | Stored annotation disagreements. |
| `rq4/annotation/final_dataset_with_agreed_labels.csv` | Agreed labels used by the RQ4 analysis. |

## 3. Notebooks

| Notebook | Description |
|---|---|
| `rq1/1-extract-ai-workflow-prs.ipynb` | Identifies workflow-changing AI PR candidates; does not save a dataset. |
| `rq1/2-extract-eligible-ai-commit-details.ipynb` | Converts fetched AI commit metadata into `fetched_eligible_pop_commits.csv`. |
| `rq1/3-extract-ai-eligible-prs.ipynb` | Selects eligible AI PRs and exports their commits, file changes, reviews, and comments. |
| `rq1/4-extract-eligible-human-prs.ipynb` | Filters collected human PRs and exports the corresponding datasets. |
| `rq1/5-extract-check-runs.ipynb` | Calculates CI failure metrics for the supplied PR list. |
| `rq1/6-metrics.ipynb` | Samples human PRs, combines both groups, and calculates review-effort and acceptance metrics. |
| `rq1/7-statistics.ipynb` | Produces the RQ1 statistical comparisons. |
| `rq2/rq2_statistics.ipynb` | Compares metrics across AI agents. |
| `rq3/analysis_syntactic_entities.ipynb` | Summarizes workflow-entity changes by PR type and change operation. |
| `rq4/thematic_analysis_results_statistics.ipynb` | Summarizes annotated workflow-change motivations. |

## 4. Usage: Execution Flow

### 4.1 Raw data recollection

Copy `.env.example` to `.env` in the repository root. The AI commit collector requires five distinct valid tokens; the human-PR and check-run collectors accept one token.

Start with the AIDev tables and archived `datasets/processed/fetched_eligible_pop_commits.csv` and `datasets/processed/rq1/all_eligible_prs.csv`. These intermediate files are required to start the current workflow.

1. Collect AI commit metadata:

   ```bash
   python data_collection_scripts/fetch-ai-workflow-commits-meta.py
   ```

2. Run RQ1 notebook 2, then notebook 3 through its CSV-export cells. Stop before **Eligible builds**, which reads check-run data collected in step 5.
3. Collect human PR metadata:

   ```bash
   cd data_collection_scripts
   python fetch-eligible-human-prs.py
   cd ..
   ```

4. Run RQ1 notebook 4 through its CSV-export cells, stopping before **Eligible builds**.
5. Collect check runs using the AI commit list and newly exported human commits:

   ```bash
   python data_collection_scripts/fetch-eligible-commits-check-runs.py
   ```

6. Continue with Section 4.2. The human collector resumes existing output; use a separate copy without its old JSONL file for a fully fresh collection. GitHub data can change between collections.

To refresh the RQ3 workflow manifest, run `python rq3/get_yaml_diff_files.py` after RQ1 notebook 6 and before RQ3 entity extraction. This downloader reads `GITHUB_TOKEN` from the process environment, not `.env`.

### 4.2 Using existing raw data

Requires the seven AIDev files, three raw JSONL files, and archived `datasets/processed/rq1/all_eligible_prs.csv`. Notebook 5 reads this PR list before notebook 6 regenerates it.

1. Run all cells in RQ1 notebooks **2 → 3 → 4 → 5 → 6 → 7**. Notebook 1 is optional inspection.
2. Run `rq2/rq2_statistics.ipynb` using the generated `combined_metrics.csv`.
3. With `datasets/processed/rq3/diff_workflow_files.csv` and its archived YAML files present, extract RQ3 entities:

   ```bash
   python rq3/syntactic_entities.py
   ```

   Then run `rq3/analysis_syntactic_entities.ipynb`.
4. Run `rq4/thematic_analysis_results_statistics.ipynb` using the included agreed-label dataset.

This flow uses local inputs and needs no GitHub token. Generated tables are saved under `datasets/processed/`; figures are saved under `figures/`.
