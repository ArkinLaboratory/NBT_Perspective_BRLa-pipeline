"""M4 — Fetch + extract page text.

Global cache: cache/pages/{sha1(url)}.json — shared across ALL technologies,
because technologies in the same domain surface the same reports and
regulator pages repeatedly. Per-technology manifest records which pages
belong to which tech and which dimensions surfaced them.

Failures are recorded in the per-tech manifest (status != "ok") and not
retried within a run, so the batch never stalls on a broken site. Only
deterministic outcomes ("ok"/"no_content") enter the global page cache —
transient failures (`fetch_failed`, `error:*`) are left uncached so the next
run retries them instead of inheriting a dead result forever (I-06).

trafilatura runs FIRST on every URL, because it is the only source of a
publication date (it reads <meta>/JSON-LD/<time>; ~96% coverage on pages it
can reach, and Tavily's API returns no date at all). When its fetch or
extraction fails, M3's staged Tavily text is used instead — recovering the
page's content while keeping whatever date trafilatura managed to scrape.
"""
import time
from urllib.parse import urlparse

import trafilatura

from .utils import (checkpoint_exists, domain_blocked, now_iso, read_json,
                    tech_dir, url_hash, write_json)

_last_hit: dict[str, float] = {}  # host -> last fetch timestamp

MIN_PAGE_CHARS = 200


def _staged_tavily(cfg: dict, url: str) -> dict | None:
    """M3's staged Tavily text for this URL, if it is long enough to use."""
    staged = read_json(cfg["paths"]["tavily_pages_dir"] / f"{url_hash(url)}.json")
    if staged and len(staged.get("text", "")) >= MIN_PAGE_CHARS:
        return staged
    return None


def _page_from_staged(cfg: dict, url: str) -> dict | None:
    """Build a page record from staged Tavily text alone, with no download.

    Used when the per-tech fetch cap is exhausted: the cap exists to bound
    download time, and Tavily already paid for this page during search, so
    there is no reason to discard free evidence. Such a page has no date.

    Caveat: this writes into the page cache, so raising max_pages_per_tech
    later will NOT retroactively give these pages a date without --force.
    """
    staged = _staged_tavily(cfg, url)
    if not staged:
        return None
    record = {"url": url, "fetched_at": now_iso(), "status": "ok",
              "extractor": "tavily_only", "title": staged.get("title", ""),
              "date": None, "text": staged["text"]}
    write_json(cfg["paths"]["pages_dir"] / f"{url_hash(url)}.json", record)
    return record


def _polite_wait(url: str, delay_s: float) -> None:
    host = urlparse(url).netloc
    elapsed = time.time() - _last_hit.get(host, 0)
    if elapsed < delay_s:
        time.sleep(delay_s - elapsed)
    _last_hit[host] = time.time()


CACHEABLE_STATUSES = ("ok", "no_content")


def fetch_page(cfg: dict, url: str, force: bool = False) -> dict:
    """Fetch one URL into the global page cache; return the cache record.

    Only deterministic outcomes are cached — see `CACHEABLE_STATUSES`.
    """
    pages_dir = cfg["paths"]["pages_dir"]
    page_path = pages_dir / f"{url_hash(url)}.json"
    if checkpoint_exists(page_path, force):
        return read_json(page_path)

    _polite_wait(url, cfg["fetch"].get("per_host_delay_s", 1.5))
    record = {"url": url, "fetched_at": now_iso(), "status": "ok",
              "extractor": "trafilatura", "title": "", "date": None, "text": ""}
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded is None:
            record["status"] = "fetch_failed"
        else:
            text = trafilatura.extract(
                downloaded, include_comments=False, include_tables=True,
                favor_recall=True,
            )
            meta = trafilatura.extract_metadata(downloaded)
            record["title"] = (meta.title or "") if meta else ""
            record["date"] = (meta.date or None) if meta else None
            if not text or len(text) < MIN_PAGE_CHARS:
                record["status"] = "no_content"
            else:
                record["text"] = text[:60000]  # hard cap per page
    except Exception as e:  # noqa: BLE001 - the web is hostile
        record["status"] = f"error: {type(e).__name__}"

    # Fallback: trafilatura could not read the page, but Tavily already did.
    # Its date (if any) survives — on `no_content` the HTML was downloaded and
    # metadata parsed successfully, only the body extraction came up empty.
    if record["status"] != "ok":
        staged = _staged_tavily(cfg, url)
        if staged:
            record["fell_back_from"] = record["status"]
            record["status"] = "ok"
            record["extractor"] = "tavily_fallback"
            record["text"] = staged["text"]
            record["title"] = record["title"] or staged.get("title", "")

    # I-06: `fetch_failed` / `error:*` are transient (timeout, 5xx, rate limit).
    # Caching them globally would mark the URL dead for every future run and
    # every other tech citing it, with no way back short of deleting the file.
    if record["status"] in CACHEABLE_STATUSES:
        write_json(page_path, record)
    return record


def run(cfg: dict, tech: dict, force: bool = False) -> dict:
    """Fetch all URLs known for a technology (M3 search hits + M4b DR URLs)."""
    tdir = tech_dir(cfg, tech["tech_id"])
    out_path = tdir / "fetch_manifest.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    blocked = cfg["fetch"].get("blocked_domains", [])
    url_pool: dict[str, dict] = {}
    n_blocked = 0
    search = read_json(tdir / "search_results.json")
    if search:
        for r in search["results"]:
            url_pool[r["url"]] = {"dimensions": r["dimensions"],
                                  "origin": "search"}
    dr = read_json(tdir / "dr_sources.json")
    if dr:
        for u in dr.get("cited_urls", []):
            if u in url_pool:
                url_pool[u]["origin"] = "search+dr"
            else:
                url_pool[u] = {"dimensions": [], "origin": "dr_report"}

    # D-3: DR-report URLs never passed through M3's filter, so apply it here.
    for url in [u for u in url_pool if domain_blocked(u, blocked)]:
        del url_pool[url]
        n_blocked += 1

    if not url_pool:
        raise RuntimeError(
            f"No URLs for {tech['tech_id']}. Run search and/or ingest-dr first."
        )

    max_new = cfg["fetch"].get("max_pages_per_tech", 40)
    manifest, new_fetches = [], 0
    for url, meta in url_pool.items():
        # `force` bypasses the page cache too, not just the tech manifest.
        cached = checkpoint_exists(
            cfg["paths"]["pages_dir"] / f"{url_hash(url)}.json", force)
        if not cached:
            if new_fetches >= max_new:
                page = _page_from_staged(cfg, url)
                if page is None:
                    manifest.append({"url": url, "page_hash": url_hash(url),
                                     "status": "skipped_cap", **meta})
                    continue
                manifest.append({"url": url, "page_hash": url_hash(url),
                                 "status": "ok", "title": page["title"],
                                 "extractor": "tavily_only",
                                 "cache_hit": False, **meta})
                continue
            new_fetches += 1
        page = fetch_page(cfg, url, force)
        manifest.append({"url": url, "page_hash": url_hash(url),
                         "status": page["status"], "title": page.get("title", ""),
                         "extractor": page.get("extractor", "trafilatura"),
                         "cache_hit": cached, **meta})

    result = {
        "tech_id": tech["tech_id"],
        "n_urls": len(manifest),
        "n_ok": sum(1 for m in manifest if m["status"] == "ok"),
        "n_cache_hits": sum(1 for m in manifest if m.get("cache_hit")),
        "n_from_tavily": sum(1 for m in manifest
                             if m.get("extractor", "").startswith("tavily")),
        "n_rescued": sum(1 for m in manifest
                         if m.get("extractor") == "tavily_fallback"),
        "n_blocked_urls": n_blocked,
        "pages": manifest,
        "fetched_at": now_iso(),
    }
    write_json(out_path, result)
    return result
