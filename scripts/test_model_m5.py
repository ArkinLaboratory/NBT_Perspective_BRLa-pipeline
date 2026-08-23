"""Test a candidate model on M5 evidence extraction and compare to cached Haiku baseline.

Runs the M5 extraction prompt on a small set of representative pages (rich,
mid, thin) per technology, then compares record counts, dimension coverage,
and snippet quality against the existing Haiku-generated evidence.

Usage:
    conda run -n env-brla python scripts/test_model_m5.py MODEL_ID [TECH_IDS...]

Examples:
    conda run -n env-brla python scripts/test_model_m5.py gpt-5.6-luna-low
    conda run -n env-brla python scripts/test_model_m5.py gpt-5.6-luna-medium t005-scp

Results saved to output/model_tests/m5_{model_slug}.json.
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from brla.config import load_config
from brla.llm import get_client, chat, _extract_json
from brla.m5_evidence import SYSTEM, _build_user_prompt
from brla.utils import read_json

DEFAULT_TECHS = ["t002-nfix-microbes", "t005-scp", "t007-manure-biofilters"]

TEST_PAGES = {
    "t002-nfix-microbes": [
        "2ceff9a476d7e69f",
        "cdc2edca9dd26881",
        "6d09aee327a80820",
    ],
    "t005-scp": [
        "f2e8f9f67d945adc",
        "271d31788226f6fa",
        "271dc49dcb35e688",
    ],
    "t007-manure-biofilters": [
        "e6a01b2eb6ee8132",
        "6e403f17c5b6a334",
        "8b362dd35948aacf",
    ],
}

TECH_NAMES = {
    "t002-nfix-microbes": "Engineered Associative Nitrogen-Fixing Microbes",
    "t005-scp": "Precision Fermentation (SCP) for Animal Feed",
    "t007-manure-biofilters": "Methanotrophic Manure Biofilters",
}


def _chat_json_flexible(client, model, system, user):
    """Like chat_json but falls back to temperature=1 for models that reject 0."""
    try:
        raw = chat(client, model, system, user,
                   json_mode=True, temperature=0.0, module="m5_test")
    except RuntimeError as e:
        if "temperature" in str(e).lower():
            raw = chat(client, model, system, user,
                       json_mode=True, temperature=1.0, module="m5_test")
        else:
            raise
    return json.loads(_extract_json(raw))


def load_baseline_records(cfg, tech_id, page_hash):
    """Get existing Haiku evidence records for a specific page."""
    ev = read_json(Path(cfg["paths"]["cache_dir"]) / tech_id / "evidence.json")
    if not ev:
        return []
    return [r for r in ev.get("records", [])
            if r.get("url") in _url_for_hash(cfg, ev, page_hash)]


def _url_for_hash(cfg, evidence, page_hash):
    """Find URL(s) associated with a page hash in the evidence stats."""
    urls = set()
    for ps in evidence.get("page_stats", []):
        if ps.get("page_hash") == page_hash:
            urls.add(ps.get("url", ""))
    return urls


def run_test(model_id, tech_ids):
    cfg = load_config()
    client = get_client(cfg)

    results = {"model": model_id, "techs": {}}

    for tid in tech_ids:
        if tid not in TEST_PAGES:
            print(f"  SKIP {tid} — no test pages configured")
            continue

        aliases_data = read_json(
            Path(cfg["paths"]["cache_dir"]) / tid / "aliases.json"
        ) or {}
        all_aliases = (
            aliases_data.get("aliases", [])
            + aliases_data.get("companies", [])
            + aliases_data.get("category_terms", [])
        )

        baseline_ev = read_json(
            Path(cfg["paths"]["cache_dir"]) / tid / "evidence.json"
        ) or {}

        tech_result = {"pages": []}
        page_hashes = TEST_PAGES[tid]

        for ph in page_hashes:
            page = read_json(cfg["paths"]["pages_dir"] / f"{ph}.json")
            if not page or not page.get("text"):
                print(f"  SKIP {ph} — no page text")
                continue

            # Find dimensions hint from baseline page_stats
            dims_hint = []
            for ps in baseline_ev.get("page_stats", []):
                if ps.get("page_hash") == ph:
                    url = ps.get("url", "")
                    break
            else:
                url = ""

            # Find baseline records for this page by URL
            baseline_recs = [r for r in baseline_ev.get("records", [])
                             if r.get("url") == url]

            user_prompt = _build_user_prompt(
                TECH_NAMES[tid], all_aliases,
                page.get("title", ""), page["text"], dims_hint,
            )

            text_len = len(page["text"])
            print(f"  {tid}/{ph} ({text_len:,} chars, "
                  f"baseline={len(baseline_recs)} records)...", end=" ", flush=True)

            t0 = time.perf_counter()
            try:
                data = _chat_json_flexible(client, model_id, SYSTEM, user_prompt)
                elapsed = time.perf_counter() - t0
                records = data.get("records", [])
                status = "ok"
            except Exception as e:
                elapsed = time.perf_counter() - t0
                records = []
                status = f"error: {type(e).__name__}: {e}"

            # Dimension distribution
            dim_counts = Counter()
            for r in records:
                for d in r.get("dimensions", []):
                    dim_counts[d] += 1

            baseline_dims = Counter()
            for r in baseline_recs:
                for d in r.get("dimensions", []):
                    baseline_dims[d] += 1

            page_result = {
                "page_hash": ph,
                "url": url,
                "text_len": text_len,
                "status": status,
                "elapsed_s": round(elapsed, 2),
                "n_records": len(records),
                "baseline_n_records": len(baseline_recs),
                "dim_counts": dict(dim_counts),
                "baseline_dim_counts": dict(baseline_dims),
                "records": records,
            }
            tech_result["pages"].append(page_result)
            print(f"{len(records)} records in {elapsed:.1f}s "
                  f"(baseline: {len(baseline_recs)})")

        # Tech-level summary
        total = sum(p["n_records"] for p in tech_result["pages"])
        baseline_total = sum(p["baseline_n_records"] for p in tech_result["pages"])
        total_time = sum(p["elapsed_s"] for p in tech_result["pages"])
        tech_result["summary"] = {
            "total_records": total,
            "baseline_total_records": baseline_total,
            "total_elapsed_s": round(total_time, 2),
            "record_ratio": round(total / baseline_total, 2) if baseline_total else None,
        }
        results["techs"][tid] = tech_result
        print(f"  {tid} total: {total} records vs baseline {baseline_total} "
              f"(ratio: {tech_result['summary']['record_ratio']})")

    # Save
    slug = model_id.replace("/", "_").replace(" ", "_")
    out_dir = Path(cfg["paths"]["output_dir"]) / "model_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"m5_{slug}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Print comparison
    print(f"\n{'=' * 70}")
    print(f"  COMPARISON: Haiku baseline vs {model_id}")
    print(f"{'=' * 70}")
    for tid in tech_ids:
        if tid not in results["techs"]:
            continue
        tr = results["techs"][tid]
        s = tr["summary"]
        print(f"\n  [{tid}]")
        print(f"    Records: {s['total_records']} candidate vs "
              f"{s['baseline_total_records']} baseline "
              f"(ratio: {s['record_ratio']})")
        print(f"    Time: {s['total_elapsed_s']:.1f}s")
        for pg in tr["pages"]:
            print(f"    {pg['page_hash'][:8]}… "
                  f"{pg['n_records']:2d} vs {pg['baseline_n_records']:2d}  "
                  f"dims: {pg['dim_counts']} vs {pg['baseline_dim_counts']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    model_id = sys.argv[1]
    tech_ids = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_TECHS

    print(f"  Model: {model_id}")
    print(f"  Techs: {', '.join(tech_ids)}")
    print(f"  Pages per tech: 3 (rich / mid / thin)\n")
    run_test(model_id, tech_ids)


if __name__ == "__main__":
    main()
