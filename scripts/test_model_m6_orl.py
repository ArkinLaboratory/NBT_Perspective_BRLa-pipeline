"""Test a candidate rater model on ORL assignment for Livestock techs.

Runs the M6 ORL rubric + existing evidence through a candidate model and
compares against Sonnet (primary), Gemini (secondary), and manual draft-1.

Usage:
    conda run -n env-brla python scripts/test_model_m6_orl.py MODEL_ID

Example:
    conda run -n env-brla python scripts/test_model_m6_orl.py google/claude-opus-4-6

Results saved to output/model_tests/m6_orl_{model_slug}.json.
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
from brla.m6_assign import SYSTEM_PREAMBLE, _load_rubric, _filter_evidence, _format_evidence, _build_user_prompt
from brla.utils import read_json

LIVESTOCK_TECHS = [
    "t003-feed-additives",
    "t004-rumen-consortia",
    "t005-scp",
    "t006-cultivated-meat",
    "t007-manure-biofilters",
    "t008-editing-rumen-microbiota",
    "t009-selecting-methane-traits",
    "t010-engineered-forages",
    "t011-disease-resistant-livestock",
    "t012-engineered-anaerobic-digestion",
]

MANUAL_ORL = {
    "t003-feed-additives": "High",
    "t004-rumen-consortia": "High",
    "t005-scp": "High",
    "t006-cultivated-meat": "Low",
    "t007-manure-biofilters": "Mid",
    "t008-editing-rumen-microbiota": "Mid",
    "t009-selecting-methane-traits": "High",
    "t010-engineered-forages": "High",
    "t011-disease-resistant-livestock": "High",
    "t012-engineered-anaerobic-digestion": "High",
}

TECH_NAMES = {
    "t003-feed-additives": "Methane-Inhibiting Feed Additives",
    "t004-rumen-consortia": "Engineered Rumen Consortia and Probiotics",
    "t005-scp": "Precision Fermentation (SCP) for Animal Feed",
    "t006-cultivated-meat": "Cultivated Meat",
    "t007-manure-biofilters": "Methanotrophic Manure Biofilters",
    "t008-editing-rumen-microbiota": "CRISPR-Enabled Editing of Rumen Microbiota",
    "t009-selecting-methane-traits": "Genomic Selection for Low Methane Traits",
    "t010-engineered-forages": "Engineered Forages",
    "t011-disease-resistant-livestock": "CRISPR-Based Disease-Resistant Livestock",
    "t012-engineered-anaerobic-digestion": "Engineered Anaerobic Digestion & Additives",
}


def _chat_json_flexible(client, model, system, user):
    try:
        raw = chat(client, model, system, user,
                   json_mode=True, temperature=0.0, module="m6_orl_test")
    except RuntimeError as e:
        if "temperature" in str(e).lower():
            raw = chat(client, model, system, user,
                       json_mode=True, temperature=1.0, module="m6_orl_test")
        else:
            raise
    return json.loads(_extract_json(raw))


def run_test(model_id):
    cfg = load_config()
    client = get_client(cfg)
    cache = Path(cfg["paths"]["cache_dir"])

    rubric = _load_rubric("ORL")
    system = SYSTEM_PREAMBLE + "\n\n" + rubric

    results = {"model": model_id, "techs": {}}

    for tid in LIVESTOCK_TECHS:
        evidence_data = read_json(cache / tid / "evidence.json")
        if not evidence_data:
            print(f"  SKIP {tid} — no evidence.json")
            continue

        orl_evidence = _filter_evidence(evidence_data["records"], "ORL")

        # Get existing assignments for comparison
        asgn = read_json(cache / tid / "assignments.json") or {}
        sonnet_bin = gemini_bin = "?"
        sonnet_gap = gemini_gap = False
        for a in asgn.get("assignments", []):
            if a["dimension"] != "ORL":
                continue
            if "sonnet" in a["rater"]:
                sonnet_bin = a["bin"]
                sonnet_gap = a.get("evidence_gap", False)
            elif "gemini" in a["rater"] or "flash" in a["rater"]:
                gemini_bin = a["bin"]
                gemini_gap = a.get("evidence_gap", False)

        tech = {"name": TECH_NAMES[tid], "tech_id": tid}
        evidence_text = _format_evidence(orl_evidence)
        user = _build_user_prompt(tech, "ORL", rubric, evidence_text,
                                  len(orl_evidence))

        print(f"  {tid} ({len(orl_evidence)} ORL records)...", end=" ",
              flush=True)

        t0 = time.perf_counter()
        try:
            data = _chat_json_flexible(client, model_id, system, user)
            elapsed = time.perf_counter() - t0
            status = "ok"
        except Exception as e:
            elapsed = time.perf_counter() - t0
            data = {"bin": "ERROR", "rationale": str(e)}
            status = f"error: {e}"

        opus_bin = data.get("bin", "?")
        opus_gap = data.get("evidence_gap", False)
        manual = MANUAL_ORL.get(tid, "?")

        results["techs"][tid] = {
            "opus_bin": opus_bin,
            "opus_gap": opus_gap,
            "opus_rationale": data.get("rationale", ""),
            "opus_level_estimate": data.get("level_estimate"),
            "opus_evidence_ids": data.get("evidence_ids", []),
            "sonnet_bin": sonnet_bin,
            "sonnet_gap": sonnet_gap,
            "gemini_bin": gemini_bin,
            "gemini_gap": gemini_gap,
            "manual_bin": manual,
            "n_orl_evidence": len(orl_evidence),
            "elapsed_s": round(elapsed, 2),
            "status": status,
        }

        match_manual = "==" if opus_bin == manual else "!="
        print(f"{elapsed:.1f}s  Opus={opus_bin:4s} Sonnet={sonnet_bin:4s} "
              f"Gemini={gemini_bin:4s} Manual={manual:4s}  "
              f"Opus{match_manual}Manual"
              f"{'  GAP' if opus_gap else ''}")

    # Save
    slug = model_id.replace("/", "_").replace(" ", "_")
    out_dir = Path(cfg["paths"]["output_dir"]) / "model_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"m6_orl_{slug}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Summary
    print(f"\n{'=' * 75}")
    print(f"  ORL AGREEMENT SUMMARY")
    print(f"{'=' * 75}")
    print(f"  {'Tech':36s} {'Sonnet':>6s} {'Gemini':>6s} {'Opus':>6s} {'Manual':>6s}")
    print(f"  {'-' * 70}")

    opus_match = sonnet_match = gemini_match = 0
    total = 0
    for tid in LIVESTOCK_TECHS:
        if tid not in results["techs"]:
            continue
        r = results["techs"][tid]
        total += 1
        if r["opus_bin"] == r["manual_bin"]:
            opus_match += 1
        if r["sonnet_bin"] == r["manual_bin"]:
            sonnet_match += 1
        if r["gemini_bin"] == r["manual_bin"]:
            gemini_match += 1

        markers = ""
        if r["opus_bin"] == r["manual_bin"]:
            markers += " <<"
        print(f"  {tid:36s} {r['sonnet_bin']:>6s} {r['gemini_bin']:>6s} "
              f"{r['opus_bin']:>6s} {r['manual_bin']:>6s}{markers}")

    print(f"  {'-' * 70}")
    print(f"  {'Agreement with manual:':36s} "
          f"{sonnet_match:>4d}/10 {gemini_match:>4d}/10 "
          f"{opus_match:>4d}/10")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    run_test(sys.argv[1])


if __name__ == "__main__":
    main()
