#!/usr/bin/env python3
"""
Calculate Cohen's kappa and raw agreement rate for low-level PR labels.

Defaults are set for the two CSV files in this folder. Rows are matched by
`pr_url`. Metrics use final low-level labels and exclude missing labels and
entries explicitly marked NEEDS_REVIEW in either file.
Use --preserved-ratings to reproduce the final-code agreement before the
remaining blank final labels were copied between raters.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_AUTHOR1 = str(SCRIPT_DIR / "rater1.csv")
DEFAULT_AUTHOR2 = str(SCRIPT_DIR / "rater2.csv")
DEFAULT_OUTPUT = str(SCRIPT_DIR / "low_level_label_disagreements.csv")
PRESERVED_RATINGS = SCRIPT_DIR / "kappa_ratings_before_fill.csv"


def normalize_label(value: str | None) -> str:
    """Normalize labels for comparison while keeping originals for output."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def read_csv_by_pr_url(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: dict[str, dict[str, str]] = {}
        for row_number, row in enumerate(reader, start=2):
            pr_url = (row.get("pr_url") or "").strip()
            if not pr_url and all(not (value or "").strip() for value in row.values()):
                continue
            if not pr_url:
                raise ValueError(f"{path}: missing pr_url on CSV row {row_number}")
            if pr_url in rows:
                raise ValueError(f"{path}: duplicate pr_url on CSV row {row_number}: {pr_url}")
            rows[pr_url] = row
    return rows


def cohen_kappa(author1_labels: list[str], author2_labels: list[str]) -> tuple[float, float, float]:
    if len(author1_labels) != len(author2_labels):
        raise ValueError("Label lists must have the same length")
    total = len(author1_labels)
    if total == 0:
        raise ValueError("No matched labels to compare")

    observed = sum(a == b for a, b in zip(author1_labels, author2_labels)) / total
    author1_counts = Counter(author1_labels)
    author2_counts = Counter(author2_labels)
    labels = set(author1_counts) | set(author2_counts)
    expected = sum((author1_counts[label] / total) * (author2_counts[label] / total) for label in labels)

    if expected == 1.0:
        kappa = 1.0 if observed == 1.0 else 0.0
    else:
        kappa = (observed - expected) / (1.0 - expected)

    return kappa, observed, expected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate Cohen's kappa and agreement rate for low-level labels."
    )
    parser.add_argument("--author1", default=DEFAULT_AUTHOR1, help="CSV file for author 1")
    parser.add_argument("--author2", default=DEFAULT_AUTHOR2, help="CSV file for author 2")
    parser.add_argument("--author1-label", default="final_low_level", help="Author 1 low-level label column")
    parser.add_argument("--author2-label", default="final_low_level", help="Author 2 low-level label column")
    parser.add_argument("--author1-title", default="pr_title", help="Author 1 PR title column")
    parser.add_argument("--author2-title", default="pr_title", help="Author 2 PR title column")
    parser.add_argument("--author1-description", default="pr_description", help="Author 1 PR description column")
    parser.add_argument("--author2-description", default="pr_description", help="Author 2 PR description column")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Disagreement CSV output path")
    parser.add_argument(
        "--preserved-ratings",
        action="store_true",
        help="Use frozen pre-fill final ratings to reproduce the pre-fill kappa",
    )
    args = parser.parse_args()

    if args.preserved_ratings:
        if (
            args.author1 != DEFAULT_AUTHOR1
            or args.author2 != DEFAULT_AUTHOR2
            or args.author1_label != "final_low_level"
            or args.author2_label != "final_low_level"
        ):
            parser.error("--preserved-ratings cannot be combined with author-file or label-column overrides")
        args.author1 = str(PRESERVED_RATINGS)
        args.author2 = str(PRESERVED_RATINGS)
        args.author1_label = "rater1_final_low_level"
        args.author2_label = "rater2_final_low_level"

    author1_path = Path(args.author1)
    author2_path = Path(args.author2)
    output_path = Path(args.output)

    author1_rows = read_csv_by_pr_url(author1_path)
    author2_rows = read_csv_by_pr_url(author2_path)

    matched_urls = sorted(set(author1_rows) & set(author2_rows))
    author1_only = sorted(set(author1_rows) - set(author2_rows))
    author2_only = sorted(set(author2_rows) - set(author1_rows))

    author1_labels: list[str] = []
    author2_labels: list[str] = []
    disagreements: list[dict[str, str]] = []
    unlabeled_urls: list[str] = []

    for pr_url in matched_urls:
        row1 = author1_rows[pr_url]
        row2 = author2_rows[pr_url]
        label1_original = row1.get(args.author1_label, "")
        label2_original = row2.get(args.author2_label, "")
        label1 = normalize_label(label1_original)
        label2 = normalize_label(label2_original)

        if (
            not label1
            or not label2
            or label1.startswith("needs_review:")
            or label2.startswith("needs_review:")
        ):
            unlabeled_urls.append(pr_url)
            continue

        author1_labels.append(label1)
        author2_labels.append(label2)

        if label1 != label2:
            disagreements.append(
                {
                    "pr_url": pr_url,
                    "pr_title": row1.get(args.author1_title) or row2.get(args.author2_title, ""),
                    "pr_description": row1.get(args.author1_description)
                    or row2.get(args.author2_description, ""),
                    "author_1_low_level_label": label1_original,
                    "author_2_low_level_label": label2_original,
                }
            )

    kappa, agreement_rate, expected_agreement = cohen_kappa(author1_labels, author2_labels)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "pr_url",
            "pr_title",
            "pr_description",
            "author_1_low_level_label",
            "author_2_low_level_label",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(disagreements)

    print(f"Ratings source: {'preserved pre-fill final codes' if args.preserved_ratings else 'current final codes'}")
    print(f"Author 1 file: {author1_path}")
    print(f"Author 2 file: {author2_path}")
    print(f"PRs present in both files: {len(matched_urls)}")
    print(f"Matched PRs used for metrics: {len(author1_labels)}")
    print(f"Agreements: {len(author1_labels) - len(disagreements)}")
    print(f"Disagreements: {len(disagreements)}")
    print(f"Agreement rate: {agreement_rate:.4f} ({agreement_rate:.2%})")
    print(f"Expected agreement by chance: {expected_agreement:.4f} ({expected_agreement:.2%})")
    print(f"Cohen's kappa: {kappa:.4f}")
    print(f"Disagreement CSV: {output_path}")

    if unlabeled_urls:
        print(f"Skipped {len(unlabeled_urls)} PR(s) with a missing or unresolved label in either file:")
        for pr_url in unlabeled_urls:
            print(f"  {pr_url}")

    if author1_only:
        print(f"Warning: {len(author1_only)} PR(s) only in author 1 file:")
        for pr_url in author1_only:
            print(f"  {pr_url}")
    if author2_only:
        print(f"Warning: {len(author2_only)} PR(s) only in author 2 file:")
        for pr_url in author2_only:
            print(f"  {pr_url}")


if __name__ == "__main__":
    main()
