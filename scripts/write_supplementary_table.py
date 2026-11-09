#!/usr/bin/env python3
"""Generate publication-format Supplementary Table 7 from review.xlsx.

Reads the sector-split review.xlsx produced by the pipeline's
output_writer, drops diagnostic columns, re-sorts rows into
publication order, and applies journal-template formatting.

Usage:
    conda run -n env-brla python scripts/write_supplementary_table.py
    conda run -n env-brla python scripts/write_supplementary_table.py \
        --review output/review.xlsx \
        --output output/supplementary_table7.xlsx
"""

import argparse
import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ── Columns to drop ─────────────────────────────────────────────────

DIAGNOSTIC_COLUMNS = {
    "tech_id",
    "evidence_strength",
    "n_sources",
    "agreement",
    "evidence_gap",
    "juris_varies",
    "entity_conc",
    "single_rater",
    "needs_review",
    "reviewed",
}

# ── Publication layout ───────────────────────────────────────────────

SECTOR_SHEET_ORDER = [
    "Crops",
    "Soils",
    "Livestock",
    "Watersheds",
    "Aquaculture",
    "Health and Security",
    "Food Waste",
    "Frontiers",
]

DIM_ORDER = {"TRL": 0, "MRL": 1, "RRL": 2, "ARL": 3, "ORL": 4}

COL_WIDTHS = {
    "tech_name": 20,
    "dimension": 10,
    "human_notes": 30,
    "rater_1_bin": 10,
    "rater_1_level_est": 13,
    "rater_1_rationale": 65,
    "rater_2_bin": 10,
    "rater_2_level_est": 13,
    "rater_2_rationale": 55,
}
SOURCE_COL_WIDTH = 35

WRAP_COLUMNS = {
    "tech_name",
    "human_notes",
    "rater_1_rationale",
    "rater_2_rationale",
}

# ── Formatting constants ─────────────────────────────────────────────

HEADER_FILL = PatternFill("solid", fgColor="FF4472C4")
HEADER_FONT = Font(bold=True, size=10, color="FFFFFFFF")
HEADER_ALIGN = Alignment(vertical="top")

HUMAN_BIN_FILL = PatternFill("solid", fgColor="FFFFF2CC")

LINK_FONT = Font(size=9, color="FF0563C1", underline="single")
WRAP_ALIGN = Alignment(wrap_text=True, vertical="top")
TOP_ALIGN = Alignment(vertical="top")

_TECH_ID_RE = re.compile(r"t(\d+)")


# ── Helpers ──────────────────────────────────────────────────────────


def _tech_sort_key(tech_id: str) -> int:
    m = _TECH_ID_RE.match(tech_id)
    return int(m.group(1)) if m else 0


def read_review_sheet(ws):
    """Read one sector sheet → list of row dicts.

    Each dict has every non-source column value keyed by header name,
    plus 'sources': list of {value, hyperlink} for each source column.
    """
    headers = [cell.value for cell in ws[1]]

    source_start = None
    for i, h in enumerate(headers):
        if h and h.startswith("source_"):
            source_start = i
            break

    rows = []
    for row_cells in ws.iter_rows(min_row=2, max_row=ws.max_row):
        # Skip fully empty rows
        if all(c.value is None for c in row_cells):
            continue

        row = {}
        for i, cell in enumerate(row_cells):
            if i < len(headers) and headers[i]:
                if source_start is not None and i >= source_start:
                    continue  # handled below
                row[headers[i]] = cell.value

        sources = []
        if source_start is not None:
            for cell in row_cells[source_start:]:
                sources.append({
                    "value": cell.value,
                    "hyperlink": cell.hyperlink.target if cell.hyperlink else None,
                })
        # Trim trailing empty sources
        while sources and sources[-1]["value"] is None:
            sources.pop()

        row["sources"] = sources
        rows.append(row)

    return rows


def write_data_sheet(ws, rows, max_sources):
    """Write one sector's data with publication formatting."""

    # -- Publication column names (fixed + dynamic source columns)
    pub_cols = [
        "tech_name", "dimension", "human_bin", "human_notes",
        "rater_1_bin", "rater_1_level_est", "rater_1_rationale",
        "rater_2_bin", "rater_2_level_est", "rater_2_rationale",
    ]
    source_headers = [f"source_{i+1}" for i in range(max_sources)]
    all_headers = pub_cols + source_headers

    # -- Header row
    for col_idx, name in enumerate(all_headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = HEADER_ALIGN

    # -- Data rows
    for row_offset, row_data in enumerate(rows):
        excel_row = row_offset + 2

        for col_idx, col_name in enumerate(pub_cols, start=1):
            cell = ws.cell(
                row=excel_row,
                column=col_idx,
                value=row_data.get(col_name),
            )
            cell.font = Font(size=11)

            if col_name == "human_bin":
                cell.fill = HUMAN_BIN_FILL

            if col_name in WRAP_COLUMNS:
                cell.alignment = WRAP_ALIGN
            else:
                cell.alignment = TOP_ALIGN

        # Source columns
        sources = row_data.get("sources", [])
        source_col_start = len(pub_cols) + 1
        for src_idx in range(max_sources):
            col_idx = source_col_start + src_idx
            cell = ws.cell(row=excel_row, column=col_idx)

            if src_idx < len(sources) and sources[src_idx]["value"] is not None:
                cell.value = sources[src_idx]["value"]
                cell.font = LINK_FONT
                cell.alignment = WRAP_ALIGN
                if sources[src_idx]["hyperlink"]:
                    cell.hyperlink = sources[src_idx]["hyperlink"]
            else:
                cell.alignment = TOP_ALIGN

    # -- Column widths
    for col_idx, col_name in enumerate(all_headers, start=1):
        letter = get_column_letter(col_idx)
        if col_name in COL_WIDTHS:
            ws.column_dimensions[letter].width = COL_WIDTHS[col_name]
        elif col_name.startswith("source_"):
            ws.column_dimensions[letter].width = SOURCE_COL_WIDTH

    # -- Group source columns (collapsible)
    if max_sources > 0:
        first_src_col = len(pub_cols) + 1
        first_letter = get_column_letter(first_src_col)
        last_letter = get_column_letter(first_src_col + max_sources - 1)
        ws.column_dimensions.group(first_letter, last_letter, outline_level=1)

        # Re-set source column widths after group() (I-55 openpyxl bug)
        for i in range(max_sources):
            letter = get_column_letter(first_src_col + i)
            ws.column_dimensions[letter].width = SOURCE_COL_WIDTH


def main():
    parser = argparse.ArgumentParser(
        description="Generate Supplementary Table 7 from review.xlsx",
    )
    parser.add_argument(
        "--review",
        default="output/review.xlsx",
        help="Path to sector-split review.xlsx (default: output/review.xlsx)",
    )
    parser.add_argument(
        "--output",
        default="output/supplementary_table7.xlsx",
        help="Output path (default: output/supplementary_table7.xlsx)",
    )
    args = parser.parse_args()

    review_path = Path(args.review)
    output_path = Path(args.output)

    if not review_path.exists():
        raise SystemExit(f"Review file not found: {review_path}")

    review_wb = load_workbook(review_path)
    out_wb = Workbook()
    # Remove the default "Sheet" that openpyxl creates
    out_wb.remove(out_wb.active)

    sheet_names_in_review = set(review_wb.sheetnames)

    for sector in SECTOR_SHEET_ORDER:
        if sector not in sheet_names_in_review:
            continue

        ws_in = review_wb[sector]
        rows = read_review_sheet(ws_in)

        if not rows:
            continue

        # Sort: tech order (by tech_id numeric), then dimension order
        rows.sort(key=lambda r: (
            _tech_sort_key(r.get("tech_id", "")),
            DIM_ORDER.get(r.get("dimension", ""), 99),
        ))

        max_sources = max(len(r.get("sources", [])) for r in rows)

        ws_out = out_wb.create_sheet(title=sector)
        write_data_sheet(ws_out, rows, max_sources)

    if not out_wb.sheetnames:
        raise SystemExit("No matching sector sheets found in review file.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_wb.save(output_path)
    print(f"Written {output_path} ({len(out_wb.sheetnames)} sheets)")


if __name__ == "__main__":
    main()
