"""M2 — Query generation.

Deterministic templates per readiness dimension, instantiated with the
technology name, aliases, companies, and category terms. No LLM call.
Terms are routed by type: companies go to MRL/TRL queries only;
category terms and the best alias cover all dimensions.
"""

# {t} = technology name, alias, or category term
TEMPLATES = {
    "TRL": [
        "{t} field trial results",
        "{t} commercial deployment",
        "{t} pilot demonstration",
        "{t} launches product",
        "{t} patent",
    ],
    "MRL": [
        "{t} pricing cost buy",
        "{t} raises funding",
        "{t} customers case study",
        "{t} market adoption",
        "{t} partnership deal announced",
    ],
    "RRL": [
        "{t} regulation approval permit",
        "{t} certification granted",
        "{t} compliance standards",
        "{t} legal requirements",
    ],
    "ARL": [
        "{t} controversy backlash criticism",
        "{t} public opinion acceptance",
        "{t} concerns opposition",
        "{t} endorsement support survey",
    ],
    "ORL": [
        "{t} adoption experience case study",
        "{t} implementation challenges",
        "{t} workflow integration training",
        "{t} deployment guide requirements",
    ],
}

# Company-anchored templates — paired with the tech name so they stay precise
COMPANY_TEMPLATES = [
    "{c} {t} deployment customers",
    "{c} {t} launch news",
]


def _jaccard_distance(a: str, b: str) -> float:
    """Word-level Jaccard distance: 1 - |A∩B| / |A∪B|.

    Higher = more different words = explores new search ground.
    """
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    union = sa | sb
    if not union:
        return 0.0
    return 1 - len(sa & sb) / len(union)


def _pick_most_diverse(primary: str, candidates: list[str], n: int) -> list[str]:
    """Pick the n candidates most dissimilar to primary by Jaccard distance."""
    if not candidates:
        return []
    scored = [(c, _jaccard_distance(primary, c)) for c in candidates]
    scored.sort(key=lambda x: -x[1])
    return [c for c, _ in scored[:n]]


def build_queries(tech_name: str, aliases: dict, dimensions: list[str]) -> dict:
    """Return {dimension: [query, ...]} using name + companies + best terms.

    Routing strategy:
    - Primary name → all templates for all dimensions.
    - Best alias (most dissimilar to primary via Jaccard) → first 2
      templates per dimension.
    - Best category_term (most dissimilar via Jaccard) → first template
      per dimension.
    - Companies (up to 2) → company-specific templates for TRL + MRL only.
    """
    companies = aliases.get("companies", [])[:2]
    best_alias = _pick_most_diverse(tech_name, aliases.get("aliases", []), 1)
    best_cat = _pick_most_diverse(tech_name, aliases.get("category_terms", []), 1)

    out = {}
    for dim in dimensions:
        queries = []

        for tpl in TEMPLATES[dim]:
            queries.append(tpl.format(t=tech_name))

        if best_alias:
            for tpl in TEMPLATES[dim][:2]:
                queries.append(tpl.format(t=best_alias[0]))

        if best_cat:
            queries.append(TEMPLATES[dim][0].format(t=best_cat[0]))

        if dim in ("TRL", "MRL") and companies:
            for comp in companies:
                for tpl in COMPANY_TEMPLATES:
                    queries.append(tpl.format(c=comp, t=tech_name))

        seen, deduped = set(), []
        for q in queries:
            if q.lower() not in seen:
                seen.add(q.lower())
                deduped.append(q)
        out[dim] = deduped

    return out
