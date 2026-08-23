"""M3 — Web search via Tavily.

One checkpoint per technology: cache/{tech}/search_results.json
Results are deduplicated by URL across dimensions; each URL remembers which
dimensions' queries surfaced it (a market report may serve MRL *and* ORL).

Tavily downloads each page while searching whether we ask for the text or
not, so `include_raw_content` costs nothing extra. We stage that text in a
GLOBAL fallback cache (D-1). M4 still fetches with trafilatura first — that is
what supplies publication dates, which Tavily does not return — and reaches
for the staged text only when its own fetch fails.
"""
import os
import time

import requests

from .llm import record_tavily_query
from .m2_queries import build_queries
from .utils import (checkpoint_exists, domain_blocked, now_iso, read_json,
                    tech_dir, url_hash, write_json)

TAVILY_URL = "https://api.tavily.com/search"

MIN_PAGE_CHARS = 200  # D-2: below this the text is too thin to be worth staging


def _stage_tavily_page(cfg: dict, res: dict) -> bool:
    """Stage Tavily's raw_content in the global fallback cache (D-1).

    This is NOT the page cache M5 reads — it is raw material M4 falls back on
    when trafilatura cannot fetch or extract the page. No `date` field: the
    search API returns no publication date (result keys are title/url/content/
    score/raw_content), which is precisely why trafilatura still runs first.

    Returns True only when a new record was staged.
    """
    text = (res.get("raw_content") or "").strip()
    if len(text) < MIN_PAGE_CHARS:
        return False
    path = cfg["paths"]["tavily_pages_dir"] / f"{url_hash(res['url'])}.json"
    if path.exists():
        return False
    write_json(path, {
        "url": res["url"],
        "staged_at": now_iso(),
        "title": res.get("title", ""),
        "text": text[:60000],  # same hard cap M4 applies
    })
    return True


def _tavily_search(api_key: str, query: str, cfg_search: dict,
                   exclude_domains: list[str] | None = None) -> list[dict] | None:
    """Run one Tavily query.

    Returns the (possibly empty) result list when the API answered, and None
    when every attempt failed at the transport level — a broken key, a network
    error, or exhausted rate-limit retries. Callers must keep the two apart:
    "searched and found nothing" is a real answer, "never reached the API" is
    not, and only the former is safe to checkpoint (I-07).
    """
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": cfg_search.get("search_depth", "basic"),
        "max_results": cfg_search.get("max_results_per_query", 6),
        "include_raw_content": True,
        # Filter server-side so blocked hosts never consume a result slot.
        "exclude_domains": exclude_domains or [],
    }
    for attempt in range(3):
        try:
            r = requests.post(TAVILY_URL, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("results", [])
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return None


def run(cfg: dict, tech: dict, aliases: dict, force: bool = False) -> dict:
    out_path = tech_dir(cfg, tech["tech_id"]) / "search_results.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    api_key = os.environ.get(cfg["search"]["api_key_env"], "")
    if not api_key:
        raise RuntimeError(
            f"{cfg['search']['api_key_env']} not set. Get a free key at tavily.com, "
            "or rely on Deep Research reports only (run.py ingest-dr)."
        )

    queries = build_queries(tech["name"], aliases, cfg["dimensions"])
    blocked = cfg["fetch"].get("blocked_domains", [])
    by_url: dict[str, dict] = {}
    n_queries = n_blocked = n_staged = 0
    n_ok = n_failed = 0
    search_depth = cfg["search"].get("search_depth", "basic")
    for dim, qs in queries.items():
        for q in qs:
            n_queries += 1
            record_tavily_query(search_depth)
            hits = _tavily_search(api_key, q, cfg["search"], blocked)
            if hits is None:  # I-07: transport failure, not empty results
                n_failed += 1
                time.sleep(0.3)
                continue
            n_ok += 1
            for res in hits:
                url = res.get("url", "")
                if not url:
                    continue
                if domain_blocked(url, blocked):  # D-3
                    n_blocked += 1
                    continue
                if url not in by_url:
                    by_url[url] = {
                        "url": url,
                        "title": res.get("title", ""),
                        "tavily_snippet": res.get("content", "")[:500],
                        "dimensions": [],
                        "queries": [],
                    }
                    if _stage_tavily_page(cfg, res):
                        n_staged += 1
                if dim not in by_url[url]["dimensions"]:
                    by_url[url]["dimensions"].append(dim)
                by_url[url]["queries"].append(q)
            time.sleep(0.3)  # gentle on the free tier

    # I-07: a checkpoint written after a total wipe-out would look exactly like
    # "searched, found nothing" and, since checkpoint_exists gates on file
    # presence alone, would mask the failure until someone passed --force.
    if n_ok == 0 and n_failed > 0:
        raise RuntimeError(
            f"All {n_failed} Tavily queries failed for "
            f"{tech['tech_id']} — check ${cfg['search']['api_key_env']} and "
            "network connectivity. Not checkpointing."
        )

    result = {
        "tech_id": tech["tech_id"],
        "n_queries": n_queries,
        "n_failed_queries": n_failed,
        "n_unique_urls": len(by_url),
        "n_blocked_urls": n_blocked,
        "n_staged_pages": n_staged,
        "results": list(by_url.values()),
        "searched_at": now_iso(),
    }
    write_json(out_path, result)
    return result
