# BRLa Automation Pipeline

Automated Balanced Readiness Level assessment (BRLa; Vik et al. 2021) for
emerging technologies. The pipeline gathers web evidence, extracts structured
evidence records, assigns Low/Mid/High bins across five readiness dimensions
(TRL, MRL, RRL, ARL, ORL) using two independent LLM raters, and produces a
human-review spreadsheet with flagged disagreements.

---

## 1. Repository layout

```
.
├── run.py                  # CLI entry point — all commands go through here
├── config.yaml             # Model IDs, search settings, paths, thresholds
├── brla/                   # Pipeline modules (M1–M7 + helpers)
│   ├── m1_aliases.py       #   M1: alias/synonym expansion for a technology
│   ├── m2_queries.py       #   M2: search-query generation (called by M3)
│   ├── m3_search.py        #   M3: Tavily web search
│   ├── m4_fetch.py         #   M4: fetch + extract page text (trafilatura)
│   ├── m4b_deepresearch.py #   M4b: ingest a Deep Research report (optional)
│   ├── m5_evidence.py      #   M5: LLM evidence extraction from pages
│   ├── m6_assign.py        #   M6: per-dimension bin assignment (2 raters)
│   ├── m7_merge.py         #   M7: merge rater outputs, flag for review
│   ├── output_writer.py    #   writes master.json + review.xlsx
│   ├── foldback.py         #   reads reviewed xlsx back into master.json
│   ├── config.py           #   config loader (YAML, path resolution)
│   ├── llm.py              #   OpenAI-compatible LLM client + usage tracking
│   └── utils.py            #   shared helpers (slugify, JSON I/O, etc.)
├── prompts/                # Per-dimension assignment prompts (M6)
│   └── assign_{TRL,MRL,RRL,ARL,ORL}.md
├── scripts/                # Standalone analysis/utility scripts
├── input/
│   └── technologies.xlsx   # Input sheet: one row per technology
├── cache/                  # Per-tech checkpoint dirs (auto-created)
│   ├── pages/              #   fetched page text (trafilatura output)
│   └── tavily_pages/       #   raw Tavily snippets (M4 fallback)
├── deep_research_reports/  # Optional: Gemini Deep Research PDFs/text
├── output/
│   ├── master.json         # Full structured output (all techs, all dims)
│   └── review.xlsx         # Human-review spreadsheet
└── .env                    # API keys (not committed — see setup below)
```

**Where files go:**
- **Your input** → `input/technologies.xlsx` (run `init-input` for a template).
- **Deep Research reports** (optional) → `deep_research_reports/`, referenced by
  filename in the `dr_report` column of the input sheet.
- **Pipeline cache** → `cache/{tech_id}/` — one directory per technology,
  containing JSON checkpoints for each module (aliases, search results, evidence,
  assignments, final merged output). Re-running a module skips work that already
  has a checkpoint unless you pass `--force`.
- **Final output** → `output/master.json` and `output/review.xlsx`.

---

## 2. Setup

### 2a. Environment and dependencies

```bash
conda create -n env-brla python=3.12 -y
conda activate env-brla
pip install -r requirements.txt
```

All commands in this README use `conda run -n env-brla` so the right
environment is always selected, even outside an activated shell.

### 2b. API keys

Copy the example and fill in your values:

```bash
cp .env.example .env
```

You need two keys:

| Variable | What it is | Where to get one |
|---|---|---|
| `CBORG_BASE_URL` | Base URL for any OpenAI-compatible chat endpoint | See §2c below |
| `CBORG_API_KEY` | API key for that endpoint | Same provider |
| `TAVILY_API_KEY` | Tavily search API key | [tavily.com](https://tavily.com) — free tier gives 1 000 searches/month |

### 2c. LLM provider setup (without CBorg)

The pipeline talks to LLMs through the **OpenAI Python client** — any service
that exposes an OpenAI-compatible `/v1/chat/completions` endpoint works.
Set `CBORG_BASE_URL` and `CBORG_API_KEY` in `.env` to point at your provider,
then update the model IDs in `config.yaml` to match.

Three practical options:

**Option A — OpenAI directly** (simplest)

```env
CBORG_BASE_URL=https://api.openai.com/v1
CBORG_API_KEY=sk-...          # your OpenAI API key
```
```yaml
# config.yaml — models section
models:
  alias_expander: "gpt-4o-mini"          # cheap, fast (M1)
  extractor: "gpt-4o-mini"               # M5 — mechanical extraction
  primary_rater: "gpt-4o"                # M6 rater A
  second_rater: "gpt-4o"                 # M6 rater B (ideally a different model)
```

**Option B — LiteLLM local proxy** (multi-provider, recommended for mixed models)

LiteLLM lets you route different model strings to different providers
(Anthropic, Google, OpenAI, etc.) through one local endpoint.

```bash
pip install litellm
litellm --config litellm_config.yaml     # see LiteLLM docs for config format
```
```env
CBORG_BASE_URL=http://localhost:4000/v1
CBORG_API_KEY=sk-anything               # LiteLLM accepts any non-empty key by default
```

This is the closest analog to the CBorg setup we use — you can assign e.g. a
Claude model as `primary_rater` and a Gemini model as `second_rater` so the
two M6 raters come from different model families (reducing shared blind spots).

**Option C — Ollama** (free, local, no API key)

Suitable for testing or low-stakes runs. Large local models may be too slow
for batch use and may not produce reliable structured JSON for M5/M6.

```env
CBORG_BASE_URL=http://localhost:11434/v1
CBORG_API_KEY=ollama                     # Ollama ignores this but the client requires it
```
```yaml
models:
  alias_expander: "llama3.1:8b"
  extractor: "llama3.1:8b"
  primary_rater: "llama3.1:70b"
  second_rater: "llama3.1:70b"
```

After choosing a provider, verify connectivity:

```bash
conda run -n env-brla python run.py list-models
```

This prints every model ID available at your endpoint. Pick IDs from this list
and paste them into `config.yaml` under `models:`.

---

## 3. Quick start

```bash
# 1. Generate a template input sheet
conda run -n env-brla python run.py init-input

# 2. Edit input/technologies.xlsx — add your technologies (one row each).
#    Required columns: name, description.
#    Optional: dr_report (filename of a Deep Research report in deep_research_reports/).

# 3. Run the full pipeline for one technology
conda run -n env-brla python run.py pipeline --tech t001-nofence

# 4. Run the full pipeline for ALL technologies in the input sheet
conda run -n env-brla python run.py pipeline

# 5. Check checkpoint status across all techs
conda run -n env-brla python run.py status
```

Output lands in `output/review.xlsx` (the review spreadsheet) and
`output/master.json` (structured data for programmatic use).

---

## 4. Command reference

All commands go through `run.py`:

```
python run.py <command> [--tech ID] [--force] [--skip-search] [--verbose]
                        [--config PATH] [--workers N]
```

### Pipeline commands

| Command | What it does |
|---|---|
| `pipeline` | Full end-to-end: M1 → M3 → M4 → M5 → M6 → M7 → write output |
| `gather` | Evidence-gathering half only: M1 → M3 → M4 → M5 (no assignment) |

### Individual module commands

Run these to re-do a single step (usually with `--force`):

| Command | Module | What it does |
|---|---|---|
| `aliases` | M1 | Expand technology name → aliases and synonyms |
| `search` | M3 | Web search via Tavily using generated queries |
| `ingest-dr` | M4b | Ingest a Deep Research report (needs `dr_report` column) |
| `fetch` | M4 | Download and extract text from all known URLs |
| `extract` | M5 | LLM-based evidence extraction from fetched pages |
| `assign` | M6 | Per-dimension bin assignment by two LLM raters |
| `merge` | M7 | Merge rater outputs, compute confidence, flag disagreements |

### Output and utility commands

| Command | What it does |
|---|---|
| `write-output` | Rebuild `output/master.json` and `output/review.xlsx` from checkpoints |
| `foldback` | Read human edits from `review.xlsx` back into `master.json` |
| `status` | Show per-tech checkpoint status + cumulative token usage |
| `list-models` | Print model IDs available at your LLM endpoint |
| `init-input` | Write a template `input/technologies.xlsx` |

### Global flags

| Flag | Effect |
|---|---|
| `--tech ID` | Scope to a single technology (e.g. `--tech t001-nofence`). Default: all. |
| `--force` | Recompute even if a checkpoint already exists for that module. |
| `--skip-search` | Skip Tavily search in `gather`/`pipeline` (Deep-Research-only mode). |
| `--config PATH` | Use an alternate config file (for A/B experiments with separate cache trees). |
| `--workers N` | Parallel workers for `pipeline`/`gather`. Default 1 (serial). |
| `--verbose` | Print full tracebacks on errors. |

---

## 5. Key scripts

These standalone scripts in `scripts/` operate on the pipeline's outputs.
They are not part of the pipeline itself — run them after a pipeline run
completes.

### `split_review_by_sector.py`

Splits `review.xlsx` into a multi-sheet workbook with one sheet per sector
(sectors are assigned in `technologies.xlsx`), plus an "All" sheet. All
formatting, hyperlinks, and filters are preserved.

```bash
conda run -n env-brla python scripts/split_review_by_sector.py \
    --review output/review.xlsx

# Custom output path:
conda run -n env-brla python scripts/split_review_by_sector.py \
    --review output/review.xlsx \
    --output output/review-sector-wise.xlsx
```

### `compare_assessments.py`

Compares the pipeline's automated assignments against a manually-performed
BRLa assessment and generates a self-contained HTML dashboard showing
agreement/disagreement patterns with auto-generated insights.

```bash
conda run -n env-brla python scripts/compare_assessments.py \
    --manual path/to/manual_brla_draft.xlsx
```

The pipeline's `output/review.xlsx` is used automatically. The HTML report is
written alongside the manual file.

### `add_manual_comparison.py`

Adds a `manual_compare` column to `review.xlsx` showing per-rater agreement
with a manual assessment. Produces a new file rather than modifying the
original.

```bash
conda run -n env-brla python scripts/add_manual_comparison.py \
    --manual path/to/manual_brla_draft.xlsx \
    --review output/review.xlsx \
    --output output/review_with-manual-compare.xlsx
```

---

## 6. Typical workflow

```
1.  Populate input/technologies.xlsx
2.  python run.py pipeline                    # full automated run
3.  python run.py status                      # verify all checkpoints landed
4.  Open output/review.xlsx — review flagged rows, edit bins/notes
5.  python run.py foldback                    # fold human edits back into master.json
6.  python scripts/split_review_by_sector.py --review output/review.xlsx
```

To re-run a single module for one tech (e.g., re-extract evidence after
adjusting `max_pages_to_extract` in `config.yaml`):

```bash
conda run -n env-brla python run.py extract --tech t003-scp --force
conda run -n env-brla python run.py assign  --tech t003-scp --force
conda run -n env-brla python run.py merge   --tech t003-scp --force
conda run -n env-brla python run.py write-output
```

Downstream modules read from the previous module's checkpoint, so you only
need to re-run from the step you changed onward.
