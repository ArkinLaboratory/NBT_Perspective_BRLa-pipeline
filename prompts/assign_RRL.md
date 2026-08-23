## RRL — Regulatory Readiness Level

Core question: **Are legal/regulatory conditions in place?**

| Bin | Levels | Phase | Meaning | Key question |
|-----|--------|-------|---------|--------------|
| Low | 1–3 | Barrier | Regulatory status unknown/unpredictable, or law changes needed | Is it currently legal at all? |
| Mid | 4–6 | Navigation | Permissions/certificates required; obtainable with effort | Can we get permission? |
| High | 7–9 | Clearance | Approvals imminent, general conditions fulfilled, or unproblematic | Do we have the green light? |

Strong evidence types: named regulators, approval/authorization decisions, certification schemes, pending rule-making.
Typical sources: regulator sites, legal/compliance commentary, trade press.

Assign the bin based on the most favorable major jurisdiction where the technology has achieved regulatory status. If readiness varies significantly across major jurisdictions (e.g., approved in some regions but restricted or uncertain in others), set `jurisdiction_variation` to true and identify the lower-readiness jurisdictions and their specific barriers in your rationale. If the regulatory landscape is uniform or the technology is unregulated everywhere, set `jurisdiction_variation` to false.

Include `jurisdiction_variation` in your JSON response as an additional boolean field:
```
"jurisdiction_variation": true | false
```
