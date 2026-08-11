# Post-Implementation Fabrication Audit

After implementing the accuracy fixes, this is a complete inventory of **every place in the
backend that can produce a client-facing dollar amount, saving, discount, eligibility, or
recommendation**, with its data source and whether it is safe to show a client.

**Bottom line:** every displayed dollar figure now traces to one of three authoritative Microsoft
sources — **Cost Management actual cost**, the **live Retail Prices API**, or an **authoritative
Microsoft recommendation API** (Reservation Recommendations / Advisor). There are **no hardcoded
prices, no hardcoded discounts, and no fabricated savings percentages** left in the engine. Where
authoritative data is missing, the finding is **dropped or shown as unavailable** — never guessed.

Legend for "Type": **A** authoritative/live Microsoft data · **B** legitimate business/eligibility
threshold · **C** pricing estimate · **D** fallback price · **E** fabricated savings calc.
(All C/D/E that produced a client-facing dollar amount were removed.)

---

## Every dollar-producing code path (post-fix)

| # | File · Function | What it calculates | Data source | Type | Assumption | Safe to show |
|---|---|---|---|---|---|---|
| 1 | `findings.py` · `commitments_from_recommendations` | Reserved Instance savings (VM **and** non-VM) | `Microsoft.Consumption/reservationRecommendations` (Azure's net savings, SKU, qty, term — verbatim) | A | none | ✅ |
| 2 | `findings.py` · `advisor_findings` | Advisor cost-rec savings | Azure Advisor `savingsAmount` | A | none | ✅ |
| 3 | `findings.py` · `detect_vm_utilisation_findings` (idle) | Saving = VM's live PAYG price, capped at actual cost | Retail Prices + Cost Management | A | idle bar = ≤5% CPU / ≤10% mem (**disclosed** in recommendation) | ✅ |
| 4 | `findings.py` · `detect_vm_utilisation_findings` (oversized) | Saving = price(current) − price(target), capped at actual | Retail Prices + Cost Management | A | 70% headroom ceiling + tool-chosen target SKU (**disclosed**) | ✅ |
| 5 | `findings.py` · `detect_app_service_rightsizing` | Live price(current) − price(target) | Retail Prices | A | 70% ceiling + target SKU (**disclosed**) | ✅ |
| 6 | `findings.py` · `detect_sql_db_rightsizing` / `detect_sql_mi_rightsizing` | actual cost × (removed vCores / current), capped | Cost Management (grounded) | A | 70% ceiling + vCore ladder (**disclosed**); grounded-only | ✅ |
| 7 | `findings.py` · `detect_disk_rightsizing` | Live price(Premium) − price(Standard SSD), capped at actual | Retail Prices + Cost Management | A | 500 IOPS / 60 MB/s baseline + 70% ceiling (**disclosed**); SQL-VM disks excluded | ✅ |
| 8 | `findings.py` · `detect_unattached_disks` | Live retail disk price (None → dropped) | Retail Prices | A | none | ✅ |
| 9 | `findings.py` · `detect_orphaned_public_ips` | Live retail IP price (None → dropped) | Retail Prices | A | none | ✅ |
| 10 | `findings.py` · `detect_idle_app_service_plans` | Live ASP price (None/0 → dropped) | Retail Prices | A | none | ✅ |
| 11 | `findings.py` · `detect_deallocated_vms` | Attached disks' actual billed cost (grounded) | Cost Management | A | none | ✅ |
| 12 | `findings.py` · `detect_paused_sql_databases` / `detect_stopped_sql_managed_instances` | Resource's actual billed cost; unavailable → 0 (dropped) | Cost Management | A | none | ✅ |
| 13 | `findings.py` · `detect_orphans` (snapshots) | Snapshot's actual billed cost; unavailable → dropped | Cost Management | A | none | ✅ |
| 14 | `findings.py` · `detect_orphans` (empty LB / idle NAT / Bastion) | Actual cost, else live retail; neither → dropped | Cost Management → Retail Prices | A | none | ✅ |
| 15 | `findings.py` · `detect_windows_ahb` | Per VM: **actual billed cost × (Windows − Linux)/Windows licence fraction**, capped at the SKU's list licence premium. No billing or no live licence price → VM **excluded** (never priced at list) | Retail Prices + Cost Management | A (price) / B (ownership) | customer **owns** qualifying licences (unknowable via API → **conditional / "Potential"**, prerequisite shown before the number) | ✅ conditional, can't exceed spend |
| 16 | `report.py` · `_projection` / `SavingsProjection.tsx` | 3-yr projection of real spend + real savings | Real spend history (Cost Management) | A | linear-growth scenario, environment's **own** measured rate; hidden if no measured growth | ✅ labelled projection |
| 17 | `assessment.py` · run-rate baseline | Monthly spend = avg daily × 30.44 when <2 billed months | Real daily Cost Management data | A | run-rate extrapolation (flagged `estimated=True`) | ✅ labelled estimate |
| 18 | `findings.py` · `metrics_confidence` / `combine_confidence` | Confidence score (0–1) | metric volume/freshness + validation | B | tool heuristic — **not a dollar value**, internal | ✅ (not shown as $) |
| 19 | `findings.py` · `severity_from_savings` (`to_usd`) | Severity band (critical/high/medium/low) | static FX table | B | approximate FX — changes the **chip only**, never a displayed saving | ✅ |

### Eligibility thresholds (Type B — legitimate, now disclosed)
`IDLE_MAX_CPU = 5`, `IDLE_MAX_MEMORY_PCT = 10`, `DOWNSIZE_HEADROOM_CEILING = 70`,
`_STANDARD_SSD_BASELINE_IOPS = 500`, `_STANDARD_SSD_BASELINE_MBPS = 60`, the vCore ladders. These
decide *whether* a resource is a candidate; they never invent a price. Each threshold-driven finding
now appends an **"Assessment methodology (not an Azure-defined rule)"** disclosure to its
recommendation, and where Azure Advisor independently flags the same resource that corroboration
raises the finding's confidence.

---

## What was removed (Types C / D / E)

| Removed | Was | Now |
|---|---|---|
| VM retail-estimate RI detector (`detect_vm_commitments`) | Computed a discount from Retail Prices, which lacks reservation prices for most VM SKUs (E) | VM RIs come only from Azure's reservation engine (A) |
| `SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD = 112` | Hardcoded "licence" ≈ the entire GP Gen5 per-vCore compute price → ~3× over-stated AHB (C/E) | SQL AHB **retired** (no API exposes the licence delta) |
| `GRS_VAULT_MONTHLY_USD = 25` | Flat guess for GRS→LRS saving (C) | Backup-redundancy saving **retired** (can't isolate the premium) |
| `SNAPSHOT_PER_GB_MONTHLY_USD = 0.05` | `diskSizeGB × per-GB` (over-states; wrong basis) (C) | Snapshot saving grounded in actual Cost Management cost (A) |
| `LOAD_BALANCER / NAT_GATEWAY / BASTION_MONTHLY_USD` | Dated flat fallbacks (D) | Live Retail Prices only; else dropped (A) |
| `APP_SERVICE_PLAN_MONTHLY_USD` table | Per-SKU dated fallback (D) | Live Retail Prices only; else dropped (A) |
| `STATIC_FALLBACK_DISK_PER_GB` | Per-GB disk fallback (D) | Live Retail Prices only; else `None` (A) |
| Public IP static fallback ($3.65/$2.88) | Dated fallback (D) | Live Retail Prices only; else `None` (A) |
| `_live_or_fallback` sanity-band | Band anchored to a hardcoded price (D) | Removed with the fallbacks |

`estimates.py` now holds **no price constant at all**.

---

## Recommendations that still cannot be made fully authoritative

1. **SQL Server Azure Hybrid Benefit — retired (shown as nothing).** No Microsoft pricing API exposes
   the SQL licence component: the Retail Prices API publishes only one consumption price per vCore
   (licence-included), with no separate base/AHB meter (verified live). Rather than fabricate a
   figure, the tool produces no SQL AHB saving. If Microsoft later exposes the base-compute split via
   an API, re-implement `detect_sql_ahb` from that live source.

2. **GRS → LRS backup redundancy — retired.** The saving is the geo-redundancy premium on backup
   *storage*, which can't be separated from the vault's total bill without an assumption, and Resource
   Graph doesn't expose backup volume. No dollar figure is produced.

3. **Windows AHB ownership — conditional, by necessity.** The saving is grounded in each VM's **actual
   billed cost × live licence fraction** (capped at the SKU's list licence premium), so it can never
   exceed the VM's own bill — and a VM with no billing or no live licence price is **excluded**, never
   priced at list (this is what fixed the ₹888K-vs-₹44K case). But whether the customer **owns**
   qualifying Windows Server licences with Software Assurance is unknowable through any Azure API, so it
   is presented as a **"Potential"** saving with the licence prerequisite shown before the number, and
   is never implied to be automatic.

4. **Rightsizing / idle eligibility — authoritative pricing, disclosed methodology.** The dollar
   figures are real (live prices, capped at actual cost). What is the assessment's own judgment is the
   *eligibility* decision — the utilisation thresholds and the chosen target SKU. These are now
   disclosed in every such finding as assessment methodology, and Azure Advisor's own rightsizing
   recommendations are surfaced alongside and corroborate them.
