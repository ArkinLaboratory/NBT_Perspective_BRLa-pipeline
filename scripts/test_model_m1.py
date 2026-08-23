"""Test a candidate model on M1 alias expansion and compare to cached Haiku baseline.

Usage:
    conda run -n env-brla python scripts/test_model_m1.py MODEL_ID [TECH_IDS...]

Examples:
    conda run -n env-brla python scripts/test_model_m1.py gpt-5.6-luna-low t002-nfix-microbes t005-scp t007-manure-biofilters
    conda run -n env-brla python scripts/test_model_m1.py gpt-5.6-luna      # defaults to 3 standard test techs

Results are saved to output/model_tests/m1_{model_slug}.json for later comparison.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from brla.config import load_config
from brla.llm import get_client, chat, _extract_json
from brla.m1_aliases import SYSTEM
from brla.utils import read_json

DEFAULT_TECHS = ["t002-nfix-microbes", "t005-scp", "t007-manure-biofilters"]

DESCRIPTIONS = {
    "t002-nfix-microbes": (
        "Engineered Associative Nitrogen-Fixing Microbes",
        "Genetically reprogrammed diazotrophs (e.g. Kosakonia sacchari, "
        "Klebsiella variicola) that continue fixing atmospheric nitrogen even "
        "in fertilized fields. Leading companies include Pivot Bio (PROVEN 40) "
        "and Bayer/Ginkgo Bioworks (Joyn Bio). Applied as seed treatments or "
        "in-furrow liquids.",
    ),
    "t005-scp": (
        "Precision Fermentation (SCP) for Animal Feed",
        "Industrial fermentation to produce high-quality protein from "
        "non-agricultural feedstocks (gas, waste) to replace soy and fishmeal. "
        "Technologies include Single-Cell Proteins (SCP) from Methylococcus "
        "capsulatus (Calysta), Fusarium venenatum (Enifer), fungal mycelium.",
    ),
    "t007-manure-biofilters": (
        "Methanotrophic Manure Biofilters",
        "Treats the back end of the cow, using bacteria to eat methane rising "
        "from manure storage. Technologies: Compost/soil beds seeded with "
        "Methylococcus or Methylosinus to oxidize methane from slurry pits.",
    ),
}


def load_baseline(cfg, tech_id):
    """Load existing Haiku-generated aliases from cache."""
    path = Path(cfg["paths"]["cache_dir"]) / tech_id / "aliases.json"
    if path.exists():
        return read_json(path)
    return None


def _chat_json_flexible(client, model, system, user):
    """Like chat_json but falls back to temperature=1 for models that reject 0."""
    try:
        raw = chat(client, model, system, user,
                   json_mode=True, temperature=0.0, module="m1_test")
    except RuntimeError as e:
        if "temperature" in str(e).lower():
            raw = chat(client, model, system, user,
                       json_mode=True, temperature=1.0, module="m1_test")
        else:
            raise
    return json.loads(_extract_json(raw))


def run_test(model_id, tech_ids):
    cfg = load_config()
    client = get_client(cfg)

    results = {"model": model_id, "techs": {}}

    for tid in tech_ids:
        if tid not in DESCRIPTIONS:
            print(f"  SKIP {tid} — no description in test harness")
            continue

        name, desc = DESCRIPTIONS[tid]
        user_msg = f"Technology: {name}\nDescription: {desc}"

        t0 = time.perf_counter()
        data = _chat_json_flexible(client, model_id, SYSTEM, user_msg)
        elapsed = time.perf_counter() - t0

        result = {
            "aliases": data.get("aliases", [])[:6],
            "companies": data.get("companies", [])[:4],
            "category_terms": data.get("category_terms", [])[:4],
            "elapsed_s": round(elapsed, 2),
        }
        results["techs"][tid] = result
        print(f"  {tid}: {elapsed:.1f}s — {len(result['aliases'])} aliases, "
              f"{len(result['companies'])} companies, "
              f"{len(result['category_terms'])} categories")

    # Save results
    slug = model_id.replace("/", "_").replace(" ", "_")
    out_dir = Path(cfg["paths"]["output_dir"]) / "model_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"m1_{slug}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Print comparison to baseline
    print(f"\n{'=' * 70}")
    print(f"  COMPARISON: cached baseline vs {model_id}")
    print(f"{'=' * 70}")
    for tid in tech_ids:
        if tid not in results["techs"]:
            continue
        baseline = load_baseline(cfg, tid)
        candidate = results["techs"][tid]
        name = DESCRIPTIONS[tid][0]

        print(f"\n  [{tid}] {name}")
        for field in ("aliases", "companies", "category_terms"):
            bl = set(baseline.get(field, [])) if baseline else set()
            cd = set(candidate.get(field, []))
            shared = bl & cd
            only_baseline = bl - cd
            only_candidate = cd - bl
            print(f"    {field}:")
            if shared:
                print(f"      shared:    {sorted(shared)}")
            if only_baseline:
                print(f"      baseline:  {sorted(only_baseline)}")
            if only_candidate:
                print(f"      candidate: {sorted(only_candidate)}")
            if not bl and not cd:
                print(f"      (both empty)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    model_id = sys.argv[1]
    tech_ids = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_TECHS

    print(f"  Model: {model_id}")
    print(f"  Techs: {', '.join(tech_ids)}\n")
    run_test(model_id, tech_ids)


if __name__ == "__main__":
    main()
