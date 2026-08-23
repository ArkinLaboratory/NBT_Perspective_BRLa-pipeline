#!/usr/bin/env python
"""Measure how much of Gemini's own Deep Research a cheaper M1/M2 recovers.

Every tech that went through `ingest-dr` + `search` already has, for free,
two things worth comparing:
  - cache/{tech_id}/dr_sources.json  -- URLs Gemini's Deep Research cited
  - cache/{tech_id}/search_results.json -- URLs Tavily returned for M2's
    template-generated queries

Diffing these tells you, per technology and with zero new API calls, what
fraction of Gemini's own sources M2's queries would have surfaced. It is a
recall measurement, not a quality judgement: a URL missing from the M2 side
just means the *domain* never showed up in a Tavily result for any query M2
built, not that the underlying fact is missing (M4b already merges DR
cited_urls into the fetch pool regardless of what M2 finds).

Split DR reports share a preamble/summary-matrix/conclusion across every
technology in a sector (see scripts/split_dr_report.py), so a handful of
citation domains show up in EVERY tech's dr_sources.json regardless of what
that tech actually is -- measured 2026-07-31: proteinproductiontechnology.com
and adsa.org appear in all 10 Livestock split files. Scoring those as "missed"
penalizes M2 for not finding a source that was never really about that
technology. Domains cited by >=2 techs sharing the same sector prefix (the
part of report_file before "__") are treated as shared/boilerplate and
excluded from the recall denominator, but still listed for transparency.

M1 alias/company recall needs an external ground truth -- there is no free
"aliases Gemini definitely would have listed" data lying in cache. Supply it
by saving Gemini's structured critique output (see the Part A JSON schema in
the mining prompt) as ground_truth/{tech_id}.json; the check is skipped for
any tech without a matching file.

    python scripts/check_recall.py                     # all cached techs, M2 only
    python scripts/check_recall.py --tech t003-feed-additives
    python scripts/check_recall.py --ground-truth-dir ground_truth

Stdlib only. No third-party imports.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"


def normalize_domain(url: str) -> str:
    """Strip scheme/www/path so syndication across paths on one host still counts."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def load_tech_cache(tech_dir: Path) -> tuple[dict, dict, dict] | None:
    """Return (dr_sources, search_results, aliases) or None if DR/search never ran."""
    dr_path = tech_dir / "dr_sources.json"
    search_path = tech_dir / "search_results.json"
    if not dr_path.exists() or not search_path.exists():
        return None
    dr_sources = json.loads(dr_path.read_text())
    search_results = json.loads(search_path.read_text())
    aliases_path = tech_dir / "aliases.json"
    aliases = json.loads(aliases_path.read_text()) if aliases_path.exists() else {}
    return dr_sources, search_results, aliases


def sector_of(dr_sources: dict) -> str:
    """report_file 'BRLa-v2_Livestock__methane-inhibiting-feed-additives.md' -> 'BRLa-v2_Livestock'."""
    report_file = dr_sources.get("report_file", "")
    return report_file.split("__", 1)[0] if "__" in report_file else report_file


def build_shared_domains(all_dr_sources: list[dict], min_techs: int = 2) -> dict[str, set[str]]:
    """Per sector, domains cited by >=min_techs distinct techs -- boilerplate, not tech-specific."""
    sector_domain_techs: dict[str, dict[str, int]] = {}
    for dr_sources in all_dr_sources:
        sector = sector_of(dr_sources)
        domains = {normalize_domain(u) for u in dr_sources.get("cited_urls", [])}
        counts = sector_domain_techs.setdefault(sector, {})
        for d in domains:
            counts[d] = counts.get(d, 0) + 1
    return {
        sector: {d for d, n in counts.items() if n >= min_techs}
        for sector, counts in sector_domain_techs.items()
    }


def m2_url_recall(dr_sources: dict, search_results: dict, shared_domains: set[str]) -> dict:
    """Domain-level (primary) and exact-URL (secondary) recall of DR citations.

    Domains in `shared_domains` (boilerplate cited across the whole sector) are
    excluded from the recall denominator -- see module docstring.
    """
    cited_urls = dr_sources.get("cited_urls", [])
    all_cited_domains = {normalize_domain(u) for u in cited_urls}
    cited_domains = all_cited_domains - shared_domains
    shared_cited_domains = sorted(all_cited_domains & shared_domains)
    found_urls = {r["url"] for r in search_results.get("results", [])}
    found_domains = {normalize_domain(u) for u in found_urls}

    missed_domains = sorted(cited_domains - found_domains)
    return {
        "n_cited_urls": len(cited_urls),
        "n_cited_domains": len(cited_domains),
        "n_exact_url_matches": len(set(cited_urls) & found_urls),
        "n_domain_matches": len(cited_domains & found_domains),
        "domain_recall": (
            len(cited_domains & found_domains) / len(cited_domains)
            if cited_domains else None
        ),
        "missed_domains": missed_domains,
        "shared_excluded_domains": shared_cited_domains,
    }


def m1_entity_recall(aliases: dict, ground_truth: dict) -> dict:
    """Case-insensitive substring match of M1's output against Gemini's entity list."""
    m1_terms = " ".join(
        aliases.get("aliases", []) + aliases.get("companies", [])
    ).lower()
    gt_entities = ground_truth.get("companies_orgs", []) + ground_truth.get(
        "named_products", []
    )
    missed = [e for e in gt_entities if e.lower() not in m1_terms]
    return {
        "n_ground_truth_entities": len(gt_entities),
        "n_recalled": len(gt_entities) - len(missed),
        "entity_recall": (
            (len(gt_entities) - len(missed)) / len(gt_entities)
            if gt_entities else None
        ),
        "missed_entities": missed,
    }


def format_report(tech_id: str, m2: dict, m1: dict | None) -> str:
    lines = [f"## {tech_id}", ""]
    if m2["n_cited_domains"] == 0:
        lines.append("(no dr_sources.json citations -- non-DR tech)")
    else:
        lines.append(
            f"M2 domain recall: {m2['n_domain_matches']}/{m2['n_cited_domains']} "
            f"({m2['domain_recall']:.0%}) -- exact URL matches: {m2['n_exact_url_matches']}"
        )
        if m2["missed_domains"]:
            lines.append("  missed: " + ", ".join(m2["missed_domains"]))
        if m2["shared_excluded_domains"]:
            lines.append(
                "  excluded (shared/boilerplate across sector): "
                + ", ".join(m2["shared_excluded_domains"])
            )
    if m1 is not None:
        if m1["n_ground_truth_entities"] == 0:
            lines.append("(ground truth file present but empty entity lists)")
        else:
            lines.append(
                f"M1 entity recall: {m1['n_recalled']}/{m1['n_ground_truth_entities']} "
                f"({m1['entity_recall']:.0%})"
            )
            if m1["missed_entities"]:
                lines.append("  missed: " + ", ".join(m1["missed_entities"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tech", help="Check a single tech_id instead of all cached techs")
    ap.add_argument(
        "--ground-truth-dir",
        type=Path,
        help="Directory of {tech_id}.json files (Gemini Part A output) for M1 recall",
    )
    args = ap.parse_args()

    tech_dirs = (
        [CACHE_DIR / args.tech]
        if args.tech
        else sorted(d for d in CACHE_DIR.iterdir() if d.is_dir() and d.name not in ("pages", "tavily_pages"))
    )

    loaded_by_dir = {}
    for tech_dir in tech_dirs:
        loaded = load_tech_cache(tech_dir)
        if loaded is not None:
            loaded_by_dir[tech_dir] = loaded

    shared_by_sector = build_shared_domains(
        [dr_sources for dr_sources, _, _ in loaded_by_dir.values()]
    )

    for tech_dir, (dr_sources, search_results, aliases) in loaded_by_dir.items():
        shared_domains = shared_by_sector.get(sector_of(dr_sources), set())
        m2 = m2_url_recall(dr_sources, search_results, shared_domains)

        m1 = None
        if args.ground_truth_dir:
            gt_path = args.ground_truth_dir / f"{tech_dir.name}.json"
            if gt_path.exists():
                m1 = m1_entity_recall(aliases, json.loads(gt_path.read_text()))

        print(format_report(tech_dir.name, m2, m1))
        print()


if __name__ == "__main__":
    main()
