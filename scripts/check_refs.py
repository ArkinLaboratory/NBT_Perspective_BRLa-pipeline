#!/usr/bin/env python
"""Verify that `file.py:LINE` references inside notes/ still point where they did.

Hand-maintained review docs rot silently: editing a module shifts every line
number cited elsewhere, and nothing complains. On 2026-07-27 a single session's
edits invalidated 20 of 21 references in notes/adversarial-review.opus-4-8.00.md
without touching that file at all.

The checker records a fingerprint of each referenced source line, then reports
when the line a document points at has changed underneath it.

    python scripts/check_refs.py            # verify against the snapshot
    python scripts/check_refs.py --bless    # accept current state as correct
    python scripts/check_refs.py --doc notes/adversarial-review.opus-4-8.00.md

Exit status is 1 if anything is stale, missing, or out of range, so this can be
wired into a pre-commit hook later.

Stdlib only. No third-party imports.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "scripts" / "ref-snapshot.json"

# `brla/m7_merge.py:59-61`, `m6_assign.py:80`, `llm.py:81,101`
REF_RE = re.compile(
    r"`([A-Za-z0-9_./]+\.py):(\d+(?:\s*[-,]\s*\d+)*)`"
)
# Bare "(line 147)" — cannot be machine-anchored to a file.
BARE_LINE_RE = re.compile(r"\(line (\d+)\)")

STATUS_ORDER = ["DRIFTED", "OUT_OF_RANGE", "MISSING_FILE", "AMBIGUOUS", "NEW", "OK"]


def find_source(rel: str) -> Path | None:
    """Resolve a cited path, which may be bare (`m4_fetch.py`) or rooted."""
    direct = ROOT / rel
    if direct.is_file():
        return direct
    if "/" not in rel:
        hits = [p for p in ROOT.rglob(rel)
                if ".git" not in p.parts and "__pycache__" not in p.parts]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None  # ambiguous
    return None


def fingerprint(line: str) -> str:
    """Hash the line's content, ignoring leading/trailing whitespace changes."""
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:12]


def parse_refs(doc: Path) -> list[tuple[str, int]]:
    """Extract (relative_path, line_number) pairs cited in a document."""
    refs = []
    for match in REF_RE.finditer(doc.read_text(encoding="utf-8")):
        rel, numbers = match.group(1), match.group(2)
        for part in re.split(r"[-,]", numbers):
            part = part.strip()
            if part.isdigit():
                refs.append((rel, int(part)))
    return refs


def check_doc(doc: Path, snapshot: dict, bless: bool) -> list[dict]:
    results = []
    for rel, lineno in sorted(set(parse_refs(doc))):
        key = f"{doc.relative_to(ROOT)}::{rel}:{lineno}"
        src = find_source(rel)

        if src is None:
            status, content = ("AMBIGUOUS" if "/" not in rel and list(ROOT.rglob(rel))
                               else "MISSING_FILE"), ""
        else:
            lines = src.read_text(encoding="utf-8").splitlines()
            if lineno > len(lines) or lineno < 1:
                status, content = "OUT_OF_RANGE", ""
            else:
                content = lines[lineno - 1].strip()
                fp = fingerprint(content)
                if bless:
                    snapshot[key] = fp
                    status = "OK"
                elif key not in snapshot:
                    status = "NEW"
                elif snapshot[key] != fp:
                    status = "DRIFTED"
                else:
                    status = "OK"

        results.append({"key": key, "ref": f"{rel}:{lineno}",
                        "status": status, "content": content[:58]})
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", action="append",
                    help="document to check (default: every .md under notes/)")
    ap.add_argument("--bless", action="store_true",
                    help="record the current state as correct")
    ap.add_argument("--quiet", action="store_true",
                    help="only print problems")
    args = ap.parse_args()

    docs = ([ROOT / d for d in args.doc] if args.doc
            else sorted((ROOT / "notes").glob("*.md")))
    docs = [d for d in docs if d.is_file()]
    if not docs:
        print("No documents to check.")
        return 0

    first_run = not SNAPSHOT.exists()
    snapshot = ({} if first_run
                else json.loads(SNAPSHOT.read_text(encoding="utf-8")))

    all_results, bare = [], []
    for doc in docs:
        results = check_doc(doc, snapshot, args.bless)
        if results:
            all_results.append((doc, results))
        n_bare = len(BARE_LINE_RE.findall(doc.read_text(encoding="utf-8")))
        if n_bare:
            bare.append((doc, n_bare))

    # On a first run there is nothing to compare against, so an unrecorded
    # reference is expected rather than a failure.
    ignorable = {"NEW"} if first_run else set()

    problems = 0
    for doc, results in all_results:
        bad = [r for r in results if r["status"] not in ignorable | {"OK"}]
        problems += len(bad)
        shown = results if not args.quiet else bad
        if not shown:
            continue
        print(f"\n{doc.relative_to(ROOT)}")
        for r in sorted(shown, key=lambda r: STATUS_ORDER.index(r["status"])):
            mark = "  " if r["status"] == "OK" else "->"
            print(f" {mark} {r['status']:<13} {r['ref']:<28} {r['content']}")

    total = sum(len(r) for _, r in all_results)
    print(f"\n{total - problems}/{total} references resolve unchanged")

    for doc, n in bare:
        print(f"note: {doc.relative_to(ROOT)} has {n} bare '(line N)' reference(s) "
              f"that cannot be checked — prefer `file.py:N`")

    if args.bless:
        SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"blessed {len(snapshot)} references -> "
              f"{SNAPSHOT.relative_to(ROOT)}")
        return 0

    if first_run:
        print("\nNo snapshot yet — run with --bless to record the current state.")
    elif problems:
        print("\nDRIFTED = the cited line changed; re-read it and update the doc, "
              "then re-run with --bless.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
