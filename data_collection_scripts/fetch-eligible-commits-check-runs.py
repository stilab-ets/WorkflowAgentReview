import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd
from dotenv import load_dotenv


GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")
PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/\d+/?$")

GLOBAL_MAX_IN_FLIGHT = 5
PER_TOKEN_MAX_IN_FLIGHT = 1
MAX_TOKENS_TO_USE = 5
MIN_VALID_TOKENS = 1


class CommitNotFoundError(Exception):
    pass


def load_tokens_from_env() -> List[str]:
    tokens: List[str] = []

    for env_name in ("GH_TOKENS", "GITHUB_TOKENS"):
        csv_tokens = os.getenv(env_name, "").strip()
        if csv_tokens:
            tokens.extend([t.strip() for t in csv_tokens.split(",") if t.strip()])

    numbered = []
    for key, value in os.environ.items():
        if key.startswith("GITHUB_TOKEN_") and key[len("GITHUB_TOKEN_") :].isdigit():
            cleaned = value.strip()
            if cleaned:
                numbered.append((int(key[len("GITHUB_TOKEN_") :]), cleaned))
    numbered.sort(key=lambda item: item[0])
    tokens.extend([value for _, value in numbered])

    for env_name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.getenv(env_name, "").strip()
        if token:
            tokens.append(token)

    seen = set()
    cleaned_tokens: List[str] = []
    for token in tokens:
        token = token.strip().strip('"').strip("'")
        if token in seen:
            continue
        seen.add(token)
        if token.lower() in {"your_token_here", "github_token", "gh_token"}:
            continue
        cleaned_tokens.append(token)

    return cleaned_tokens


def build_headers(token: str) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    if GITHUB_API_VERSION:
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
    return headers


async def filter_valid_tokens(tokens: List[str]) -> List[str]:
    if not tokens:
        return []

    valid: List[str] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for idx, token in enumerate(tokens, start=1):
            try:
                response = await client.get(
                    f"{GITHUB_API}/user",
                    headers=build_headers(token),
                )
                if response.status_code == 200:
                    valid.append(token)
                else:
                    print(f"Token #{idx} rejected with status {response.status_code}")
            except Exception as exc:
                print(f"Token #{idx} validation failed: {exc}")
    return valid


@dataclass
class TokenState:
    token: str
    sem: asyncio.Semaphore
    blocked_until: float = 0.0


class TokenPool:
    def __init__(self, tokens: List[str]):
        if not tokens:
            raise RuntimeError(
                "No GitHub token found. Set GH_TOKEN/GITHUB_TOKEN or token lists in .env."
            )
        self.tokens = [
            TokenState(token=token, sem=asyncio.Semaphore(PER_TOKEN_MAX_IN_FLIGHT))
            for token in tokens
        ]
        self.idx = 0
        self.lock = asyncio.Lock()

    async def next_available(self) -> TokenState:
        while True:
            async with self.lock:
                now = time.time()
                if not self.tokens:
                    raise RuntimeError("No usable GitHub tokens remain.")
                for _ in range(len(self.tokens)):
                    state = self.tokens[self.idx]
                    self.idx = (self.idx + 1) % len(self.tokens)
                    if state.blocked_until <= now:
                        return state
            await asyncio.sleep(0.2)

    async def invalidate(self, bad: TokenState) -> None:
        async with self.lock:
            self.tokens = [state for state in self.tokens if state.token != bad.token]
            self.idx = 0 if not self.tokens else self.idx % len(self.tokens)


class GitHubClient:
    def __init__(self, pool: TokenPool):
        self.pool = pool
        self.global_sem = asyncio.Semaphore(GLOBAL_MAX_IN_FLIGHT)
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, object]] = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                state = await self.pool.next_available()
            except RuntimeError as exc:
                raise RuntimeError(
                    "No usable GitHub tokens remain during fetch."
                ) from exc

            async with self.global_sem, state.sem:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    headers=build_headers(state.token),
                )

            if response.status_code == 404:
                raise CommitNotFoundError(f"404 Not Found for URL: {url}")

            if response.status_code == 401:
                print("401 Unauthorized token. Removing token and retrying.")
                await self.pool.invalidate(state)
                continue

            if response.status_code in (403, 429):
                txt = response.text.lower()
                remaining = response.headers.get("X-RateLimit-Remaining", "")
                is_rate_limit = (remaining == "0") or ("rate limit" in txt)
                if is_rate_limit:
                    reset = response.headers.get("X-RateLimit-Reset")
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        state.blocked_until = time.time() + int(retry_after)
                    else:
                        state.blocked_until = (
                            float(reset) if reset else (time.time() + 15)
                        )
                    continue

            if response.status_code in (500, 502, 503, 504):
                await asyncio.sleep(min(8, 0.5 * (2**attempt)))
                continue

            response.raise_for_status()
            return response

    async def get_commit_check_runs(self, owner: str, repo: str, sha: str) -> dict:
        base_url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}/check-runs"
        page = 1
        merged: Optional[dict] = None

        while True:
            response = await self.request(
                "GET",
                base_url,
                params={"per_page": 100, "page": page, "filter": "all"},
            )
            data = response.json()
            page_runs = data.get("check_runs", [])

            if merged is None:
                merged = {
                    key: value for key, value in data.items() if key != "check_runs"
                }
                merged["check_runs"] = []

            merged["check_runs"].extend(page_runs)

            if not page_runs or len(page_runs) < 100:
                break
            page += 1

        if merged is None:
            return {"total_count": 0, "check_runs": []}
        return merged


def clean_str(value) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def parse_owner_repo_from_pr_url(
    pr_url: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    url = clean_str(pr_url)
    if not url:
        return None, None
    match = PR_URL_RE.match(url)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def write_combined_input_csv(rows: List[dict], output_csv: str) -> None:
    out_path = Path(output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {"commit": row["commit"], "pr": row["pr"], "pr_type": row["pr_type"]}
            for row in rows
        ]
    )
    df.to_csv(out_path, index=False)


def load_source_rows(input_csv: str, pr_type: str) -> Tuple[List[dict], dict]:
    csv_path = Path(input_csv)
    df = pd.read_csv(csv_path)
    required = {"sha", "pr_url"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV must contain columns {sorted(required)}: {csv_path}")

    rows: List[dict] = []
    stats = {
        "source_csv": str(csv_path),
        "pr_type": pr_type,
        "rows_total": int(len(df)),
        "rows_with_required_fields": 0,
        "rows_valid": 0,
        "skipped_missing_required_fields": 0,
        "skipped_invalid_pr_url_rows": 0,
        "skipped_duplicate_rows": 0,
    }

    seen = set()
    subset = df[["sha", "pr_url"]].dropna(subset=["sha", "pr_url"])
    stats["rows_with_required_fields"] = int(len(subset))
    stats["skipped_missing_required_fields"] = int(len(df) - len(subset))

    for row in subset.itertuples(index=False):
        commit = clean_str(row.sha)
        pr = clean_str(row.pr_url)
        if not commit or not pr:
            continue

        owner, repo = parse_owner_repo_from_pr_url(pr)
        if not owner or not repo:
            stats["skipped_invalid_pr_url_rows"] += 1
            continue

        dedupe_key = (commit, pr, pr_type)
        if dedupe_key in seen:
            stats["skipped_duplicate_rows"] += 1
            continue
        seen.add(dedupe_key)

        rows.append(
            {
                "commit": commit,
                "pr": pr,
                "pr_type": pr_type,
                "owner": owner,
                "repo": repo,
            }
        )

    stats["rows_valid"] = int(len(rows))
    return rows, stats


def prepare_combined_rows(
    ai_input_csv: str, human_input_csv: str, combined_output_csv: str
):
    ai_rows, ai_stats = load_source_rows(ai_input_csv, "ai")
    human_rows, human_stats = load_source_rows(human_input_csv, "human")

    rows = ai_rows + human_rows
    write_combined_input_csv(rows, combined_output_csv)

    prep_stats = {
        "combined_output_csv": combined_output_csv,
        "input_rows_total": int(ai_stats["rows_total"] + human_stats["rows_total"]),
        "valid_rows_total": int(len(rows)),
        "unique_commits": int(
            len({(r["owner"], r["repo"], r["commit"]) for r in rows})
        ),
        "unique_prs": int(len({r["pr"] for r in rows})),
        "sources": {
            "ai": ai_stats,
            "human": human_stats,
        },
    }
    return rows, prep_stats


async def fetch_unique_commit_check_runs(
    gh: GitHubClient, unique_keys: List[Tuple[str, str, str]]
):
    details_map: Dict[Tuple[str, str, str], dict] = {}
    not_found_keys: List[Tuple[str, str, str]] = []

    async def fetch_one(key: Tuple[str, str, str]):
        owner, repo, commit = key
        attempt = 0
        while True:
            attempt += 1
            try:
                print(f"Fetching check runs for {owner}/{repo}@{commit}")
                details_map[key] = await gh.get_commit_check_runs(owner, repo, commit)
                return
            except CommitNotFoundError:
                print(f"404 commit not found for check runs: {owner}/{repo}@{commit}")
                not_found_keys.append(key)
                return
            except RuntimeError as exc:
                raise RuntimeError("All tokens became unusable during fetch.") from exc
            except Exception as exc:
                print(
                    f"Retry check runs for {owner}/{repo}@{commit} "
                    f"(attempt {attempt}): {exc}"
                )
                await asyncio.sleep(min(10, 0.5 * (2 ** min(attempt, 6))))

    await asyncio.gather(*(fetch_one(key) for key in unique_keys))
    return details_map, not_found_keys


def write_results(rows: List[dict], details_map: Dict, output_path: str):
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written_rows = 0
    rows_with_missing_check_runs = 0
    total_check_runs_fetched = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            key = (row["owner"], row["repo"], row["commit"])
            check_runs = details_map.get(key)
            if check_runs is None:
                rows_with_missing_check_runs += 1
            else:
                total_check_runs_fetched += len(check_runs.get("check_runs", []))

            payload = {
                "commit": row["commit"],
                "pr": row["pr"],
                "pr_type": row["pr_type"],
                "check_runs": check_runs,
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            written_rows += 1

    return written_rows, rows_with_missing_check_runs, total_check_runs_fetched


@dataclass
class Args:
    ai_input_csv: str
    human_input_csv: str
    combined_output_csv: str
    output_jsonl: str
    summary_json: Optional[str]
    max_tokens_to_use: int
    min_valid_tokens: int


def parse_args(base_dir: Path) -> Args:
    parser = argparse.ArgumentParser(
        description="Combine AI and human eligible commits and fetch GitHub check runs."
    )
    parser.add_argument(
        "--ai-input-csv",
        default=str(base_dir / "datasets" / "processed" / "fetched_eligible_pop_commits.csv"),
        help="AI commit source CSV.",
    )
    parser.add_argument(
        "--human-input-csv",
        default=str(base_dir / "datasets" / "processed" / "human_eligible_commits.csv"),
        help="Human commit source CSV.",
    )
    parser.add_argument(
        "--combined-output-csv",
        default=str(base_dir / "datasets" / "raw" / "eligible_commits_check_runs_input.csv"),
        help="Normalized combined input CSV written before fetching.",
    )
    parser.add_argument(
        "--output-jsonl",
        default=str(base_dir / "datasets" / "raw" / "eligible_commits_check_runs.jsonl"),
        help="Where to write fetched check-run rows.",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Optional explicit summary JSON path.",
    )
    parser.add_argument(
        "--max-tokens-to-use",
        type=int,
        default=MAX_TOKENS_TO_USE,
        help="Maximum number of valid GitHub tokens to use concurrently.",
    )
    parser.add_argument(
        "--min-valid-tokens",
        type=int,
        default=MIN_VALID_TOKENS,
        help="Minimum number of valid tokens required before fetching starts.",
    )
    parsed = parser.parse_args()

    if parsed.max_tokens_to_use < 1:
        parser.error("--max-tokens-to-use must be >= 1")
    if parsed.min_valid_tokens < 1:
        parser.error("--min-valid-tokens must be >= 1")

    return Args(
        ai_input_csv=parsed.ai_input_csv,
        human_input_csv=parsed.human_input_csv,
        combined_output_csv=parsed.combined_output_csv,
        output_jsonl=parsed.output_jsonl,
        summary_json=parsed.summary_json,
        max_tokens_to_use=parsed.max_tokens_to_use,
        min_valid_tokens=parsed.min_valid_tokens,
    )


async def run_pipeline(args: Args):
    started_at = time.time()
    run_started_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    rows, prep_stats = prepare_combined_rows(
        ai_input_csv=args.ai_input_csv,
        human_input_csv=args.human_input_csv,
        combined_output_csv=args.combined_output_csv,
    )
    unique_keys = list({(row["owner"], row["repo"], row["commit"]) for row in rows})

    print(f"Prepared {len(rows)} combined rows")
    print(f"Unique commits to fetch check runs for: {len(unique_keys)}")
    print(f"Wrote normalized combined input CSV to {args.combined_output_csv}")

    raw_tokens = load_tokens_from_env()
    if not raw_tokens:
        raise RuntimeError("No tokens found in env/.env.")

    valid_tokens = await filter_valid_tokens(raw_tokens)
    print(f"Tokens loaded: {len(raw_tokens)}, valid: {len(valid_tokens)}")
    if len(valid_tokens) < args.min_valid_tokens:
        raise RuntimeError(
            f"Need at least {args.min_valid_tokens} valid token(s), "
            f"found {len(valid_tokens)}."
        )

    active_tokens = valid_tokens[: min(len(valid_tokens), args.max_tokens_to_use)]
    print(f"Using {len(active_tokens)} token(s) for fetching")

    pool = TokenPool(active_tokens)
    gh = GitHubClient(pool)
    try:
        details_map, not_found_keys = await fetch_unique_commit_check_runs(
            gh, unique_keys
        )
    finally:
        await gh.close()

    written_rows, rows_with_missing_check_runs, total_check_runs_fetched = (
        write_results(
            rows,
            details_map,
            args.output_jsonl,
        )
    )
    print(f"Saved {written_rows} rows to {args.output_jsonl}")

    summary_path = (
        Path(args.summary_json)
        if args.summary_json
        else Path(args.output_jsonl).with_name(
            "eligible_commits_check_runs_summary.json"
        )
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    fetch_success_rate = (
        (len(details_map) / len(unique_keys) * 100) if unique_keys else 0.0
    )
    summary = {
        "run_started_at_utc": run_started_at_utc,
        "run_seconds": round(time.time() - started_at, 3),
        "ai_input_csv": args.ai_input_csv,
        "human_input_csv": args.human_input_csv,
        "combined_output_csv": args.combined_output_csv,
        "output_jsonl": args.output_jsonl,
        "summary_json": str(summary_path),
        "tokens_loaded": int(len(raw_tokens)),
        "tokens_valid": int(len(valid_tokens)),
        "tokens_used": int(len(active_tokens)),
        "max_tokens_to_use": int(args.max_tokens_to_use),
        "min_valid_tokens": int(args.min_valid_tokens),
        "concurrency_limit": int(GLOBAL_MAX_IN_FLIGHT),
        "combined_rows": int(len(rows)),
        "unique_commits": int(len(unique_keys)),
        "fetched_commits_unique": int(len(details_map)),
        "not_found_commits_unique": int(len(not_found_keys)),
        "failed_commits_unique": int(
            len(unique_keys) - len(details_map) - len(not_found_keys)
        ),
        "output_rows_written": int(written_rows),
        "output_rows_with_missing_check_runs": int(rows_with_missing_check_runs),
        "total_check_runs_fetched": int(total_check_runs_fetched),
        "fetch_success_rate_unique_commits_pct": round(fetch_success_rate, 3),
        **prep_stats,
    }
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(f"Saved summary to {summary_path}")


if __name__ == "__main__":
    load_dotenv()
    base_dir = Path(__file__).resolve().parent.parent
    cli_args = parse_args(base_dir)
    asyncio.run(run_pipeline(cli_args))
