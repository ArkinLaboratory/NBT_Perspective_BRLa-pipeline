---
id: 9v2pwxhvtjynmj03uwgf4n1
title: Background
desc: ''
updated: 1784069470511
created: 1784069470511
---
# BACKGROUND.md — The BRLa Framework and Binning Scheme

This document is the domain reference for the pipeline. Read it before touching
prompts or rubrics. It summarizes (in our own words) the methodology from:

> Vik, J., Melås, A.M., Stræte, E.P., Søraa, R.A. (2021). "Balanced readiness
> level assessment (BRLa): A tool for exploring new and emerging technologies."
> Technological Forecasting & Social Change 169, 120854.
> https://doi.org/10.1016/j.techfore.2021.120854

## 1. What BRLa is

Classic Technology Readiness Levels (TRL, NASA lineage) measure only the
maturity of the *material technology*. Vik et al. argue implementation
readiness is multi-dimensional and propose five parallel 9-point scales:

| Dim | Name | Theme (one word) | Core question |
|-----|------|------------------|---------------|
| TRL | Technology Readiness Level | Development | How well developed is the technology itself? |
| MRL | Market Readiness Level | Commodification | How ready is the market / business model? |
| RRL | Regulatory Readiness Level | Legalization | Are legal/regulatory conditions in place? |
| ARL | Acceptance Readiness Level | Legitimization | Will society/the sector accept it? |
| ORL | Organizational Readiness Level | Domestication | Does it fit users' existing work practices? |

Each scale runs 1 (idea / unpredictable / illegitimate / fundamental break)
to 9 (proven in use / stable market / regulatory approved / generally
accepted / seamless integration).

Assessment logic in the paper (Table 2): for each dimension, questions are
asked top-down from level 9 downward; the first "Yes" fixes the level. Our
binned adaptation preserves this "highest supportable claim" logic.

## 2. The Low/Mid/High binning (project-specific simplification)

Because adjacent-level demarcations are fuzzy (e.g. ARL 5 vs ARL 6), this
project bins each 9-point scale into three phases. The pipeline assigns
**bins**, with an optional finer level estimate when evidence clearly
supports one.

### TRL
| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Research | Idea → proof of concept (scientific feasibility) | Is it scientifically plausible? |
| Mid | 4–6 | Development | Lab validation → prototype in relevant/natural environment (engineering feasibility) | Does a prototype work outside the lab? |
| High | 7–9 | Deployment | System prototype in natural environment → proven functional in use | Is the full system operating in the real world? |

### MRL
| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Exploration | Market hunch → need/product described, no validation | Is there a theoretical market? |
| Mid | 4–6 | Validation | Pilots, described business model, limited launches | Will early adopters buy it? |
| High | 7–9 | Expansion | Customer satisfaction → stable/growing sales | Is it selling at scale? |

### RRL
| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Barrier | Regulatory status unknown/unpredictable, or law changes needed | Is it currently legal at all? |
| Mid | 4–6 | Navigation | Permissions/certificates required; obtainable with effort | Can we get permission? |
| High | 7–9 | Clearance | Approvals imminent, general conditions fulfilled, or unproblematic | Do we have the green light? |

### ARL
| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Resistance | Seen as illegitimate/controversial/unwanted by large groups of the population | Does society actively reject it? |
| Mid | 4–6 | Hesitation | Questionable/inappropriate to groups of the population or key sector actors | Are stakeholders skeptical? |
| High | 7–9 | Legitimacy | Only marginal groups object, or generally accepted | Is it broadly accepted? |

### ORL
| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Mismatch | Fundamental break with existing practices; integration unclear or only vaguely imagined | Does it break current workflows? |
| Mid | 4–6 | Adaptation | Integration described/planned; major-to-moderate org changes needed | Can processes adapt to fit it? |
| High | 7–9 | Integration | Minor changes only → works seamlessly with existing processes | Does it fit seamlessly? |

Quick heuristic across all five dimensions:
- **Low (1–3):** Theoretical / Problematic
- **Mid (4–6):** Experimental / Challenging
- **High (7–9):** Operational / Proven

## 3. Evidence-type mapping (what to search for, per dimension)

| Dim | Strong evidence types | Typical sources |
|-----|----------------------|-----------------|
| TRL | Field trials, pilot deployments, product launches, patents, spec sheets | Company sites, trade press, academic papers, patent DBs |
| MRL | Pricing pages, sales figures, funding rounds tied to commercialization, customer counts, market reports | Company sites, market research, business press, Crunchbase-style coverage |
| RRL | Named regulators, approval/authorization decisions, certification schemes, pending rule-making | Regulator sites, legal/compliance commentary, trade press |
| ARL | Public controversy or endorsement, NGO/advocacy positions, opinion surveys, media sentiment, protests | News, NGO reports, social commentary, surveys |
| ORL | Adoption case studies, workflow-integration accounts, training/support requirements, user testimonials | Trade press, case studies, extension-service reports, user forums |

## 4. Known pitfalls (encode these in prompts)

1. **ARL/ORL evidence scarcity.** The literature and the web are thin on
   acceptance and organizational readiness. Expect weak evidence; confidence
   scoring must reflect evidence availability, not just model certainty.
2. **Absence of evidence ≠ evidence of acceptance.** "No controversy found"
   may mean High ARL *or* an obscure technology nobody has opinions on yet.
   When no direct evidence exists for a dimension, the assigner must set
   `evidence_gap: true` and say what was inferred from what.
3. **Dimensions move independently and non-monotonically.** A technology can
   be TRL-High and RRL-Low (Nofence pre-2017 in the paper). ARL can bounce
   back and forth. Never let one dimension's bin anchor another's.
4. **Terminology drift.** The same technology appears under product names,
   company names, generic category names, and academic terms
   (e.g. "Nofence" = "virtual fencing" = "fenceless grazing" = "GPS livestock
   collar"). Alias expansion (M1) exists to solve this; searches must use
   aliases, and evidence extraction must accept alias matches.
5. **Scores are point-in-time estimates from public information.** Vik et al.
   flag this themselves (their footnote 2). The output schema keeps
   `assessed_date` and human-override fields for exactly this reason.
