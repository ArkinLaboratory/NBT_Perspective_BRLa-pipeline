"""Split a multi-technology Deep Research report into per-technology files.

DR reports from Gemini often cover an entire sector (e.g. "Soils") with
multiple technologies.  M4b expects one report per technology, so this
script splits the composite report along its section boundaries.

Each output file gets:
  - The original report's preamble / framework section (shared context)
  - The technology-specific section
  - The comparative analysis and conclusion (shared context)
  - Only the Works Cited entries actually referenced in the included text

Usage:
    python scripts/split_dr_report.py deep_research_reports/BRLa-v2_Soils.md

Output lands in deep_research_reports/ alongside the original, named:
    {stem}__{slug}.md
e.g. BRLa-v2_Soils__engineered-associative-n-fixing-microbes.md
"""
import re
import sys
from pathlib import Path

TECH_SECTION_RE = re.compile(
    r"^(?:#{1,6}\s+)?\*\*\d+(?:\.\d+)?\\?\.\s+(?:Technology|Tech)\s+\d+:\s+(.+?)\*\*$"
)
SECTION_DIVIDER = "## ---"
DIVIDER_RE = re.compile(r"^#{2,4}\s+---\s*$")

# Gemini DR reports attach citations directly to the end of a sentence, e.g.
# "...of US corn production.7" — so the dot must be allowed to follow a word
# character.  The lookbehind covers everything a sentence can end on: word
# characters, closing brackets, markdown emphasis markers, a percent sign and
# closing quotes ("...a critical \"Biological Ceiling\".30").  Requiring *some*
# such character keeps list markers ("\n7.") and spaced-out prose (". 7") out.
DOT_CITE_RE = re.compile(r"(?<=[\w\)\]\*%\"'’”])\.(\d{1,3})(?!\d)")
# Bracket citations may carry several refs: [7], [3, 5], [3-5], [3–5, 9].
# The trailing `(?!\()` rejects markdown link labels — the summary-matrix tables
# render BRL scores as `**[9](https://...)**`, which are scores, not references.
BRACKET_CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\](?!\()")
RANGE_RE = re.compile(r"^(\d{1,3})\s*[–-]\s*(\d{1,3})$")
# A dot-number whose line begins with nothing but heading markup and digits is a
# section number ("### **2.1 Technology Overview**"), not a citation.
SECTION_NUMBER_PREFIX_RE = re.compile(r"^[#*\s\d]*$")


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:60]


def expand_bracket_refs(group: str) -> set[int]:
    """Expand a bracket citation body into its individual numbers.

    '7' -> {7};  '3, 5' -> {3, 5};  '3-5' / '3–5' -> {3, 4, 5}.
    """
    refs: set[int] = set()
    for part in group.split(","):
        part = part.strip()
        if not part:
            continue
        m = RANGE_RE.match(part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            refs.update(range(lo, hi + 1))
        elif part.isdigit():
            refs.add(int(part))
        else:
            refs.update(int(n) for n in re.findall(r"\d{1,3}", part))
    return refs


def _is_section_number(text: str, dot_pos: int) -> bool:
    """True if the dot at `dot_pos` separates a heading's section number."""
    line_start = text.rfind("\n", 0, dot_pos) + 1
    return bool(SECTION_NUMBER_PREFIX_RE.match(text[line_start:dot_pos]))


def find_referenced_numbers(text: str) -> set[int]:
    """Find all numeric citation markers like .2, [7] or [3-5] in the text."""
    refs: set[int] = set()
    for m in DOT_CITE_RE.finditer(text):
        if _is_section_number(text, m.start()):
            continue
        refs.add(int(m.group(1)))
    for m in BRACKET_CITE_RE.finditer(text):
        refs |= expand_bracket_refs(m.group(1))
    return refs


def parse_sections(lines: list[str]) -> list[dict]:
    """Split the report into logical sections delimited by '## ---'."""
    sections: list[dict] = []
    current_lines: list[str] = []
    for line in lines:
        if DIVIDER_RE.match(line.strip()):
            if current_lines:
                sections.append({"lines": current_lines})
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append({"lines": current_lines})

    for sec in sections:
        sec["text"] = "\n".join(sec["lines"])
        match = None
        for l in sec["lines"]:
            match = TECH_SECTION_RE.match(l.strip())
            if match:
                break
        sec["tech_name"] = match.group(1).strip() if match else None

    return sections


def parse_references(text: str) -> dict[int, str]:
    """Parse the Works Cited block into {number: full_line}."""
    refs: dict[int, str] = {}
    in_refs = False
    for line in text.splitlines():
        if "Works cited" in line or "Works Cited" in line:
            in_refs = True
            continue
        if in_refs:
            m = re.match(r"^(?:>\s*)?(\d{1,3})\.\s+", line.strip())
            if m:
                refs[int(m.group(1))] = line.strip()
    return refs


def split_report(report_path: Path) -> list[Path]:
    text = report_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    refs_block_start = None
    for i, line in enumerate(lines):
        if "Works cited" in line or "Works Cited" in line:
            refs_block_start = i
            break

    body_lines = lines[:refs_block_start] if refs_block_start else lines
    sections = parse_sections(body_lines)
    all_refs = parse_references(text)

    preamble_sections: list[dict] = []
    tech_sections: list[dict] = []
    epilogue_sections: list[dict] = []

    past_techs = False
    found_first_tech = False
    for sec in sections:
        if sec["tech_name"]:
            found_first_tech = True
            past_techs = False
            tech_sections.append(sec)
        elif found_first_tech:
            # Scan the whole section: the heading is not always in the first
            # few lines, and missing it silently merges the epilogue into the
            # last technology's file.
            is_comparative = any(
                "Comparative" in l or "Conclusion" in l
                for l in sec["lines"]
            )
            if is_comparative:
                past_techs = True
            if past_techs:
                epilogue_sections.append(sec)
            else:
                tech_sections[-1]["lines"].extend(sec["lines"])
                tech_sections[-1]["text"] = "\n".join(
                    tech_sections[-1]["lines"]
                )
        else:
            preamble_sections.append(sec)

    preamble_text = ("\n\n" + SECTION_DIVIDER + "\n\n").join(
        s["text"] for s in preamble_sections
    )
    epilogue_text = ("\n\n" + SECTION_DIVIDER + "\n\n").join(
        s["text"] for s in epilogue_sections
    )

    stem = report_path.stem
    out_dir = report_path.parent
    written: list[Path] = []

    for sec in tech_sections:
        slug = slugify(sec["tech_name"])
        combined = (
            preamble_text
            + "\n\n" + SECTION_DIVIDER + "\n\n"
            + sec["text"]
            + "\n\n" + SECTION_DIVIDER + "\n\n"
            + epilogue_text
        )

        used_refs = find_referenced_numbers(combined)
        if all_refs and used_refs:
            ref_lines = [
                all_refs[n] for n in sorted(used_refs) if n in all_refs
            ]
            if ref_lines:
                combined += "\n\n#### **Works cited**\n\n"
                combined += "  \n".join(ref_lines) + "\n"

        out_path = out_dir / f"{stem}__{slug}.md"
        out_path.write_text(combined, encoding="utf-8")
        written.append(out_path)
        print(f"  {out_path.name}  ({len(sec['lines'])} lines, "
              f"{len(used_refs & set(all_refs))} refs)")

    return written


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/split_dr_report.py <report.md>")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    if not report_path.exists():
        print(f"Error: {report_path} not found")
        sys.exit(1)

    print(f"Splitting {report_path.name}...")
    written = split_report(report_path)
    print(f"\nDone — {len(written)} per-technology files written to "
          f"{report_path.parent}/")


if __name__ == "__main__":
    main()
