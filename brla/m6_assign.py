"""M6 — Per-dimension readiness-level assignment with two independent raters.

For each dimension, the rubric prompt (prompts/assign_{DIM}.md) plus the
filtered evidence records are sent to two different model families.  Each rater
outputs a bin (Low/Mid/High), optional level estimate, rationale citing
evidence IDs, and an evidence_gap flag.  Both raters' outputs are stored so
M7 can compare them.
"""
import json
import time
from pathlib import Path

from . import llm
from .config import ROOT
from .utils import checkpoint_exists, now_iso, read_json, tech_dir, write_json

PROMPTS_DIR = ROOT / "prompts"

SYSTEM_PREAMBLE = """\
You are a Balanced Readiness Level (BRLa) assessor.  You will be given a
rubric for one readiness dimension and a set of evidence records extracted
from web sources about a specific technology.  Your job is to assign a
readiness bin (Low / Mid / High) for that dimension based ONLY on the
evidence provided.

Assessment rules:
1. TOP-DOWN LOGIC: Consider whether the evidence supports High first, then
   Mid, then Low.  The bin is the HIGHEST level the evidence clearly supports.
2. ABSENCE OF EVIDENCE ≠ EVIDENCE OF ACCEPTANCE.  "No controversy found" may
   mean High ARL or may mean the technology is too obscure for public opinion
   to exist.  When no direct evidence supports the bin, you MUST set
   evidence_gap to true and explain what you inferred and from what.
3. NO CROSS-DIMENSION ANCHORING.  A technology can be TRL-High and RRL-Low.
   Do not let your knowledge of one dimension influence another.  You are
   assessing ONE dimension.
4. CITE EVIDENCE.  Your rationale must reference specific evidence_ids.  Do
   not make claims the evidence does not support.
5. LEVEL ESTIMATE: after choosing a bin, estimate a finer level (1-9) ONLY if
   the evidence clearly supports it.  Otherwise set level_estimate to null.
   null is a valid and common answer.
6. EVIDENCE GAP: set to true whenever the bin rests on inference, absence of
   counter-evidence, or very thin coverage rather than direct, concrete
   evidence.  This is especially expected for ARL and ORL dimensions.
7. TECHNOLOGY SCOPE: assess ONLY the specific technology named above.  If the
   evidence contains information about adjacent or simpler technologies (e.g.,
   conventional probiotics when the technology is engineered rumen consortia,
   or single-strain inoculants when the technology is synthetic
   meta-organisms), do not base your assessment on that evidence.  Stay
   anchored to the technology as described.

Respond with a single JSON object:
{
  "dimension": "<DIM>",
  "bin": "Low" | "Mid" | "High",
  "level_estimate": "<N>" | "<N-M>" | null,
  "rationale": "2-3 sentences citing evidence_ids",
  "evidence_ids": ["...", "..."],
  "evidence_gap": true | false
}"""


def _load_rubric(dimension: str) -> str:
    path = PROMPTS_DIR / f"assign_{dimension}.md"
    if not path.exists():
        raise RuntimeError(f"Missing prompt file: {path}")
    return path.read_text(encoding="utf-8")


def _filter_evidence(records: list[dict], dimension: str) -> list[dict]:
    """Return evidence records relevant to this dimension."""
    return [r for r in records if dimension in r.get("dimensions", [])]


def _format_evidence(records: list[dict]) -> str:
    if not records:
        return "(No evidence records for this dimension.)"
    lines = []
    for r in records:
        lines.append(
            f"{r['evidence_id']} | {r['source_type']} | {r['source_quality']} | "
            f"\"{r['snippet']}\""
        )
    return "\n".join(lines)


def _build_user_prompt(tech: dict, dimension: str, rubric: str,
                       evidence_text: str, n_records: int) -> str:
    lines = [
        f"Technology: {tech['name']}",
        f"Description: {tech.get('description', '(none)')}",
        f"Dimension to assess: {dimension}",
        f"Number of evidence records: {n_records}",
        "",
        "--- RUBRIC ---",
        rubric,
        "",
        "--- EVIDENCE ---",
        evidence_text,
    ]
    return "\n".join(lines)


def _assign_one(client, model: str, tech: dict, dimension: str,
                rubric: str, evidence: list[dict]) -> dict:
    evidence_text = _format_evidence(evidence)
    system = SYSTEM_PREAMBLE + "\n\n" + rubric
    user = _build_user_prompt(tech, dimension, rubric, evidence_text,
                              len(evidence))

    data = llm.chat_json(client, model, system, user, module="m6_assign")
    data["dimension"] = dimension
    data["rater"] = model
    data.setdefault("bin", "Mid")
    data.setdefault("level_estimate", None)
    data.setdefault("rationale", "")
    data.setdefault("evidence_ids", [])
    data.setdefault("evidence_gap", len(evidence) == 0)
    if dimension == "RRL":
        data.setdefault("jurisdiction_variation", False)
    else:
        data["jurisdiction_variation"] = False
    return data


def run(cfg: dict, client, tech: dict, force: bool = False) -> dict:
    tdir = tech_dir(cfg, tech["tech_id"])
    out_path = tdir / "assignments.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    evidence_data = read_json(tdir / "evidence.json")
    if not evidence_data:
        raise RuntimeError(
            f"No evidence.json for {tech['tech_id']}. Run extract first."
        )
    all_records = evidence_data["records"]

    dimensions = cfg.get("dimensions", ["TRL", "MRL", "RRL", "ARL", "ORL"])
    raters = [cfg["models"]["primary_rater"], cfg["models"]["second_rater"]]
    assignments = []

    for dim in dimensions:
        rubric = _load_rubric(dim)
        evidence = _filter_evidence(all_records, dim)

        for model in raters:
            try:
                result = _assign_one(client, model, tech, dim, rubric, evidence)
                assignments.append(result)
            except Exception:  # noqa: BLE001
                # One assignment-level retry after a pause, so a short burst
                # rate-limit does not cost us the whole rater/dimension cell.
                time.sleep(10)
                try:
                    result = _assign_one(client, model, tech, dim, rubric,
                                         evidence)
                    assignments.append(result)
                except Exception as e:  # noqa: BLE001
                    assignments.append({
                        "dimension": dim,
                        "rater": model,
                        "bin": None,
                        "level_estimate": None,
                        "rationale": f"LLM error: {type(e).__name__}: {e}",
                        "evidence_ids": [],
                        "evidence_gap": True,
                        "error": str(e),
                    })

    result = {
        "tech_id": tech["tech_id"],
        "n_assignments": len(assignments),
        "assignments": assignments,
        "assigned_at": now_iso(),
    }
    write_json(out_path, result)
    return result
