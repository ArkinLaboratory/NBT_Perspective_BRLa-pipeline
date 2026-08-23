"""M1 — Alias expansion.

The same technology appears online under product names, company names,
generic category names and academic terms. One cheap LLM call per technology
generates the alias set that M2 uses to build queries and M5 uses to
recognize the technology in fetched text.
"""
from . import llm
from .utils import checkpoint_exists, now_iso, read_json, tech_dir, write_json

SYSTEM = """You expand technology names into search-friendly terms for web search.
The goal is to surface diverse sources: company sites, trade press, industry news,
regulatory pages, AND academic papers — not just synonyms that all find the same
academic literature.

Respond with a single JSON object, no other text:
{
  "aliases": [up to 6 terms — include branded/product names if any, names
              practitioners or buyers use, and academic synonyms. Put the most
              distinctive terms first; avoid near-duplicates that share most words],
  "companies": [up to 4 companies, startups, or organizations actively developing,
                selling, or deploying this technology. Include industry groups or
                research consortia if no commercial vendors exist],
  "category_terms": [up to 4 broader applied-category terms that practitioners use,
                     e.g. "seed inoculants" for nitrogen-fixing microbe products,
                     or "virtual fencing" for GPS-collar grazing systems]
}

Guidelines:
- For aliases, prefer terms that would appear in trade press, product pages, and
  extension/field-trial reports — not just journal article titles.
- For companies, include any you are reasonably confident about. Organizations that
  have published pilots, raised funding, or hold patents all count.
- Do not invent company names you are unsure of; but do not leave the list empty
  just because the technology is niche — even niche technologies have developers.
- Avoid near-duplicate aliases (e.g. do not list both "methane biofilter" and
  "methane biofiltration" — pick one)."""


def run(cfg: dict, client, tech: dict, force: bool = False) -> dict:
    out_path = tech_dir(cfg, tech["tech_id"]) / "aliases.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    user = f"Technology: {tech['name']}\nDescription: {tech.get('description', '(none)')}"
    data = llm.chat_json(
        client, cfg["models"]["alias_expander"], SYSTEM, user, module="m1_aliases"
    )
    result = {
        "tech_id": tech["tech_id"],
        "name": tech["name"],
        "aliases": data.get("aliases", [])[:6],
        "companies": data.get("companies", [])[:4],
        "category_terms": data.get("category_terms", [])[:4],
        "generated_at": now_iso(),
    }
    write_json(out_path, result)
    return result
