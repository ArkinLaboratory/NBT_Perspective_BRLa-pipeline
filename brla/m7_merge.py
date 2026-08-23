"""M7 — Confidence merge and review flagging.

No LLM calls.  Two deterministic signals per dimension:

1. evidence_strength (0–1): f(n_sources, quality_mix, recency, directness)
2. rater_agreement (bool): do both raters' bins match?

Flag: needs_review = (not agreement) or (strength < threshold) or evidence_gap
      or single_entity_risk

A dimension is also marked "degraded" when the primary rater errored out in M6
and the second rater's result was promoted, so only one rater stands behind it.

Output: cache/{tech}/final.json with per-dimension merged results.
"""
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from .utils import (checkpoint_exists, normalize_bin_signal, now_iso,
                    read_json, tech_dir, write_json)

QUALITY_WEIGHTS = {"high": 1.0, "med": 0.6, "low": 0.3}
STRENGTH_THRESHOLD = 0.4

# evidence_strength component weights (sum to 1.0)
W_COUNT = 0.20
W_QUALITY = 0.25
W_RECENCY = 0.10
W_DIRECTNESS = 0.25
W_INDEPENDENCE = 0.20
COUNT_SATURATION = 15  # unique URLs at which source-count factor reaches 1.0


def _recency_score(pub_date: str | None) -> float:
    if not pub_date:
        return 0.3
    try:
        d = datetime.strptime(pub_date[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - d).days
        if age_days < 365 * 2:
            return 1.0
        if age_days < 365 * 5:
            return 0.7
        return 0.4
    except (ValueError, TypeError):
        return 0.3


def evidence_strength(records: list[dict], dimension: str,
                      companies: list[str] | None = None) -> float:
    """Compute a 0–1 evidence strength score for one dimension."""
    if not records:
        return 0.0

    n = len(records)
    unique_urls = len({r.get("url", "") for r in records})
    count_score = min(1.0, unique_urls / COUNT_SATURATION)

    quality_vals = [QUALITY_WEIGHTS.get(r.get("source_quality", "med"), 0.5)
                    for r in records]
    quality_score = sum(quality_vals) / len(quality_vals)

    recency_vals = [_recency_score(r.get("pub_date")) for r in records]
    recency_score = max(recency_vals)

    direct = sum(1 for r in records
                 if normalize_bin_signal(r).get(dimension) is not None)
    directness_score = direct / n

    if companies:
        _, conc = _entity_concentration(records, companies)
        independence_score = 1.0 - conc
    else:
        independence_score = 1.0

    return (W_COUNT * count_score
            + W_QUALITY * quality_score
            + W_RECENCY * recency_score
            + W_DIRECTNESS * directness_score
            + W_INDEPENDENCE * independence_score)


def _pick_primary(assignments: list[dict], dimension: str,
                  primary_model: str) -> dict | None:
    """Pick the rating to report for a dimension.

    M6 still records an entry when a rater call fails (bin=None plus an
    "error" key), so the primary rater's own entry is only usable if it did
    not error. When it did, fall back to any non-error assignment for the
    dimension — in practice the second rater's result.
    """
    for a in assignments:
        if (a["dimension"] == dimension
                and a["rater"] == primary_model
                and not a.get("error")):
            return a
    for a in assignments:
        if a["dimension"] == dimension and not a.get("error"):
            return a
    return None


def _pick_second(assignments: list[dict], dimension: str,
                 primary_model: str) -> dict | None:
    for a in assignments:
        if (a["dimension"] == dimension
                and a["rater"] != primary_model
                and not a.get("error")):
            return a
    return None


def _entity_concentration(dim_evidence: list[dict],
                          companies: list[str]) -> tuple[str | None, float]:
    """Return (dominant_company, fraction) for the most-mentioned company."""
    if not companies or not dim_evidence:
        return None, 0.0
    normed = [(c, c.lower()) for c in companies if len(c) >= 4]
    if not normed:
        return None, 0.0
    mentions: Counter[str] = Counter()
    for rec in dim_evidence:
        text = (rec.get("snippet", "") + " " + rec.get("url", "")).lower()
        domain = urlparse(rec.get("url", "")).netloc.lower()
        for orig, low in normed:
            if low in text or low.replace(" ", "") in domain:
                mentions[orig] += 1
                break
    if not mentions:
        return None, 0.0
    top, count = mentions.most_common(1)[0]
    return top, count / len(dim_evidence)


def run(cfg: dict, tech: dict, force: bool = False) -> dict:
    tdir = tech_dir(cfg, tech["tech_id"])
    out_path = tdir / "final.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    evidence_data = read_json(tdir / "evidence.json")
    if not evidence_data:
        raise RuntimeError(
            f"No evidence.json for {tech['tech_id']}. Run extract first.")
    assign_data = read_json(tdir / "assignments.json")
    if not assign_data:
        raise RuntimeError(
            f"No assignments.json for {tech['tech_id']}. Run assign first.")
    aliases_data = read_json(tdir / "aliases.json") or {}

    all_records = evidence_data["records"]
    assignments = assign_data["assignments"]
    companies = aliases_data.get("companies", [])
    primary_model = cfg["models"]["primary_rater"]
    threshold = cfg.get("review_threshold", STRENGTH_THRESHOLD)
    conc_threshold = cfg.get("entity_concentration_threshold", 0.70)
    dimensions = cfg.get("dimensions", ["TRL", "MRL", "RRL", "ARL", "ORL"])

    dim_results = {}
    for dim in dimensions:
        dim_evidence = [r for r in all_records if dim in r.get("dimensions", [])]
        strength = evidence_strength(dim_evidence, dim, companies)

        primary = _pick_primary(assignments, dim, primary_model)
        # The primary rater failed and a second-rater result was promoted in
        # its place: only one rater actually produced a rating, so there is no
        # independent second opinion to compare against.
        degraded = primary is not None and primary["rater"] != primary_model
        second = (None if degraded
                  else _pick_second(assignments, dim, primary_model))

        p_bin = primary.get("bin") if primary else None
        s_bin = second.get("bin") if second else None
        agreement = (p_bin is not None and s_bin is not None
                     and p_bin == s_bin)

        gap = False
        if primary and primary.get("evidence_gap"):
            gap = True
        if second and second.get("evidence_gap"):
            gap = True

        juris_var = False
        if dim == "RRL":
            if primary and primary.get("jurisdiction_variation"):
                juris_var = True
            if second and second.get("jurisdiction_variation"):
                juris_var = True

        dom_entity, conc = _entity_concentration(dim_evidence, companies)
        entity_risk = conc >= conc_threshold

        needs_review = ((not agreement) or (strength < threshold)
                        or gap or entity_risk)

        dim_results[dim] = {
            "bin": p_bin,
            "level_estimate": primary.get("level_estimate") if primary else None,
            "rationale": primary.get("rationale", "") if primary else "",
            "evidence_ids": primary.get("evidence_ids", []) if primary else [],
            "evidence_strength": round(strength, 3),
            "n_evidence_records": len(dim_evidence),
            "rater_agreement": agreement,
            "evidence_gap": gap,
            "jurisdiction_variation": juris_var,
            "entity_concentration": round(conc, 2),
            "dominant_entity": dom_entity,
            "single_entity_risk": entity_risk,
            "needs_review": needs_review,
            "degraded": degraded,
            "primary_rater": {
                "model": primary["rater"] if primary else None,
                "bin": p_bin,
                "level_estimate": primary.get("level_estimate") if primary else None,
            },
            "second_rater": {
                "model": second["rater"] if second else None,
                "bin": s_bin,
                "level_estimate": second.get("level_estimate") if second else None,
                "rationale": second.get("rationale", "") if second else "",
                "evidence_ids": second.get("evidence_ids", []) if second else [],
            },
        }

    result = {
        "tech_id": tech["tech_id"],
        "dimensions": dim_results,
        "review_threshold": threshold,
        "merged_at": now_iso(),
    }
    write_json(out_path, result)
    return result
