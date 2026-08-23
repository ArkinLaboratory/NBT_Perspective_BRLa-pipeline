"""M4b — Gemini Deep Research report ingestion (URL discovery only).

DR reports are used purely as a source of citations: every cited URL is
extracted and merged into the tech's URL pool, so M4 fetches the ORIGINAL
sources and provenance points at primary evidence.  The report text itself
never enters the evidence pipeline — it is LLM-generated synthesis, and
extracting from it would feed the report's own readiness verdicts back to
the raters as if they were evidence.

Supported inputs in deep_research_reports/: .md, .txt (recommended exports),
.pdf if pypdf is installed.
"""
from pathlib import Path

from .utils import (checkpoint_exists, extract_urls, now_iso, read_json,
                    tech_dir, write_json)


def _read_report(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError(
                "pip install pypdf to ingest PDF reports, or export the "
                "report as markdown/text instead."
            ) from e
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise RuntimeError(f"Unsupported DR report format: {suffix}")


def run(cfg: dict, tech: dict, report_filename: str, force: bool = False) -> dict:
    tdir = tech_dir(cfg, tech["tech_id"])
    out_path = tdir / "dr_sources.json"
    if checkpoint_exists(out_path, force):
        return read_json(out_path)

    report_path = cfg["paths"]["dr_reports_dir"] / report_filename
    if not report_path.exists():
        raise RuntimeError(f"DR report not found: {report_path}")

    text = _read_report(report_path)
    cited = [u for u in extract_urls(text)
             if "google.com/search" not in u and "gemini.google" not in u]

    result = {
        "tech_id": tech["tech_id"],
        "report_file": report_filename,
        "cited_urls": cited,
        "n_cited_urls": len(cited),
        "ingested_at": now_iso(),
    }
    write_json(out_path, result)
    return result
