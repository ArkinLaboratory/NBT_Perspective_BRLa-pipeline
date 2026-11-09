"""Foldback — optional utility to sync review.xlsx human fields into master.json.

The reviewer fills in three columns per row in review.xlsx:
  human_bin    (Low / Mid / High or blank)
  human_notes  (free text or blank)
  reviewed     (any truthy value: "yes", "x", "1", TRUE)

This is no longer a required step in the pipeline workflow — human review
fields are preserved automatically across rebuilds by output_writer.run().
Foldback remains available as a standalone utility for manually syncing
xlsx edits into master.json without re-running the pipeline.
"""
import shutil

from .output_writer import read_reviewed_xlsx
from .utils import read_json, write_json


def run(cfg: dict) -> dict:
    out_dir = cfg["paths"]["output_dir"]
    master_path = out_dir / "master.json"
    xlsx_path = out_dir / "review.xlsx"

    if not master_path.exists():
        raise RuntimeError(f"master.json not found at {master_path}")
    if not xlsx_path.exists():
        raise RuntimeError(f"review.xlsx not found at {xlsx_path}")

    backup_path = master_path.with_suffix(".json.bak")
    shutil.copy2(master_path, backup_path)

    master = read_json(master_path)
    reviewed = read_reviewed_xlsx(xlsx_path)

    updated = 0
    for entry in master["entries"]:
        key = (entry["tech_id"], entry["dimension"])
        if key in reviewed:
            human = reviewed[key]
            entry["human_bin"] = human["human_bin"]
            entry["human_notes"] = human["human_notes"]
            entry["reviewed"] = human["reviewed"]
            updated += 1

    write_json(master_path, master)
    return {
        "n_reviewed_rows": len(reviewed),
        "n_updated": updated,
        "backup": str(backup_path),
    }
