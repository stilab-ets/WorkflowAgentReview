# Replication Package: GitHub Actions Workflow Changes

This repository contains the data, Jupyter notebooks, scripts, and figures for an empirical study of 522 GitHub Actions workflow pull requests from 101 repositories.

The study compares:

- 264 AI-agent pull requests (APRs)
- 258 human-authored pull requests (HPRs)

## Setup

Python 3.13 is recommended.

The analyses are implemented as Jupyter notebooks. Before running them, install the notebook and analysis packages listed in `requirements.txt`:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\activate
```

The requirements file installs Jupyter and the packages needed for data analysis, statistics, plotting, and GitHub Actions workflow analysis.

## Run the Analysis

Start Jupyter from the project root:

```bash
jupyter notebook
```

Open and run these notebooks in order:

| Research question | Notebook |
|---|---|
| RQ1: Review effort and acceptance | `rq1/6-metrics.ipynb`, then `rq1/7-statistics.ipynb` |
| RQ2: Cross-agent comparison | `rq2/rq2_statistics.ipynb` |
| RQ3: Syntactic workflow changes | `rq3/analysis_syntactic_entities.ipynb` |
| RQ4: Workflow-change motivations | `rq4/thematic_analysis_results_statistics.ipynb` |

To execute all analysis notebooks from the command line:

```bash
jupyter nbconvert --to notebook --execute --inplace \
  rq1/6-metrics.ipynb \
  rq1/7-statistics.ipynb \
  rq2/rq2_statistics.ipynb \
  rq3/analysis_syntactic_entities.ipynb \
  rq4/thematic_analysis_results_statistics.ipynb
```

The earlier notebooks in `rq1/` document the historical data-construction process and are not required to reproduce the final analysis.

## Repository Contents

### Data Collection Scripts

| Script | Generated outputs | Output type |
|---|---|---|
| `data_collection_scripts/fetch-ai-workflow-commits-meta.py` | `datasets/raw/eligible_ai_pop_commits_metadata.jsonl`<br>`datasets/raw/ai_workflow_commits_metadata_summary.json` | Dataset |
| `data_collection_scripts/fetch-eligible-human-prs.py` | `datasets/raw/eligible_human_pop_prs_metadata.jsonl`<br>`datasets/raw/eligible_human_pop_prs_metadata_checkpoint.json`<br>`datasets/raw/eligible_human_pop_prs_metadata_summary.json` | Dataset |
| `data_collection_scripts/fetch-eligible-commits-check-runs.py` | `datasets/raw/eligible_commits_check_runs_input.csv`<br>`datasets/raw/eligible_commits_check_runs.jsonl`<br>`datasets/raw/eligible_commits_check_runs_summary.json` | Dataset |

### RQ1 Notebooks

| Notebook | Generated outputs | Output type |
|---|---|---|
| `rq1/1-extract-ai-workflow-prs.ipynb` | No file output | — |
| `rq1/2-extract-eligible-ai-commit-details.ipynb` | `datasets/processed/fetched_eligible_pop_commits.csv` | Dataset |
| `rq1/3-extract-ai-eligible-prs.ipynb` | `datasets/processed/ai_eligible_prs.csv`<br>`datasets/processed/ai_eligible_fetched_commits.csv`<br>`datasets/processed/ai_eligible_commit_details.csv`<br>`datasets/processed/ai_eligible_reviews.csv`<br>`datasets/processed/ai_eligible_review_comments.csv`<br>`datasets/processed/ai_eligible_comments.csv` | Dataset |
| `rq1/4-extract-eligible-human-prs.ipynb` | `datasets/processed/human_eligible_prs.csv`<br>`datasets/processed/human_eligible_commits.csv`<br>`datasets/processed/human_eligible_commit_details.csv`<br>`datasets/processed/human_eligible_reviews.csv`<br>`datasets/processed/human_eligible_inline_reviews.csv`<br>`datasets/processed/human_eligible_comments.csv` | Dataset |
| `rq1/5-extract-check-runs.ipynb` | `datasets/processed/eligible_pr_failure_rates.csv` | Dataset |
| `rq1/6-metrics.ipynb` | `datasets/processed/rq1/all_eligible_prs.csv`<br>`datasets/processed/rq1/all_eligible_commits.csv`<br>`datasets/processed/combined_metrics.csv` | Dataset |
| `rq1/7-statistics.ipynb` | `datasets/processed/rq1/shapiro_wilk_normality_tests.csv`<br>`datasets/processed/rq1/review_effort_statistics.csv` | Dataset |

### RQ2 Notebook

| Notebook | Generated outputs | Output type |
|---|---|---|
| `rq2/rq2_statistics.ipynb` | `datasets/processed/rq2/agent_median_metrics.csv`<br>`figures/rq2_agent_all_metrics.pdf` | Dataset and figure |

### RQ3 Scripts

| Script | Generated outputs | Output type |
|---|---|---|
| `rq3/get_yaml_diff_files.py` | `datasets/processed/rq3/diff_files/`<br>`datasets/processed/rq3/diff_workflow_files.csv` | Dataset |
| `rq3/syntactic_entities.py` | `datasets/processed/rq3/workflow_syntactic_entities.csv` | Dataset |

### RQ3 Notebook

| Notebook | Generated outputs | Output type |
|---|---|---|
| `rq3/analysis_syntactic_entities.ipynb` | `datasets/processed/rq3/rq3_entity_change_table.csv` | Dataset |

### RQ4 Script

| Script | Generated outputs | Output type |
|---|---|---|
| `rq4/calculate_agreement.py` | `rq4/low_level_label_disagreements.csv` | Dataset |

### RQ4 Notebook

| Notebook | Generated outputs | Output type |
|---|---|---|
| `rq4/thematic_analysis_results_statistics.ipynb` | `figures/rq4/plot_10_high_level_labels_by_pr_type_counts_with_values.png` | Figure |

## Data Note

Use the included processed datasets to reproduce the reported analyses. The historical data-collection scripts are only needed to collect fresh GitHub data; they require a GitHub token, and results may change as GitHub repositories evolve.
