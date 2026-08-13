# Financial Integrity Audit — Azure Cost Assessment Tool

_Last updated 2026-08-13 (second pass: closed the four remaining risks — PDF consistency, sponsored/
credited subscriptions, RI-vs-right-sizing overlap, and the billing-history threshold wording). Scope:
the recommendation engine, the PDF report + every financial number the tool produces._

## Overriding rule

**The tool never invents, assumes, or fabricates a financial number.** Every finding carries an
explicit **evidence state**, and every financial value carries an explicit **source type**. The two are
never silently interchangeable: a retail/list price is *not* actual billed cost; a potential saving is
*not* an identified saving; an estimate is *not* a validated saving.

## Evidence model (`app/services/financial_evidence.py`)

| State | Meaning | May show a saving? | Counts in Total / Projected? |
|---|---|---|---|
| **QUANTIFIED** | Defensible, reproducible financial impact (actual billed cost, authoritative price, or Azure's own engine) | Yes | Yes — **unless conditional** (AHB) |
| **REVIEW** | A real optimisation signal we cannot price for this customer | No — shows "Not quantified" (+ optional clearly-labelled reference list price) | Never |
| **SUPPRESSED** | Not enough evidence to make a defensible recommendation | — (finding not shown) | Never |

**Value source types** (tagged on each finding's `details.savings_source` / `reference_price_source`):
`ACTUAL_BILLED_COST`, `AUTHORITATIVE_RETAIL_PRICE`, `AUTHORITATIVE_RESERVATION_PRICE`,
`AZURE_ADVISOR_ESTIMATE`, `ESTIMATED_ANNUALISED_COST`, `QUANTIFIED_SAVINGS`, `POTENTIAL_SAVINGS`,
`UNQUANTIFIED`.

The single gate `counts_toward_total(evidence_state, conditional)` — QUANTIFIED **and not** conditional —
is deferred to by the persistence layer, the dismiss/restore API, and the dashboard aggregates alike.

## Per-recommendation matrix

| Recommendation | Pricing source | Billing source | Eligibility req. | Evidence state | In total? | Annualised? | Known limitations |
|---|---|---|---|---|---|---|---|
| Unattached disk | — | Cost Management | none | QUANTIFIED if billed cost known, else **REVIEW** (retail = reference) | if quantified | last-month/run-rate | REVIEW when no per-resource billing |
| Orphaned public IP | — | Cost Management | none | QUANTIFIED / **REVIEW** | if quantified | " | " |
| Idle App Service Plan | — | Cost Management | none | QUANTIFIED / **REVIEW** | if quantified | " | " |
| Empty Load Balancer / Idle NAT / **Bastion** | Retail (reference only) | Cost Management | none | QUANTIFIED if billed cost known, else **REVIEW** | if quantified | " | Bastion on sponsored subs → REVIEW (was the ₹44K bug) |
| Orphaned snapshot | — | Cost Management | none | QUANTIFIED, else SUPPRESSED | if quantified | " | Snapshots bill on incremental storage; no retail reference → grounded-cost-only |
| Idle / Oversized VM | Retail (delta) | Cost Management (**required**) | none | QUANTIFIED (grounded-only) | yes | " | Suppressed with no billed cost |
| Disk right-sizing | Retail (delta) | Cost Management (cap) | none | QUANTIFIED | yes | " | Skips latency-sensitive/SQL disks |
| SQL DB / MI right-sizing | — | Cost Management (**required**) | none | QUANTIFIED (grounded-only) | yes | " | Grounded-only |
| App Service Plan right-sizing | Retail (delta) | — | none | QUANTIFIED | yes | " | Authoritative retail delta |
| Deallocated VM | — | Cost Management (disks) | none | QUANTIFIED, else SUPPRESSED | yes | " | Saving = still-billing disks' actual cost |
| Paused SQL / Stopped MI | — | Cost Management (**required**) | none | QUANTIFIED, else SUPPRESSED | yes | " | Grounded-only |
| **Reserved Instances (all types)** | **Azure reservation engine** | Azure engine (real usage) | Azure-verified | QUANTIFIED (authoritative) | yes | Azure-provided | **Only** source; no tool-side discount; empty when usage isn't steady |
| **Windows AHB** | Retail (Win−Linux delta) | Cost Management (**required**) | **owns eligible licences + SA** | QUANTIFIED **but conditional → POTENTIAL** | **no** | last-month/run-rate | Ceiling = actual × licence fraction ≤ actual cost |
| SQL AHB | — | — | — | **RETIRED** (no authoritative base/AHB split) | no | — | Not quantified anywhere |
| Advisor cost | Azure Advisor | Azure Advisor | Azure | QUANTIFIED (Azure estimate) | yes | Azure-provided | Advisor's own number, kept verbatim |

## No-fabrication guarantees (what was removed / enforced)

- **No hardcoded fallback prices.** Every `pricing.py` getter returns `None` when the Retail Prices API
  (and cache) has nothing; the caller drops or REVIEWs the finding — never substitutes a dated/guessed rate.
- **No fabricated RI price.** The retail *reservation* price helper (`get_vm_reserved_monthly_price`) was
  **removed**, and the old retail-estimate VM RI detector was already retired. RIs come solely from Azure's
  usage-based reservation engine; a generic list rate can never be mistaken for a reservation saving.
- **No made-up discount %.** There is no tool-side RI/Savings-Plan discount anywhere.
- **List price is never a saving.** For orphan/always-on resources without billed cost, the retail rate is
  shown only as a clearly-labelled **reference** on a REVIEW finding — this eliminated the Bastion figure
  that was a list price clamped to total subscription spend.
- **AHB is bounded.** Saving = `actual billed × (Windows−Linux)/Windows`, additionally capped at the SKU's
  list licence premium — so it can never exceed the eligible spend being displaced, and is excluded from
  every total (conditional on licences the tool never assumes the customer owns).
- **Insufficient history isn't annualised.** A run-rate saving over fewer than
  `MIN_BILLING_DAYS_FOR_ANNUAL` (14) days becomes REVIEW ("insufficient billing history"). **This 14-day
  minimum is THIS TOOL'S OWN conservative validation rule — NOT an Azure or Azure Advisor requirement**
  (Microsoft defines no such minimum). Azure's 7/30/60-day windows are a separate concept (the
  reservation-recommendation look-back in `azure_client.py`), unrelated to this annualisation guard.
- **Sponsored/credited guard.** A flat-rate resource billed at < `SPONSORED_COST_FRACTION` (1%) of its
  list price is effectively free under Azure credits (or a currency-scale mismatch). It becomes REVIEW —
  the reference list price is shown (the resource still has economic value) but no near-zero "saving" is
  quantified, so a credited subscription never reads as "this resource is worthless" nor invents a number.
- **Last-resort net.** No single finding may exceed the subscription's measured monthly spend.

## Double-counting / overlap — RESOLVED (deterministic)

- `_dedupe` keeps **one finding per resource** (the highest-saving), so per-resource overlaps
  (e.g. an idle VM that also has an Advisor rec) never sum.
- AHB is **excluded from the total** entirely (conditional); idle VMs are excluded from AHB
  (`exclude_ids`) — you can't save a licence on a VM you'd delete.
- **RI vs right-sizing (`resolve_overlaps` in `assessment.py`).** Azure's RI recommendations are
  per-(SKU, region) with a quantity; a VM flagged for right-sizing/idle can fall inside an RI for its
  current SKU/region. Reserving a VM at its current size and shrinking/deallocating it are **mutually
  exclusive** strategies for the same spend. **Precedence rule (deterministic, reproducible):** each
  flagged VM consumes one reservable instance of its SKU/region; for that instance only the **larger** of
  {right-sizing/idle saving, per-instance RI saving} is counted (ties → the concrete per-VM action). The
  loser's `counted_savings_*` is reduced — a superseded per-VM finding drops to 0; the RI aggregate is
  reduced by the ceded instance's per-unit saving. Every finding is **still displayed** at its own
  `estimated_savings_*`; only the **total** uses `counted_savings_*`. Nothing is hidden from the UI.
- Each finding now carries `counted_savings_monthly/annual` (its non-overlapping contribution). The
  headline total, donut, category tiles, waterfall (web) and pillar tables (PDF) all sum **counted**,
  while individual cards/rows show **estimated** — so the same spend is never double-counted.

## Projected spend

`Projected = current validated spend − Σ(quantified, non-conditional, NON-OVERLAPPING counted savings)`.
It excludes REVIEW findings, conditional AHB, list-price-only figures, superseded overlaps, and anything
suppressed. Frontend and backend compute the total from the same `counts_toward_total` + `counted_savings`
rule; the PDF (`report.py`) uses the identical helpers.

## PDF report — evidence-consistent with the web

`report.py` now applies the exact same semantics as the dashboard (traced through the data layer, not a
text replacement):
- REVIEW findings render **"Financial impact: Not quantified"**; any reference price is shown as
  **"reference list price … (not billed cost)"** and never as a saving.
- REVIEW and conditional AHB are excluded from every pillar total and savings chart; pillar figures
  decompose the headline total exactly.
- Pillar totals/charts sum `counted_savings` (non-overlapping); a right-sizing finding superseded by an
  RI is shown but labelled **"Counted under Reserved Instances"**, never added again.
- Three-year projections already used the (counted, non-conditional) headline total, so they inherit the
  same exclusions.

## Tests performed (all passing — 278 backend tests, frontend typecheck + build green)

`tests/test_financial_integrity.py`:
- missing RI price → no RI finding; RI uses Azure's saving verbatim; retail-reservation path absent
- Bastion with no billed cost → REVIEW (reference only); with billed cost → QUANTIFIED
- **sponsored/credited** near-zero bill → REVIEW (reference only); normal/discounted bill → QUANTIFIED
- AHB without eligibility → POTENTIAL, excluded from total; AHB saving ≤ eligible spend (ceiling)
- **RI overlap**: RI only; right-sizing only; both on one VM (larger counted, never summed); right-sizing
  larger than RI wins; multiple VMs mixed (per-instance precedence, total non-overlapping)
- insufficient billing history (5 days) → REVIEW; sufficient (30 days) → QUANTIFIED
- `counts_toward_total` rule (quantified-non-conditional only)

`tests/test_report.py`: REVIEW → "Not quantified" + reference, excluded from pillar total/chart; pillar
totals use `counted_savings`; superseded overlap labelled; PDF renders with REVIEW/overlap findings.
Plus updated `test_findings.py` and `test_assessment_pipeline.py` (grounded-in-actual / REVIEW-without-
billing / no-cost-access / degraded-billing → total 0).

## Remaining financial-integrity risks / follow-ups

1. **Advisor estimates** are taken from Azure verbatim (QUANTIFIED); they are Azure's own numbers, not
   independently grounded in the customer's per-resource bill. Acceptable (authoritative source), noted.
2. **`MIN_BILLING_DAYS_FOR_ANNUAL = 14`** and **`SPONSORED_COST_FRACTION = 1%`** are OUR conservative
   thresholds (documented as such); tune per appetite.
3. **A "sponsored subscription" run-level banner** (like the degraded-billing banner) would add context
   on credited subscriptions. Per-finding handling is now safe (REVIEW); a run-level signal is a UI
   nicety, deferred (no UI work in this pass).
4. **RI vs right-sizing precedence is "larger wins"** — a defensible, conservative, reproducible choice.
   If a client prefers always recommending one strategy over the other, the precedence in
   `resolve_overlaps` is the single place to change it.
