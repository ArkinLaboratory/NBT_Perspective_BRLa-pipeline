---
id: 315u1i9u09kyuvnpyau193t
title: Plan
desc: ''
updated: 1785191668064
created: 1784069470547
---
# PLAN.md — BRLa Automation Pipeline Architecture

Goal: for each technology in an input list (~80, one-time batch), gather and
curate online evidence, assign Low/Mid/High bins on five readiness dimensions
(TRL, MRL, RRL, ARL, ORL), attach confidence signals, and emit a
human-reviewable spreadsheet plus a provenance-complete master JSON.

Design principles:
- **Plain Python, no orchestration framework.** Linear DAG with the filesystem
  as state manager. Every module writes a JSON checkpoint; reruns skip
  completed work. Any intermediate can be inspected in a text editor.
- **Separate fact-finding from judgment.** Search/fetch/extract modules never
  assign bins; assignment modules never see the open web, only the curated
  evidence pack.
- **Human attention is the scarce resource.** The pipeline's job is to make
  most rows skimmable and flag the contestable ones (`needs_review`).

## Pipeline

```bash
input/technologies.xlsx  (tech_id, name, description, [dr_report])
      │
      ▼
[M1] Alias expansion (LLM, cheap model)      → cache/{tech}/aliases.json
[M2] Query generation (templates × aliases)  → in-memory
[M3] Web search (Tavily, dedup)              → cache/{tech}/search_results.json
[M4b] Deep Research report ingestion         → cache/{tech}/dr_sources.json
      │   (extract cited URLs + synthesized claims from exported DR reports)
      ▼
[M4] Fetch + extract (trafilatura)           → cache/pages/{sha1}.json  (GLOBAL cache)
      │
      ▼
[M5] Evidence extraction (LLM, cheap model)  → cache/{tech}/evidence.json
[M6] RL assignment ×5 dims ×2 rater models   → cache/{tech}/assignments.json
[M7] Confidence merge + flagging             → cache/{tech}/final.json
      │
      ▼
output/master.json  +  output/review.xlsx
```

## Module specs

### M1 — Alias expansion (IMPLEMENTED)
One LLM call per technology. Input: name + description. Output JSON:
`{"aliases": [...], "companies": [...], "category_terms": [...]}`.
Purpose: recall. Same tech appears under product/company/generic/academic names.

### M2 — Query generation (IMPLEMENTED)
Deterministic templates per dimension (see `brla/m2_queries.py`), instantiated
with the primary name + top aliases. ~5–7 queries per dimension. No LLM call.

### M3 — Web search (IMPLEMENTED)
Tavily API (free tier 1k/mo; batch of 80 techs ≈ 2,400 calls → spread over
free tier or ~$12 paid). Results deduped by URL across dimensions; each URL
remembers which dimensions' queries surfaced it.

### M4 — Fetch + extract (IMPLEMENTED)
trafilatura fetch/extract, global URL-hash cache shared across ALL
technologies (domain clustering → expect 30–40% cache hits after the first
~20 techs). Failures recorded, never retried within a run. Polite delay
between fetches to the same host.

### M4b — Deep Research ingestion (IMPLEMENTED)
Optional per technology. Reads an exported Gemini Deep Research report
(.md / .txt; .pdf via pypdf if installed) from `deep_research_reports/`.
Two outputs:
1. Cited URLs → merged into the tech's URL pool → fetched by M4 like any
   search hit (provenance points at primary sources, not at the DR report).
2. The report text itself → stored as evidence records tagged
   `source_type: "dr_synthesis"` with quality capped at "med" (it is
   LLM-generated secondary synthesis, not primary evidence).

### M5 — Evidence extraction (TO BUILD — see HANDOFF.md)
Cheap model (haiku/flash class) reads each fetched page once and emits zero
or more evidence records:

```json
{
  "evidence_id": "t012_e034",
  "url": "...", "title": "...", "pub_date": "2024-03-01|null",
  "snippet": "<verbatim-ish quote or tight paraphrase, <=60 words>",
  "dimensions": ["MRL", "ORL"],
  "bin_signal": {"MRL": "Mid", "ORL": null},
  "source_type": "news|company|regulator|academic|market_report|forum|dr_synthesis",
  "source_quality": "high|med|low"
}
```

One page can yield records for multiple dimensions (fetch once, reuse
everywhere). Pages with no relevant content yield an empty list — record that
too, so reruns skip them. This is the highest-token module (~100–200k input
tokens/tech) → cheapest model.

### M6 — RL assignment (TO BUILD)
Per dimension, per rater model (2 models via CBorg): input is BACKGROUND.md's
rubric for that dimension + the curated evidence records for that dimension
only (~5–15k tokens). Output:

```json
{
  "dimension": "ARL", "rater": "<model-id>",
  "bin": "Mid", "level_estimate": "5-6|null",
  "rationale": "2-3 sentences citing evidence_ids",
  "evidence_ids": ["t012_e034", ...],
  "evidence_gap": false
}
```

Rules to encode in the prompt: top-down "highest supportable claim" logic;
absence of evidence ≠ acceptance (pitfall #2); no cross-dimension anchoring;
`evidence_gap: true` whenever the bin rests on inference rather than direct
evidence.

### M7 — Confidence merge (TO BUILD)
No verbalized LLM confidence (poorly calibrated). Two independent signals:
1. `evidence_strength` (0–1, deterministic): function of n independent
   sources, source_quality mix, recency, and directness (bin_signal present
   vs inferred).
2. `rater_agreement` (bool): do the two models' bins match?

Flag `needs_review = (not rater_agreement) or evidence_strength < THRESHOLD
or evidence_gap`. Threshold default 0.4, tune after first 10 techs.

### Output writer (TO BUILD)
- `output/master.json`: everything, provenance-complete, plus empty
  `human_bin`, `human_notes`, `reviewed` fields per tech-dimension.
- `output/review.xlsx`: 400 rows (80 techs × 5 dims), one row per
  tech-dimension. Columns: tech_id, tech_name, dimension, bin, level_est,
  rationale, evidence_strength, n_sources, top_sources (hyperlinked, up to 5,
  each with one-line snippet), rater_2_bin, agreement, evidence_gap,
  needs_review, human_bin, human_notes, reviewed. Sorted needs_review desc,
  evidence_strength asc.
- A fold-back script reads the reviewed xlsx and writes human fields into
  master.json.

## CBorg / LiteLLM specifics
- Single OpenAI-compatible client, `base_url` from `CBORG_BASE_URL`,
  key from `CBORG_API_KEY` (see `brla/llm.py`).
- **First task on a new machine:** `python run.py list-models` and copy real
  model IDs into `config.yaml` (LiteLLM deployments name models differently).
- Batch APIs are assumed unavailable through the proxy → sequential calls
  with retry/backoff. Fine at this scale.
- Multi-model raters = two model strings in config, same client.

## Cost envelope (80 techs, no batch discount)
| Item | Est. |
|---|---|
| Search (Tavily) | $0–12 |
| Fetch (local) | $0 |
| M1 + M5 (cheap model) | ~$10–20 |
| M6 ×2 raters (mid-tier models) | ~$20–40 |
| **Total** | **~$30–70** |

DR-report ingestion substitutes for search+fetch tokens on techs where a
report exists, and costs only M5 extraction on the report text.

## Build order (remaining)
1. M5 evidence extraction + its prompt file
2. M6 assignment + per-dimension prompt files (rubrics from BACKGROUND.md)
3. M7 merge + flagging
4. Excel writer + fold-back script
5. Smoke test on 2–3 technologies end-to-end, inspect every checkpoint
6. Tune evidence_strength threshold, then run the batch


Foldba