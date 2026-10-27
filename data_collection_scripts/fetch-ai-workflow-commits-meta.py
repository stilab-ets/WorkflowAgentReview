import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import httpx
import pandas as pd
from dotenv import load_dotenv


GITHUB_API = "https://api.github.com"
PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/\d+/?$")

GLOBAL_MAX_IN_FLIGHT = 5
PER_TOKEN_MAX_IN_FLIGHT = 1
REQUIRED_VALID_TOKENS = 5


class CommitNotFoundError(Exception):
    pass


def load_tokens_from_env() -> List[str]:
    tokens: List[str] = []

    # GH_TOKENS / GITHUB_TOKENS = comma-separated list
    for env_name in ("GH_TOKENS", "GITHUB_TOKENS"):
        csv_tokens = os.getenv(env_name, "").strip()
        if csv_tokens:
            tokens.extend([t.strip() for t in csv_tokens.split(",") if t.strip()])

    # GITHUB_TOKEN_1, GITHUB_TOKEN_2, ...
    numbered = []
    for k, v in os.environ.items():
        if (
            k.startswith("GITHUB_TOKEN_")
            and k[len("GITHUB_TOKEN_") :].isdigit()
            and v.strip()
        ):
            numbered.append((int(k[len("GITHUB_TOKEN_") :]), v.strip()))
    numbered.sort(key=lambda x: x[0])
    tokens.extend([v for _, v in numbered])

    # single token fallback
    for env_name in ("GH_TOKEN", "GITHUB_TOKEN"):
        t = os.getenv(env_name, "").strip()
        if t:
            tokens.append(t)

    # dedupe + cleanup placeholders
    seen = set()
    cleaned: List[str] = []
    for t in tokens:
        t = t.strip().strip('"').strip("'")
        if t in seen:
            continue
        seen.add(t)
        if t.lower() in {"your_token_here", "github_token", "gh_token"}:
            continue
        cleaned.append(t)

    return cleaned


async def filter_valid_tokens(tokens: List[str]) -> List[str]:
    if not tokens:
        return []

    valid: List[str] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for idx, token in enumerate(tokens, start=1):
            try:
                r = await client.get(
                    f"{GITHUB_API}/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if r.status_code == 200:
                    valid.append(token)
                else:
                    print(f"Token #{idx} rejected with status {r.status_code}")
            except Exception as e:
                print(f"Token #{idx} validation failed: {e}")
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
            TokenState(token=t, sem=asyncio.Semaphore(PER_TOKEN_MAX_IN_FLIGHT))
            for t in tokens
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
                    st = self.tokens[self.idx]
                    self.idx = (self.idx + 1) % len(self.tokens)
                    if st.blocked_until <= now:
                        return st
            await asyncio.sleep(0.2)

    async def invalidate(self, bad: TokenState) -> None:
        async with self.lock:
            self.tokens = [t for t in self.tokens if t.token != bad.token]
            self.idx = 0 if not self.tokens else self.idx % len(self.tokens)


class GitHubClient:
    def __init__(self, pool: TokenPool):
        self.pool = pool
        self.global_sem = asyncio.Semaphore(GLOBAL_MAX_IN_FLIGHT)
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str):
        attempt = 0
        while True:
            attempt += 1
            try:
                st = await self.pool.next_available()
            except RuntimeError as e:
                raise RuntimeError(
                    "No usable GitHub tokens remain during fetch."
                ) from e

            async with self.global_sem, st.sem:
                r = await self.client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {st.token}",
                        "Accept": "application/vnd.github+json",
                    },
                )

            if r.status_code == 404:
                raise CommitNotFoundError(f"404 Not Found for URL: {url}")

            if r.status_code == 401:
                print("401 Unauthorized token. Removing token and retrying.")
                await self.pool.invalidate(st)
                continue

            if r.status_code in (403, 429):
                txt = r.text.lower()
                remaining = r.headers.get("X-RateLimit-Remaining", "")
                is_rate_limit = (remaining == "0") or ("rate limit" in txt)
                if is_rate_limit:
                    reset = r.headers.get("X-RateLimit-Reset")
                    st.blocked_until = float(reset) if reset else (time.time() + 15)
                    continue

            if r.status_code in (500, 502, 503, 504):
                await asyncio.sleep(min(8, 0.5 * (2**attempt)))
                continue

            r.raise_for_status()
            return r

    async def get_commit(self, owner: str, repo: str, sha: str) -> dict:
        base_url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}"
        r = await self.request("GET", base_url)
        data = r.json()

        # GitHub paginates files for very large commits
        if "files" in data and len(data["files"]) == 300:
            page = 2
            while True:
                rr = await self.request("GET", f"{base_url}?per_page=100&page={page}")
                page_data = rr.json()
                files = page_data.get("files", [])
                if not files:
                    break
                data["files"].extend(files)
                page += 1

        return data


def parse_owner_repo_from_pr_url(pr_url: str):
    m = PR_URL_RE.match(str(pr_url).strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def prepare_rows(csv_path: str):
    df = pd.read_csv(csv_path)
    required = {"sha", "pr_url"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV must contain columns: {sorted(required)}")

    rows = []
    skipped_invalid_url = 0
    subset = df[["sha", "pr_url"]].dropna(subset=["sha", "pr_url"])
    for row in subset.itertuples(index=False):
        sha = str(row.sha).strip()
        if not sha:
            continue

        pr_url = str(row.pr_url).strip()
        owner, repo = parse_owner_repo_from_pr_url(pr_url)
        if owner is None:
            print(f"Skipping invalid PR url: {pr_url}")
            skipped_invalid_url += 1
            continue
        rows.append(
            {
                "sha": sha,
                "pr_url": pr_url,
                "owner": owner,
                "repo": repo,
            }
        )
    prep_stats = {
        "input_rows_total": int(len(df)),
        "rows_after_required_fields": int(len(subset)),
        "skipped_invalid_pr_url_rows": int(skipped_invalid_url),
        "valid_rows": int(len(rows)),
    }
    return rows, prep_stats


async def fetch_unique_commit_details(
    gh: GitHubClient, unique_keys: List[Tuple[str, str, str]]
):
    details_map: Dict[Tuple[str, str, str], dict] = {}
    not_found_keys: List[Tuple[str, str, str]] = []

    async def fetch_one(key: Tuple[str, str, str]):
        owner, repo, sha = key
        attempt = 0
        while True:
            attempt += 1
            try:
                print(f"Fetching {owner}/{repo}@{sha}")
                details_map[key] = await gh.get_commit(owner, repo, sha)
                return
            except CommitNotFoundError:
                print(f"404 commit not found: {owner}/{repo}@{sha}")
                not_found_keys.append(key)
                return
            except RuntimeError as e:
                raise RuntimeError("All tokens became unusable during fetch.") from e
            except Exception as e:
                # keep retrying with other token(s); do not skip this commit
                print(f"Retry {owner}/{repo}@{sha} (attempt {attempt}): {e}")
                await asyncio.sleep(min(10, 0.5 * (2 ** min(attempt, 6))))

    await asyncio.gather(*(fetch_one(k) for k in unique_keys))
    return details_map, not_found_keys


def write_results(
    rows, details_map: Dict[Tuple[str, str, str], dict], output_path: str
):
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written_rows = 0
    skipped_missing_details = 0
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            key = (row["owner"], row["repo"], row["sha"])
            if key not in details_map:
                skipped_missing_details += 1
                continue
            out = {
                "sha": row["sha"],
                "pr_url": row["pr_url"],
                "commit_details": details_map[key],
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            written_rows += 1
    return written_rows, skipped_missing_details


async def run_pipeline(input_csv: str, output_jsonl: str):
    started_at = time.time()
    run_started_at_utc = datetime.utcnow().isoformat() + "Z"

    rows, prep_stats = prepare_rows(input_csv)
    unique_keys = list({(r["owner"], r["repo"], r["sha"]) for r in rows})

    print(f"Prepared {len(rows)} valid rows")
    print(f"Unique commits to fetch: {len(unique_keys)}")

    raw_tokens = load_tokens_from_env()
    if not raw_tokens:
        raise RuntimeError("No tokens found in env/.env.")

    valid_tokens = await filter_valid_tokens(raw_tokens)
    print(f"Tokens loaded: {len(raw_tokens)}, valid: {len(valid_tokens)}")
    if len(valid_tokens) < REQUIRED_VALID_TOKENS:
        raise RuntimeError(
            f"Need at least {REQUIRED_VALID_TOKENS} valid tokens, found {len(valid_tokens)}."
        )

    active_tokens = valid_tokens[:REQUIRED_VALID_TOKENS]
    pool = TokenPool(active_tokens)
    gh = GitHubClient(pool)
    try:
        details, not_found_keys = await fetch_unique_commit_details(gh, unique_keys)
    finally:
        await gh.close()

    written_rows, skipped_missing_details = write_results(rows, details, output_jsonl)
    print(f"Saved {len(details)} commit detail objects to {output_jsonl}")

    summary_path = str(
        Path(output_jsonl).with_name("ai_workflow_commits_metadata_summary.json")
    )
    fetch_success_rate = (len(details) / len(unique_keys) * 100) if unique_keys else 0.0
    summary = {
        "run_started_at_utc": run_started_at_utc,
        "run_seconds": round(time.time() - started_at, 3),
        "input_csv": input_csv,
        "output_jsonl": output_jsonl,
        "summary_json": summary_path,
        "tokens_loaded": int(len(raw_tokens)),
        "tokens_valid": int(len(valid_tokens)),
        "tokens_used": int(len(active_tokens)),
        "concurrency_limit": int(GLOBAL_MAX_IN_FLIGHT),
        # requested metrics
        "existing_commits_unique": int(len(unique_keys)),
        "existing_pr_commit_rows": int(len(rows)),
        "fetched_commits_unique": int(len(details)),
        "fetched_pr_commit_rows": int(written_rows),
        # useful extra stats
        "not_found_commits_unique": int(len(not_found_keys)),
        "failed_commits_unique": int(
            len(unique_keys) - len(details) - len(not_found_keys)
        ),
        "rows_skipped_missing_commit_details": int(skipped_missing_details),
        "fetch_success_rate_unique_commits_pct": round(fetch_success_rate, 3),
        "unique_repos_in_valid_rows": int(
            len({f"{r['owner']}/{r['repo']}" for r in rows})
        ),
        **prep_stats,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"Saved summary to {summary_path}")


if __name__ == "__main__":
    load_dotenv()
    base_dir = Path(__file__).resolve().parent.parent
    input_csv = str(
        base_dir / "datasets" / "processed" / "fetched_eligible_pop_commits.csv"
    )
    output_jsonl = str(
        base_dir / "datasets" / "raw" / "eligible_ai_pop_commits_metadata.jsonl"
    )

    asyncio.run(run_pipeline(input_csv, output_jsonl))
