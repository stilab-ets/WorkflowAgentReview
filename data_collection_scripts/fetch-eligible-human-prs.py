import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pandas as pd
from dotenv import load_dotenv

OUTPUT_JSONL_PATH = "../datasets/raw/eligible_human_pop_prs_metadata.jsonl"
OUTPUT_SUMMARY_PATH = "../datasets/raw/eligible_human_pop_prs_metadata_summary.json"
CHECKPOINT_PATH = "../datasets/raw/eligible_human_pop_prs_metadata_checkpoint.json"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = f"{GITHUB_API}/graphql"

GLOBAL_MAX_IN_FLIGHT = 12
PER_TOKEN_MAX_IN_FLIGHT = 4
REPO_MAX_IN_FLIGHT = 4
PER_REPO_MAX_PR_IN_FLIGHT = 8
PER_PR_MAX_COMMIT_DETAILS_IN_FLIGHT = 6
MAX_COMMITS_PER_PR_DETAILS = 250
PR_PROCESS_BATCH_SIZE = 25
CHECKPOINT_EVERY_N_WRITES = 20
RESUME_FROM_OUTPUT = True

FIRST_COMMIT_SHA_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      commits(first: 1) {
        nodes {
          commit {
            oid
          }
        }
      }
    }
  }
}
"""

WORKFLOW_PREFIX = ".github/workflows/"

log = logging.getLogger("fetch_human_prs")


def is_workflow_yaml(path: str) -> bool:
    p = (path or "").lower()
    return p.startswith(WORKFLOW_PREFIX) and (p.endswith(".yml") or p.endswith(".yaml"))


def load_completed_pr_urls_from_jsonl(path: str) -> set:
    completed = set()
    if not RESUME_FROM_OUTPUT or not os.path.exists(path):
        return completed

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pr_url = row.get("pr_url")
            if pr_url:
                completed.add(str(pr_url))
    return completed


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def load_tokens_from_env() -> List[str]:
    tokens: List[str] = []

    for env_name in ("GH_TOKENS", "GITHUB_TOKENS"):
        csv_tokens = os.getenv(env_name, "").strip()
        if csv_tokens:
            tokens.extend([t.strip() for t in csv_tokens.split(",") if t.strip()])

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

    for env_name in ("GH_TOKEN", "GITHUB_TOKEN"):
        t = os.getenv(env_name, "").strip()
        if t:
            tokens.append(t)

    seen = set()
    cleaned = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        if t.lower() in {"your_token_here", "github_token", "gh_token"}:
            continue
        cleaned.append(t)
    return cleaned


def repo_from_url(repo_url: str) -> str:
    parts = repo_url.rstrip("/").split("/")
    return f"{parts[-2]}/{parts[-1]}"


def build_repo_windows_from_csv(
    csv_path: str,
    repo_col: str = "repo_url",
    created_col: str = "created_at",
    pr_url_col: str = "html_url",
) -> Tuple[Dict[str, Tuple[datetime, datetime]], set]:
    df = pd.read_csv(csv_path)
    df = df.dropna(subset=[repo_col, created_col]).copy()
    df[created_col] = pd.to_datetime(df[created_col], utc=True, errors="coerce")
    df = df.dropna(subset=[created_col])
    df["repo_full"] = df[repo_col].astype(str).apply(repo_from_url)

    grouped = df.groupby("repo_full")[created_col].agg(["min", "max"]).reset_index()
    repo_windows = {
        row["repo_full"]: (row["min"], row["max"]) for _, row in grouped.iterrows()
    }
    existing_pr_urls = (
        set(df[pr_url_col].dropna().astype(str)) if pr_url_col in df.columns else set()
    )
    return repo_windows, existing_pr_urls


@dataclass
class TokenState:
    token: str
    sem: asyncio.Semaphore
    blocked_until: float = 0.0


class TokenPool:
    def __init__(self, tokens: List[str]):
        if not tokens:
            raise RuntimeError(
                "No GitHub tokens found. Set GH_TOKEN, GITHUB_TOKEN, GH_TOKENS, or GITHUB_TOKENS."
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
        self.client = httpx.AsyncClient(timeout=60)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, params=None, json_body=None):
        attempt = 0
        while True:
            attempt += 1
            st = await self.pool.next_available()
            async with self.global_sem, st.sem:
                r = await self.client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {st.token}"},
                )

            if r.status_code == 401:
                log.warning(
                    "401 Unauthorized token detected. Removing token and retrying."
                )
                await self.pool.invalidate(st)
                continue

            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset = r.headers.get("X-RateLimit-Reset")
                if reset:
                    st.blocked_until = float(reset)
                    continue

            if r.status_code >= 500:
                await asyncio.sleep(min(10, 0.5 * (2**attempt)))
                continue

            r.raise_for_status()
            return r

    async def list_prs_in_window(
        self, owner: str, repo: str, start_dt: datetime, end_dt: datetime
    ):
        prs_in_window = []
        page = 1
        while True:
            r = await self.request(
                "GET",
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
                params={
                    "state": "all",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": 100,
                    "page": page,
                },
            )
            page_items = r.json()
            if not page_items:
                break

            stop_repo = False
            for pr in page_items:
                created = pd.to_datetime(
                    pr.get("created_at"), utc=True, errors="coerce"
                )
                if pd.isna(created):
                    continue
                if created < start_dt:
                    continue
                if created > end_dt:
                    stop_repo = True
                    break
                prs_in_window.append(pr)

            if stop_repo:
                break
            page += 1

        return prs_in_window

    async def pr_full_bundle(
        self,
        owner: str,
        repo: str,
        number: int,
        preloaded_commit_details: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        async def get(url: str):
            r = await self.request("GET", url)
            return r.json()

        async def get_all_pages(url: str):
            items = []
            page = 1
            while True:
                r = await self.request(
                    "GET", url, params={"per_page": 100, "page": page}
                )
                batch = r.json()
                if not isinstance(batch, list) or not batch:
                    break
                items.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            return items

        async def get_commit_detail(sha: str):
            r = await self.request(
                "GET",
                f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}",
            )
            return r.json()

        pr_task = get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}")
        comments_task = get_all_pages(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments"
        )
        reviews_task = get_all_pages(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/reviews"
        )
        inline_comments_task = get_all_pages(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/comments"
        )
        commits_task = get_all_pages(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/commits"
        )

        pr, comments, reviews, inline_comments, commits = await asyncio.gather(
            pr_task, comments_task, reviews_task, inline_comments_task, commits_task
        )

        shas = [c.get("sha") for c in commits if c.get("sha")]
        shas = shas[:MAX_COMMITS_PER_PR_DETAILS]
        preloaded = preloaded_commit_details or {}
        commit_details: List[Dict[str, Any]] = []
        if shas:
            detail_sem = asyncio.Semaphore(PER_PR_MAX_COMMIT_DETAILS_IN_FLIGHT)

            async def one_detail(sha: str):
                async with detail_sem:
                    try:
                        return await get_commit_detail(sha)
                    except Exception:
                        return None

            missing_shas = [sha for sha in shas if sha not in preloaded]
            fetched = await asyncio.gather(*(one_detail(sha) for sha in missing_shas))

            detail_map: Dict[str, Dict[str, Any]] = {}
            for sha, detail in preloaded.items():
                if isinstance(detail, dict):
                    detail_map[sha] = detail
            for detail in fetched:
                if isinstance(detail, dict) and detail.get("sha"):
                    detail_map[str(detail["sha"])] = detail

            commit_details = [detail_map[sha] for sha in shas if sha in detail_map]

        return {
            "pull_request_metadata": pr,
            "pr": pr,  # backward-compatible alias
            "comments": comments,
            "reviews": reviews,
            "inline_review_comments": inline_comments,
            "commits": commits,
            "commit_details": commit_details,
        }

    async def graphql_first_commit_sha(
        self, owner: str, repo: str, number: int
    ) -> Optional[str]:
        r = await self.request(
            "POST",
            GITHUB_GRAPHQL,
            json_body={
                "query": FIRST_COMMIT_SHA_QUERY,
                "variables": {"owner": owner, "repo": repo, "number": number},
            },
        )
        payload = r.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")

        pr_node = payload.get("data", {}).get("repository", {}).get("pullRequest", {})
        commits_nodes = pr_node.get("commits", {}).get("nodes", [])
        if not commits_nodes:
            return None

        return commits_nodes[0].get("commit", {}).get("oid")

    async def first_commit_workflow_check(
        self, owner: str, repo: str, number: int
    ) -> Tuple[Optional[str], List[str], List[str], Optional[Dict[str, Any]]]:
        """
        Uses GraphQL to get first commit SHA, then REST commit detail to inspect changed files.
        Returns: (first_commit_sha, first_commit_changed_files, first_commit_workflow_files)
        """
        first_sha = await self.graphql_first_commit_sha(owner, repo, number)
        if not first_sha:
            return None, [], [], None

        r = await self.request(
            "GET",
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{first_sha}",
        )
        commit_payload: Dict[str, Any] = r.json()
        changed_files = [
            f.get("filename", "")
            for f in commit_payload.get("files", [])
            if f.get("filename")
        ]
        workflow_files = [p for p in changed_files if is_workflow_yaml(p)]
        return first_sha, changed_files, workflow_files, commit_payload


async def main(csv_path: str) -> None:
    run_started_at_utc = datetime.utcnow().isoformat() + "Z"
    started_at_epoch = time.time()
    setup_logging()
    load_dotenv()

    repo_windows, existing_pr_urls = build_repo_windows_from_csv(csv_path)
    completed_pr_urls = load_completed_pr_urls_from_jsonl(OUTPUT_JSONL_PATH)
    tokens = load_tokens_from_env()
    pool = TokenPool(tokens)
    gh = GitHubClient(pool)

    stats = {
        "repos_total": len(repo_windows),
        "repos_processed": 0,
        "repos_failed": 0,
        "prs_seen": 0,
        "prs_saved": 0,
        "prs_excluded_existing": 0,
        "prs_excluded_checkpoint": 0,
        "checkpoint_loaded_prs": len(completed_pr_urls),
        "prs_no_first_commit": 0,
        "prs_skipped_no_workflow_in_first_commit": 0,
        "pr_bundle_failures": 0,
        "first_commit_check_failures": 0,
        "comments_fetched": 0,
        "reviews_fetched": 0,
        "inline_review_comments_fetched": 0,
        "commits_fetched": 0,
        "commit_details_fetched": 0,
        "checkpoint_writes": 0,
    }
    per_repo_pr_counts: Dict[str, int] = {}
    write_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()
    repo_sem = asyncio.Semaphore(REPO_MAX_IN_FLIGHT)
    records_since_checkpoint = 0

    try:
        mode = "a" if RESUME_FROM_OUTPUT and completed_pr_urls else "w"
        with open(OUTPUT_JSONL_PATH, mode, encoding="utf-8") as writer:

            async def incr_stat(key: str, amount: int = 1) -> None:
                async with stats_lock:
                    stats[key] += amount

            async def maybe_write_checkpoint(force: bool = False) -> None:
                nonlocal records_since_checkpoint
                if not force and records_since_checkpoint < CHECKPOINT_EVERY_N_WRITES:
                    return
                async with stats_lock:
                    snapshot = dict(stats)
                checkpoint_payload = {
                    "updated_at_utc": datetime.utcnow().isoformat() + "Z",
                    "records_written": len(completed_pr_urls),
                    "stats": snapshot,
                    "output_jsonl_path": OUTPUT_JSONL_PATH,
                    "csv_path": csv_path,
                }
                with open(CHECKPOINT_PATH, "w", encoding="utf-8") as cf:
                    json.dump(checkpoint_payload, cf, indent=2)
                records_since_checkpoint = 0
                await incr_stat("checkpoint_writes", 1)

            async def write_record(record: dict) -> None:
                nonlocal records_since_checkpoint
                async with write_lock:
                    writer.write(json.dumps(record, ensure_ascii=False) + "\n")
                    writer.flush()
                    pr_url = record.get("pr_url")
                    if pr_url:
                        completed_pr_urls.add(str(pr_url))
                    records_since_checkpoint += 1
                await maybe_write_checkpoint(force=False)

            async def scan_repo(
                repo_full: str, window: Tuple[datetime, datetime]
            ) -> None:
                async with repo_sem:
                    owner, repo = repo_full.split("/", 1)
                    start_dt, end_dt = window
                    try:
                        prs = await gh.list_prs_in_window(owner, repo, start_dt, end_dt)
                        await incr_stat("repos_processed", 1)
                        await incr_stat("prs_seen", len(prs))
                        per_repo_pr_counts[repo_full] = len(prs)
                        pr_sem = asyncio.Semaphore(PER_REPO_MAX_PR_IN_FLIGHT)

                        async def process_pr(pr: Dict[str, Any]) -> None:
                            async with pr_sem:
                                pr_url = pr.get("html_url")
                                if not pr_url:
                                    return
                                if pr_url in existing_pr_urls:
                                    await incr_stat("prs_excluded_existing", 1)
                                    return
                                if pr_url in completed_pr_urls:
                                    await incr_stat("prs_excluded_checkpoint", 1)
                                    return

                                number = int(pr["number"])
                                try:
                                    (
                                        first_commit_sha,
                                        first_commit_changed_files,
                                        first_commit_workflow_files,
                                        first_commit_detail,
                                    ) = await gh.first_commit_workflow_check(
                                        owner, repo, number
                                    )
                                except Exception:
                                    await incr_stat("first_commit_check_failures", 1)
                                    return

                                if not first_commit_sha:
                                    await incr_stat("prs_no_first_commit", 1)
                                    return

                                if not first_commit_workflow_files:
                                    await incr_stat(
                                        "prs_skipped_no_workflow_in_first_commit", 1
                                    )
                                    return

                                preloaded_details: Dict[str, Dict[str, Any]] = {}
                                if isinstance(first_commit_detail, dict):
                                    preloaded_details[first_commit_sha] = (
                                        first_commit_detail
                                    )

                                try:
                                    bundle = await gh.pr_full_bundle(
                                        owner,
                                        repo,
                                        number,
                                        preloaded_commit_details=preloaded_details,
                                    )
                                except Exception:
                                    await incr_stat("pr_bundle_failures", 1)
                                    return

                                await incr_stat(
                                    "comments_fetched", len(bundle.get("comments", []))
                                )
                                await incr_stat(
                                    "reviews_fetched", len(bundle.get("reviews", []))
                                )
                                await incr_stat(
                                    "inline_review_comments_fetched",
                                    len(bundle.get("inline_review_comments", [])),
                                )
                                await incr_stat(
                                    "commits_fetched", len(bundle.get("commits", []))
                                )
                                await incr_stat(
                                    "commit_details_fetched",
                                    len(bundle.get("commit_details", [])),
                                )

                                record = {
                                    "repo": repo_full,
                                    "repo_window_start": start_dt.isoformat(),
                                    "repo_window_end": end_dt.isoformat(),
                                    "pr_url": pr_url,
                                    "first_commit_sha": first_commit_sha,
                                    "first_commit_changed_files": first_commit_changed_files,
                                    "first_commit_workflow_files": first_commit_workflow_files,
                                    **bundle,
                                }
                                await write_record(record)
                                await incr_stat("prs_saved", 1)

                        for i in range(0, len(prs), PR_PROCESS_BATCH_SIZE):
                            batch = prs[i : i + PR_PROCESS_BATCH_SIZE]
                            await asyncio.gather(*(process_pr(pr) for pr in batch))
                    except Exception:
                        await incr_stat("repos_failed", 1)
                        per_repo_pr_counts[repo_full] = 0

            await asyncio.gather(
                *(
                    scan_repo(repo_full, window)
                    for repo_full, window in repo_windows.items()
                )
            )
            await maybe_write_checkpoint(force=True)
    finally:
        await gh.close()

    run_seconds = round(time.time() - started_at_epoch, 3)
    per_repo_counts = list(per_repo_pr_counts.values())
    summary = {
        **stats,
        "passed_prs_count": stats["prs_saved"],
        "run_started_at_utc": run_started_at_utc,
        "run_seconds": run_seconds,
        "repos_with_no_prs_in_window": sum(1 for c in per_repo_counts if c == 0),
        "prs_per_repo_min": min(per_repo_counts) if per_repo_counts else 0,
        "prs_per_repo_max": max(per_repo_counts) if per_repo_counts else 0,
        "prs_per_repo_avg": (sum(per_repo_counts) / len(per_repo_counts))
        if per_repo_counts
        else 0,
        "csv_path": csv_path,
        "output_jsonl_path": OUTPUT_JSONL_PATH,
    }

    with open(OUTPUT_SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    log.info(
        "DONE. repos_processed=%d prs_saved=%d",
        stats["repos_processed"],
        stats["prs_saved"],
    )


if __name__ == "__main__":
    asyncio.run(main("../datasets/processed/ai_eligible_prs.csv"))
