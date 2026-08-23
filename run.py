#!/usr/bin/env python
"""BRLa pipeline CLI.

Implemented commands (M1-M4b):
  list-models            Show model IDs available through the CBorg proxy
  init-input             Write a template input/technologies.xlsx
  aliases  --tech ID     M1 alias expansion (all techs if --tech omitted)
  search   --tech ID     M3 Tavily search
  ingest-dr --tech ID    M4b ingest the Deep Research report named in the
                         input sheet's dr_report column
  fetch    --tech ID     M4 fetch+extract all known URLs
  extract  --tech ID     M5 evidence extraction from fetched pages
  assign   --tech ID     M6 per-dimension assignment with two raters
  merge    --tech ID     M7 confidence merge + review flagging
  write-output           Write output/master.json + output/review.xlsx
  foldback               Read reviewed xlsx, update master.json with human fields
  gather   --tech ID     M1 -> (M4b if dr_report set) -> M3 -> M4 -> M5
  pipeline --tech ID     gather + M6 + M7 + write-output (full pipeline)
  status                 Per-tech checkpoint overview + token usage

Global flags: --tech ID, --force, --skip-search, --verbose,
              --config PATH (alternate config; see config.b.yaml),
              --workers N (parallel workers for pipeline/gather; default 1)
"""
import argparse
import multiprocessing
import random
import sys
import time
import traceback

from tqdm import tqdm

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from brla import llm, m1_aliases, m3_search, m4_fetch, m4b_deepresearch, m5_evidence, m6_assign, m7_merge, output_writer, foldback
from brla.config import load_config
from brla.utils import read_json, slugify, tech_dir


def load_techs(cfg, only_tech=None) -> list[dict]:
    path = cfg["paths"]["input_file"]
    if not path.exists():
        sys.exit(f"Input file missing: {path}\nRun `python run.py init-input` "
                 "for a template.")
    df = pd.read_excel(path).fillna("")
    if "tech_id" not in df.columns:
        df["tech_id"] = [f"t{i:03d}-{slugify(n)}" for i, n in enumerate(df["name"], 1)]
    techs = df.to_dict(orient="records")
    if only_tech:
        techs = [t for t in techs if t["tech_id"] == only_tech]
        if not techs:
            sys.exit(f"tech_id {only_tech!r} not found in input sheet.")
    return techs


def cmd_init_input(cfg, _args):
    path = cfg["paths"]["input_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"tech_id": "t001-nofence", "name": "Nofence",
         "description": "Virtual fencing system for livestock (goats, sheep, "
                        "cattle) using GPS collars, audio cues and mild "
                        "electric pulses; Norwegian company Nofence AS.",
         "dr_report": ""},
    ]).to_excel(path, index=False)
    print(f"Template written: {path}")


def _print_assignments(result, write=print):
    write(f"\n  {result['tech_id']} assignments:")
    for a in result["assignments"]:
        rater_short = a["rater"].split("/")[-1][:20]
        gap = " [EVIDENCE GAP]" if a.get("evidence_gap") else ""
        level = f" (est. {a['level_estimate']})" if a.get("level_estimate") else ""
        err = " !! ERROR" if a.get("error") else ""
        write(f"    {a['dimension']:>3} {rater_short:<22} → "
              f"{a.get('bin') or '???':<4}{level}{gap}{err}")


def _print_final(result, write=print):
    write(f"\n  {result['tech_id']} final (threshold={result['review_threshold']}):")
    for dim, d in result["dimensions"].items():
        flags = []
        if d["needs_review"]:
            flags.append("REVIEW")
        if d["evidence_gap"]:
            flags.append("GAP")
        if not d["rater_agreement"]:
            flags.append("DISAGREE")
        if d.get("degraded"):
            flags.append("SINGLE RATER")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        write(f"    {dim:>3} → {d['bin'] or '???':<4}  "
              f"strength={d['evidence_strength']:.2f}  "
              f"n={d['n_evidence_records']:<3}{flag_str}")


def _gather_one_tech(cfg, client, tech, args, pbar=None):
    """Run M1 -> M4b -> M3 -> M4 -> M5 for a single technology."""
    w = tqdm.write if pbar else print
    _status = pbar.set_postfix_str if pbar else lambda _: None

    _status("M1 aliases")
    aliases = m1_aliases.run(cfg, client, tech, force=args.force)
    w(f"  M1 aliases: {aliases['aliases']}")

    if tech.get("dr_report"):
        _status("M4b DR ingest")
        dr = m4b_deepresearch.run(cfg, tech, tech["dr_report"],
                                  force=args.force)
        w(f"  M4b DR report: {dr['n_cited_urls']} cited URLs")

    if not args.skip_search:
        _status("M3 search")
        s = m3_search.run(cfg, tech, aliases, force=args.force)
        n_bad = s.get("n_failed_queries", 0)
        w(f"  M3 search: {s['n_unique_urls']} unique URLs "
          f"from {s['n_queries']} queries"
          + (f" ({n_bad} QUERIES FAILED)" if n_bad else ""))

    _status("M4 fetch")
    f = m4_fetch.run(cfg, tech, force=args.force)
    w(f"  M4 fetch: {f['n_ok']}/{f['n_urls']} ok "
      f"({f['n_cache_hits']} cache hits)")

    _status("M5 evidence")
    on_page = (
        lambda i, n: pbar.set_postfix_str(f"M5 {i}/{n} pages")
    ) if pbar else None
    ev = m5_evidence.run(cfg, client, tech, force=args.force,
                         on_page=on_page)
    w(f"  M5 evidence: {ev['n_records']} records from "
      f"{ev['n_pages_processed']} pages")


def cmd_gather(cfg, args, techs):
    client = llm.get_client(cfg)
    pbar = tqdm(techs, unit="tech")
    for tech in pbar:
        pbar.set_description(tech["tech_id"])
        tqdm.write(f"\n=== {tech['tech_id']} : {tech['name']} ===")
        try:
            _gather_one_tech(cfg, client, tech, args, pbar=pbar)
        except Exception as e:  # noqa: BLE001 - keep the batch alive
            tqdm.write(f"  !! FAILED: {e}")
            if args.verbose:
                traceback.print_exc()
    pbar.close()
    print("\n" + llm.usage_report())


# ---------------------------------------------------------------------------
# Parallel pipeline: per-tech worker + orchestrator
# ---------------------------------------------------------------------------

def _pipeline_worker(task):
    """Full pipeline worker: M1 → M4b → M3 → M4 → M5 → M6 → M7.

    Runs in a child process. Each module has its own try/except so a
    failure in one doesn't prevent later modules from running (prior
    checkpoints may exist).
    """
    cfg, tech, force, skip_search = task
    tech_id = tech["tech_id"]
    msgs, errors = [], []
    assign_result = None
    merge_result = None
    fetch_stats = None

    client = llm.get_client(cfg)

    # -- M1 aliases --
    try:
        aliases = m1_aliases.run(cfg, client, tech, force=force)
        msgs.append(f"M1: {aliases['aliases']}")
    except Exception as e:
        aliases = {"aliases": []}
        errors.append(f"M1: {e}")

    # -- M4b DR ingest --
    if tech.get("dr_report"):
        try:
            dr = m4b_deepresearch.run(cfg, tech, tech["dr_report"],
                                      force=force)
            msgs.append(f"M4b: {dr['n_cited_urls']} cited URLs")
        except Exception as e:
            errors.append(f"M4b: {e}")

    # -- M3 search --
    if not skip_search:
        time.sleep(random.uniform(0, 3))
        try:
            s = m3_search.run(cfg, tech, aliases, force=force)
            n_bad = s.get("n_failed_queries", 0)
            msg = (f"M3: {s['n_unique_urls']} URLs "
                   f"from {s['n_queries']} queries")
            if n_bad:
                msg += f" ({n_bad} FAILED)"
            msgs.append(msg)
        except Exception as e:
            errors.append(f"M3: {e}")

    # -- M4 fetch --
    try:
        f = m4_fetch.run(cfg, tech, force=force)
        fetch_stats = {"n_ok": f["n_ok"], "n_urls": f["n_urls"],
                       "n_cache_hits": f["n_cache_hits"]}
        msgs.append(f"M4: {f['n_ok']}/{f['n_urls']} ok "
                    f"({f['n_cache_hits']} cache hits)")
    except Exception as e:
        errors.append(f"M4: {e}")

    # -- M5 evidence --
    try:
        ev = m5_evidence.run(cfg, client, tech, force=force)
        msgs.append(f"M5: {ev['n_records']} records from "
                     f"{ev['n_pages_processed']} pages")
    except Exception as e:
        errors.append(f"M5: {e}")

    # -- M6 assign --
    try:
        assign_result = m6_assign.run(cfg, client, tech, force=force)
    except Exception as e:
        errors.append(f"M6: {e}")

    # -- M7 merge --
    try:
        merge_result = m7_merge.run(cfg, tech, force=force)
    except Exception as e:
        errors.append(f"M7: {e}")

    return {
        "tech_id": tech_id, "msgs": msgs, "errors": errors,
        "assign": assign_result, "merge": merge_result,
        "fetch_stats": fetch_stats,
        "usage": llm.get_usage(),
    }


def _pipeline_parallel(cfg, args, techs, all_techs):
    """Run the full pipeline with N parallel workers, one tech per worker."""
    n_workers = args.workers
    n_techs = len(techs)
    usage_snapshots = []
    all_fetch_stats = []

    print(f"\n=== Parallel pipeline: {n_techs} techs, "
          f"{n_workers} workers ===\n")

    tasks = [(cfg, t, args.force, args.skip_search) for t in techs]
    with multiprocessing.Pool(n_workers) as pool:
        pbar = tqdm(
            pool.imap_unordered(_pipeline_worker, tasks),
            total=n_techs, unit="tech", desc="Pipeline",
        )
        for r in pbar:
            pbar.set_postfix_str(r["tech_id"])
            for m in r["msgs"]:
                tqdm.write(f"  {r['tech_id']}: {m}")
            for e in r["errors"]:
                tqdm.write(f"  {r['tech_id']}: !! {e}")
            if r["assign"]:
                _print_assignments(r["assign"], write=tqdm.write)
            if r["merge"]:
                _print_final(r["merge"], write=tqdm.write)
            if r["fetch_stats"]:
                all_fetch_stats.append(
                    (r["tech_id"], r["fetch_stats"]))
            usage_snapshots.append(r["usage"])
        pbar.close()

    # -- Fetch health check --
    if all_fetch_stats:
        total_ok = sum(s["n_ok"] for _, s in all_fetch_stats)
        total_urls = sum(s["n_urls"] for _, s in all_fetch_stats)
        rate = total_ok / total_urls if total_urls else 0
        print(f"\nFetch health: {total_ok}/{total_urls} ok "
              f"({rate:.0%} success)")
        if rate < 0.75:
            print("  !! WARNING: fetch success rate below 75% — "
                  "concurrent fetches may be triggering rate limits. "
                  "Consider reducing --workers.")

    # -- Write output --
    llm.merge_usage(usage_snapshots)
    out = output_writer.run(cfg, all_techs)
    print(f"\nWrote {out['n_rows']} rows → {out['review_xlsx']}")
    print(llm.usage_report())


def cmd_status(cfg, _args, techs):
    checkpoints = ["aliases.json", "dr_sources.json", "search_results.json",
                   "fetch_manifest.json", "evidence.json",
                   "assignments.json", "final.json"]
    print(f"{'tech_id':<28}" + "".join(f"{c.split('.')[0][:10]:>12}" for c in checkpoints))
    for tech in techs:
        tdir = tech_dir(cfg, tech["tech_id"])
        row = "".join(
            f"{'YES' if (tdir / c).exists() else '-':>12}" for c in checkpoints
        )
        print(f"{tech['tech_id']:<28}{row}")
    print("\n" + llm.usage_report())


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=[
        "list-models", "init-input", "aliases", "search", "ingest-dr",
        "fetch", "extract", "assign", "merge", "write-output", "foldback",
        "gather", "pipeline", "status"])
    p.add_argument("--tech", help="single tech_id (default: all)")
    p.add_argument("--config", default=None,
                   help="alternate config file (e.g. config.b.yaml for an "
                        "isolated A/B run against a separate cache tree)")
    p.add_argument("--force", action="store_true",
                   help="recompute even if checkpoint exists")
    p.add_argument("--skip-search", action="store_true",
                   help="gather without Tavily (DR-report-only mode)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--workers", type=int, default=1,
                   help="parallel workers for pipeline/gather (default 1 = serial)")
    args = p.parse_args()

    cfg = load_config(args.config) if args.config else load_config()
    if args.config:
        print(f"[config: {args.config} -> cache={cfg['paths']['cache_dir'].name}/ "
              f"output={cfg['paths']['output_dir'].name}/]")

    if args.command == "list-models":
        for m in llm.list_models(cfg):
            print(m)
        return
    if args.command == "init-input":
        cmd_init_input(cfg, args)
        return

    techs = load_techs(cfg, args.tech)
    # `--tech` scopes which technologies get COMPUTED. It must never scope which
    # ones appear in the outputs: master.json and review.xlsx are rebuilt whole
    # every time, so handing output_writer the filtered list silently deletes
    # every other tech's rows from them. Always aggregate the full list.
    all_techs = load_techs(cfg, None)

    if args.command == "status":
        cmd_status(cfg, args, techs)
    elif args.command == "gather":
        cmd_gather(cfg, args, techs)
    elif args.command == "aliases":
        client = llm.get_client(cfg)
        for t in techs:
            a = m1_aliases.run(cfg, client, t, force=args.force)
            print(f"{t['tech_id']}: {a['aliases']}")
        print(llm.usage_report())
    elif args.command == "search":
        for t in techs:
            aliases = read_json(tech_dir(cfg, t["tech_id"]) / "aliases.json") or \
                {"aliases": []}
            s = m3_search.run(cfg, t, aliases, force=args.force)
            print(f"{t['tech_id']}: {s['n_unique_urls']} URLs")
    elif args.command == "ingest-dr":
        for t in techs:
            if not t.get("dr_report"):
                print(f"{t['tech_id']}: no dr_report column value, skipping")
                continue
            dr = m4b_deepresearch.run(cfg, t, t["dr_report"], force=args.force)
            print(f"{t['tech_id']}: {dr['n_cited_urls']} cited URLs")
    elif args.command == "fetch":
        for t in techs:
            f = m4_fetch.run(cfg, t, force=args.force)
            print(f"{t['tech_id']}: {f['n_ok']}/{f['n_urls']} ok")
    elif args.command == "extract":
        client = llm.get_client(cfg)
        for t in techs:
            ev = m5_evidence.run(cfg, client, t, force=args.force)
            print(f"{t['tech_id']}: {ev['n_records']} evidence records "
                  f"from {ev['n_pages_processed']} pages")
        print(llm.usage_report())
    elif args.command == "assign":
        client = llm.get_client(cfg)
        for t in techs:
            try:
                a = m6_assign.run(cfg, client, t, force=args.force)
                _print_assignments(a)
            except Exception as e:  # noqa: BLE001 - keep the batch alive
                print(f"{t['tech_id']}: FAILED — {e}")
                if args.verbose:
                    traceback.print_exc()
        print(llm.usage_report())
    elif args.command == "merge":
        for t in techs:
            try:
                f = m7_merge.run(cfg, t, force=args.force)
                _print_final(f)
            except Exception as e:  # noqa: BLE001 - keep the batch alive
                print(f"{t['tech_id']}: FAILED — {e}")
                if args.verbose:
                    traceback.print_exc()
    elif args.command == "write-output":
        out = output_writer.run(cfg, all_techs)
        print(f"Wrote {out['n_rows']} rows ({out['n_techs']} techs)")
        print(f"  {out['master_json']}")
        print(f"  {out['review_xlsx']}")
    elif args.command == "foldback":
        fb = foldback.run(cfg)
        print(f"Updated {fb['n_updated']} entries from {fb['n_reviewed_rows']} "
              f"reviewed rows")
        print(f"  Backup: {fb['backup']}")
    elif args.command == "pipeline":
        if args.workers > 1 and len(techs) > 1:
            _pipeline_parallel(cfg, args, techs, all_techs)
        else:
            client = llm.get_client(cfg)
            pbar = tqdm(techs, unit="tech")
            for tech in pbar:
                pbar.set_description(tech["tech_id"])
                tqdm.write(f"\n=== {tech['tech_id']} : {tech['name']} ===")
                try:
                    _gather_one_tech(cfg, client, tech, args, pbar=pbar)

                    pbar.set_postfix_str("M6 assign")
                    a = m6_assign.run(cfg, client, tech, force=args.force)
                    _print_assignments(a, write=tqdm.write)

                    pbar.set_postfix_str("M7 merge")
                    merge_result = m7_merge.run(cfg, tech, force=args.force)
                    _print_final(merge_result, write=tqdm.write)
                except Exception as e:  # noqa: BLE001 - keep the batch alive
                    tqdm.write(f"  !! FAILED: {e}")
                    if args.verbose:
                        traceback.print_exc()
            pbar.close()
            out = output_writer.run(cfg, all_techs)
            print(f"\nWrote {out['n_rows']} rows → {out['review_xlsx']}")
            print(llm.usage_report())


if __name__ == "__main__":
    main()
