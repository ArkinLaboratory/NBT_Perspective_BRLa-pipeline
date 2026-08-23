# BRLa Automation Pipeline

Automated Balanced Readiness Level assessment (Vik et al. 2021) for a batch
of emerging technologies: web evidence gathering -> curated evidence packs ->
Low/Mid/High bin assignment on TRL/MRL/RRL/ARL/ORL with two-model
inter-rater checks -> human review spreadsheet.

## Start here

1. `notes/background.md` — the BRLa framework and binning rubrics
2. `notes/plan.md` — architecture, module specs, schemas

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python run.py list-models
# paste real model IDs into config.yaml
python run.py init-input
python run.py gather --tech t001-feed-additives
```

**To run the entire pipeline with multiple parallel threads:**

```bash
python run.py pipeline --workers 5
```

**To split up the `review.xlsx` output into sector-wise sheets:**

```bash
conda run -n env-brla python scripts/split_review_by_sector.py \
    --review output/review.xlsx \
    --technologies input/technologies.xlsx \
    --output output/review-sector-wise.xlsx
```