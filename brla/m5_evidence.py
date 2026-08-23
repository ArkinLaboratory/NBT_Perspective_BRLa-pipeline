"""M5 — Evidence extraction.

For each successfully fetched page, one cheap-model LLM call extracts zero or
more structured evidence records tagged with BRLa dimensions and bin signals.
The alias list is provided in-context so the model recognizes the technology
under alternate names.
"""

from urllib.parse import urlparse

from . import llm
from .utils import (checkpoint_exists, normalize_bin_signal, now_iso,
                    read_json, tech_dir, write_json)

SYSTEM = """\
You are an evidence extractor for a Balanced Readiness Level assessment (BRLa).

Given a web page about a technology, extract concrete evidence relevant to one
or more of these five readiness dimensions:

- TRL (Technology Readiness): How developed is the technology?
  Low = idea/lab research, Mid = prototype/field trials, High = proven in operational use.
- MRL (Market Readiness): How ready is the market/business model?
  Low = theoretical market, Mid = pilots/early sales, High = selling at scale.
  Also extract as MRL: indirect market mechanisms such as carbon-credit programs,
  supply-chain sustainability mandates, government incentive/subsidy programs, or
  third-party funding models that drive adoption without direct product sales.
- RRL (Regulatory Readiness): Are legal/regulatory conditions in place?
  Low = legality unknown/blocked, Mid = permits obtainable with effort, High = approved.
- ARL (Acceptance Readiness): Will society/the sector accept it?
  Low = active rejection/controversy, Mid = skepticism, High = broadly accepted.
  Also extract as ARL: industry endorsements, extension service recommendations,
  voluntary adoption rates, consumer purchasing behavior, and government support
  programs. These signal acceptance even without formal opinion surveys.
- ORL (Organizational Readiness): Does it fit users' existing work practices?
  Low = fundamental workflow break, Mid = significant adaptation needed, High = seamless fit.
  Also extract as ORL: descriptions of how the technology physically slots into
  existing operations (e.g. "mixed into feed rations", "plugs into standard grid
  connections", "replaces existing equipment with no retraining"). These are ORL
  signals even when the page is primarily about technology performance or markets.

Rules:
1. Extract only CONCRETE claims backed by specifics in the text (dates,
   numbers, names, events).  Skip vague or generic marketing language.
2. Each snippet must be a verbatim or tight paraphrase from the text, <=60 words.
3. A single page may yield records for multiple dimensions, or zero if nothing
   is relevant.
4. bin_signal: for each dimension you tag, give your best judgment of which bin
   (Low / Mid / High) this evidence supports, or null if the snippet is
   relevant but does not clearly point to a bin.
5. source_type: one of  news | company | regulator | academic | market_report | forum
6. source_quality:  high (peer-reviewed research, government/regulatory
   report, independent field trial with data) | med (trade press,
   company announcement, market report with methodology, secondary
   synthesis) | low (opinion, forum, boilerplate content, undated page
   with no named author)

Respond with a single JSON object:
{
  "records": [
    {
      "snippet": "...",
      "dimensions": ["TRL", "MRL"],
      "bin_signal": {"TRL": "High", "MRL": null},
      "source_type": "news",
      "source_quality": "high"
    }
  ]
}

If the page contains no relevant evidence, return {"records": []}."""


def _build_user_prompt(tech_name, aliases, page_title, page_text,
                       hint_dimensions):
    lines = [f"Technology: {tech_name}"]
    if aliases:
        lines.append(f"Also known as: {', '.join(aliases)}")
    if page_title:
        lines.append(f"Page title: {page_title}")
    if hint_dimensions:
        lines.append(
            f"Dimensions whose queries surfaced this URL: "
            f"{', '.join(hint_dimensions)}"
        )
    lines += ["", "--- PAGE TEXT ---", page_text[:50000]]
    return "\n".join(lines)


def _collect_pages(cfg, manifest):
    """Return list of page dicts to process (successfully fetched pages)."""
    pages = []

    for entry in manifest["pages"]:
        if entry["status"] != "ok":
            continue
        ph = entry["page_hash"]
        page_path = cfg["paths"]["pages_dir"] / f"{ph}.json"
        page = read_json(page_path)
        if not page or not page.get("text"):
            continue
        pages.append({
            "url": entry["url"],
            "page_hash": ph,
            "title": page.get("title", ""),
            "text": page["text"],
            "date": page.get("date"),
            "dimensions": entry.get("dimensions", []),
            "extractor": entry.get("extractor", ""),
        })

    return pages


def _page_quality(page: dict) -> float:
    """Score a page by pre-extraction credibility signals."""
    score = 0.0
    ext = page.get("extractor", "")
    if ext == "trafilatura":
        score += 3
    elif ext == "tavily_fallback":
        score += 2
    else:
        score += 1
    if page.get("date") is not None:
        score += 1
    domain = urlparse(page.get("url", "")).netloc.lower()
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    if tld in ("gov", "edu") or domain.endswith(".gov") or domain.endswith(".edu"):
        score += 2
    elif tld == "org" or domain.endswith(".org"):
        score += 1
    score += len(page.get("dimensions") or []) * 0.5
    return score


def _select_pages(pages: list[dict], max_pages: int,
                  min_per_dim: int = 5) -> list[dict]:
    """Coverage-aware page selection.

    Phase 1: guarantee each dimension has at least min_per_dim pages by
    round-robin picking the best-quality page for the most-starved dimension.
    Phase 2: fill remaining slots by overall quality score.
    """
    dims = ["TRL", "MRL", "RRL", "ARL", "ORL"]
    selected_idx: set[int] = set()
    dim_counts = {d: 0 for d in dims}
    scores = [_page_quality(p) for p in pages]

    # Phase 1 — coverage guarantee
    while len(selected_idx) < max_pages:
        starved = min(dims, key=lambda d: dim_counts[d])
        if dim_counts[starved] >= min_per_dim:
            break
        best_i, best_score = -1, -1.0
        for i, p in enumerate(pages):
            if i in selected_idx:
                continue
            if starved in (p.get("dimensions") or []):
                if scores[i] > best_score:
                    best_i, best_score = i, scores[i]
        if best_i < 0:
            break
        selected_idx.add(best_i)
        for d in pages[best_i].get("dimensions") or []:
            dim_counts[d] += 1

    # Phase 2 — quality fill
    remaining = [(scores[i], i) for i in range(len(pages))
                 if i not in selected_idx]
    remaining.sort(reverse=True)
    for _, i in remaining:
        if len(selected_idx) >= max_pages:
            break
        selected_idx.add(i)

    return [pages[i] for i in sorted(selected_idx)]


def run(cfg: dict, client, tech: dict, force: bool = False,
        on_page=None) -> dict:
    tdir = tech_dir(cfg, tech["tech_id"])
    out_path = tdir / "evidence.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    manifest = read_json(tdir / "fetch_manifest.json")
    if not manifest:
        raise RuntimeError(
            f"No fetch_manifest.json for {tech['tech_id']}. Run fetch first."
        )

    aliases_data = read_json(tdir / "aliases.json") or {}
    all_aliases = (
        aliases_data.get("aliases", [])
        + aliases_data.get("companies", [])
        + aliases_data.get("category_terms", [])
    )

    pages = _collect_pages(cfg, manifest)

    max_pages = cfg.get("extract", {}).get("max_pages_to_extract", 200)
    min_per_dim = cfg.get("extract", {}).get("min_pages_per_dim", 7)
    n_dropped = 0
    if len(pages) > max_pages:
        n_dropped = len(pages) - max_pages
        pages = _select_pages(pages, max_pages, min_per_dim)
        from collections import Counter
        cov = Counter()
        for p in pages:
            for d in p.get("dimensions", []):
                cov[d] += 1
        cov_str = " ".join(f"{d}={cov[d]}" for d in
                           ["TRL", "MRL", "RRL", "ARL", "ORL"])
        print(f"  M5: {len(pages)}/{len(pages)+n_dropped} pages selected "
              f"({n_dropped} dropped). Coverage: {cov_str}")

    model = cfg["models"]["extractor"]
    all_records = []
    page_stats = []
    counter = 0

    for i, pg in enumerate(pages):
        if on_page:
            on_page(i + 1, len(pages))
        user_prompt = _build_user_prompt(
            tech["name"], all_aliases, pg["title"], pg["text"],
            pg["dimensions"],
        )
        calls: list[dict] = []
        try:
            data = llm.chat_json(
                client, model, SYSTEM, user_prompt, module="m5_evidence",
                collect=calls,
            )
            records = data.get("records", [])
        except Exception as e:  # noqa: BLE001
            page_stats.append({
                "url": pg["url"], "page_hash": pg["page_hash"],
                "status": f"llm_error: {type(e).__name__}", "n_records": 0,
                "n_calls": len(calls),
                "elapsed_s": round(sum(c["elapsed_s"] for c in calls), 3),
                "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                "completion_tokens": sum(c["completion_tokens"] for c in calls),
                "n_unparsed": sum(1 for c in calls if c["parsed"] is False),
            })
            continue

        for rec in records:
            counter += 1
            all_records.append({
                "evidence_id": f"{tech['tech_id']}_e{counter:03d}",
                "url": pg["url"],
                "title": pg["title"],
                "pub_date": pg.get("date"),
                "snippet": rec.get("snippet", ""),
                "dimensions": rec.get("dimensions") or [],
                "bin_signal": normalize_bin_signal(rec),
                "source_type": rec.get("source_type", "news"),
                "source_quality": rec.get("source_quality", "med"),
            })

        page_stats.append({
            "url": pg["url"], "page_hash": pg["page_hash"],
            "status": "ok", "n_records": len(records),
            "n_calls": len(calls),
            "elapsed_s": round(sum(c["elapsed_s"] for c in calls), 3),
            "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
            "completion_tokens": sum(c["completion_tokens"] for c in calls),
            "n_unparsed": sum(1 for c in calls if c["parsed"] is False),
        })

    result = {
        "tech_id": tech["tech_id"],
        "n_pages_processed": len(page_stats),
        "n_pages_dropped_by_cap": n_dropped,
        "n_records": len(all_records),
        "n_llm_calls": sum(ps["n_calls"] for ps in page_stats),
        "llm_elapsed_s": round(sum(ps["elapsed_s"] for ps in page_stats), 3),
        "records": all_records,
        "page_stats": page_stats,
        "extracted_at": now_iso(),
    }
    write_json(out_path, result)
    return result
