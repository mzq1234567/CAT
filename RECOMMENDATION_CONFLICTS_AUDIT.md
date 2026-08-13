# Recommendation Conflict & Overlap Audit

**Scope:** every pair of recommendation families the engine can emit, and whether acting on both would
double-count the same Azure spend in **Total Identified Savings** / **Projected Spend**.

**Governing rule:** the headline total must never *overstate* savings by counting the same spend twice,
and must never *fabricate* a de-overlap number it cannot defensibly compute. Where a precise numeric
de-overlap is computable from the data, we compute it; where it is not, we **disclose** the overlap
rather than silently double-count *or* invent a cancellation.

---

## 1. How the engine prevents double-counting (three layers)

| Layer | Mechanism | Guarantee |
|---|---|---|
| **Per-resource dedupe** | `_dedupe()` (assessment.py) keeps at most **one finding per `resource_id`** — the highest-savings one. | The same resource can never be counted by two per-resource findings (e.g. a disk flagged both "unattached" and "rightsizing"). |
| **RI ↔ VM compute de-overlap** | `resolve_overlaps()` matches aggregate `ri_vm` recommendations to per-VM `idle_vms`/`oversized_vms`/`vm_rightsizing` by **(SKU, region)**; per overlapping instance, only the **larger** monthly saving counts (`counted_savings_*`); ties → the concrete per-VM action. | RI and right-sizing/idle on the same VM never sum. Both are still **displayed** at their own `estimated_savings_*`; only `counted_savings_*` (which feeds the total) is de-overlapped. |
| **Reserved-capacity ↔ right-sizing disclosure** | `flag_reservation_rightsizing_overlaps()` stamps `mutually_exclusive_with_reservation` on per-resource right-sizing/idle findings whose family also has a reserved-capacity recommendation in the same subscription. | The overlap is **surfaced** in the UI/PDF (never silent). See §4 for why this is disclosure, not numeric de-overlap. |
| **Backstops** | Every finding is capped at the resource's **actual billed cost** and at **measured monthly spend**; the dashboard **withholds** a Projected-Spend figure and shows an honest caveat whenever total savings exceed measured spend. | A gross double-count can never present as a plausible-but-false projected spend. |

---

## 2. Pair-by-pair verdicts

Legend: **DE-OVERLAPPED** (numeric) · **DISCLOSED** (flagged, not summed silently) · **EXCLUDED** (one side
never counts toward the headline) · **DEDUPED** (one finding per resource) · **INDEPENDENT** (different spend).

| Pair | Same spend? | Verdict | Mechanism |
|---|---|---|---|
| RI (`ri_vm`) ↔ VM idle/oversized/rightsizing | Yes — reserve vs shrink the same VM | **DE-OVERLAPPED** | `resolve_overlaps` (SKU+region, larger wins) |
| SQL DB reserved ↔ `sql_db_rightsizing` | Partially — reserve vs shrink same DB | **DISCLOSED** | `flag_reservation_rightsizing_overlaps` |
| SQL MI reserved ↔ `sql_mi_rightsizing` | Partially | **DISCLOSED** | same |
| Managed-disk reserved ↔ `disk_rightsizing` | Partially | **DISCLOSED** | same |
| App Service reserved ↔ `app_service_plan_rightsizing` / `idle_app_service_plans` | Partially | **DISCLOSED** | same |
| SQL reserved ↔ `paused_sql_databases` / `stopped_sql_managed_instances` | Reserving a paused/stopped resource is pointless | **DISCLOSED** | same (Azure's engine rarely recommends reserving idle usage, so co-occurrence is rare) |
| **AHB** (`windows_ahb`/`sql_ahb`) ↔ **anything** | License vs compute | **EXCLUDED** | AHB is *conditional* — excluded from the headline total entirely (surfaced separately as "potential"), so it can never double-count the realisable total. In Azure, **AHB + RI legitimately stack** (AHB removes the license premium; RI discounts compute), so no numeric conflict exists. |
| RI ↔ **Savings Plan** | Would be same compute | **N/A** | The engine emits **no** Savings-Plan findings; RI is the only commitment source (Azure's reservation engine). Nothing to reconcile. |
| `deallocated_vms` (disk residual cost) ↔ `unattached_managed_disks` ↔ `disk_rightsizing` | Same disk? | **DEDUPED / GUARDED** | A disk attached to a deallocated VM is *not* unattached (so not flagged unattached); `detect_disk_rightsizing(..., exclude_vm_ids=…)` excludes disks of deallocated/idle VMs; `_dedupe` covers same-`resource_id` collisions. |
| `empty_load_balancers` ↔ `orphaned_public_ips` | The LB's frontend IP? | **INDEPENDENT** | A public IP associated with a load balancer (even an empty one) is **not** orphaned, so it isn't flagged by `orphaned_public_ips`. The two findings address different resources. |
| `idle_nat_gateways` / `bastion_hosts` ↔ others | — | **INDEPENDENT** | Distinct network resources; no shared spend with other families. |
| `orphaned_snapshots` ↔ `unattached_managed_disks` | — | **INDEPENDENT** | A snapshot and a disk are separate billed resources. |
| Backup (`backup_redundancy` / `backup_policy_review` / `incremental_backup`) | Same vault item? | **DEDUPED** | Each is a distinct optimization; per-resource findings dedupe by `resource_id`, so a single backup item is counted once. |
| `advisor_cost` ↔ our own detection of the same resource | Yes | **DEDUPED** | `_dedupe` keeps the higher-savings finding per `resource_id`; a corroborating Advisor rec raises confidence but does not add a second saving. |

---

## 3. Precedence rules (deterministic, reproducible)

1. **One optimization per resource.** For a given `resource_id`, only the highest-savings finding counts
   toward the total (`_dedupe`). Rationale: you implement one strategy per resource.
2. **Reserve vs right-size the same VM → larger wins.** For each VM that a Reserved Instance and a
   right-sizing/idle finding both cover, count only the larger monthly saving; ties → the concrete per-VM
   action (`resolve_overlaps`). The loser's `counted_savings_*` is reduced (RI aggregate) or zeroed
   (per-VM finding); both remain displayed.
3. **Conditional savings never enter the headline.** Azure Hybrid Benefit is realised only with eligible,
   customer-owned licences, so it is surfaced as "potential" and excluded from Total Identified Savings and
   Projected Spend (`counts_toward_total` gate).
4. **Reserve vs right-size a non-VM resource → disclose, don't sum silently.** See §4.

---

## 4. Why reserved-capacity vs right-sizing (non-VM) is DISCLOSED, not numerically de-overlapped

Azure's VM Reserved-Instance recommendations expose a **(SKU, region, quantity)** we can match to a specific
right-sized VM, so we de-overlap them numerically with confidence.

Azure's **non-VM** reserved-capacity recommendations (SQL DB/MI, managed disk, App Service, Cosmos, MySQL,
Files) are **aggregates** with **no resource id** and a quantity expressed in units (vCores, GiB, etc.) that
do **not** map one-to-one to an individual right-sizing candidate. That means:

- We **cannot** reliably identify *which* specific resources a given aggregate reservation covers, so we
  cannot compute the precise overlapping amount.
- Fabricating a de-overlap (e.g. cancelling one whole side) would violate the same no-fabrication rule as
  inventing a saving — it would produce a number we cannot defend, and would likely **understate** by
  cancelling legitimate independent savings on *different* resources of the same family.

The honest, defensible design is therefore **disclosure**: `flag_reservation_rightsizing_overlaps()` stamps
`mutually_exclusive_with_reservation` on the per-resource finding, and the UI/PDF show *"This resource also
appears in the {family} recommendation… treat the two savings as an upper bound, not a sum."* The backstops
in §1 ensure this residual can never present as a false Projected-Spend figure.

**Materiality note.** Azure's reservation engine simulates *actual* usage, so it recommends reserving the
capacity a resource genuinely uses. A right-sizing candidate is, by definition, under-utilised — so the
reservation saving attributable to it is small, and the practical overlap is bounded and modest. The
disclosure calls it out without over- or under-stating.

---

## 5. What changed in this phase

- **New:** `flag_reservation_rightsizing_overlaps()` (assessment.py) + `_RESERVED_RIGHTSIZING_OVERLAP` map —
  disclosure pass, wired into `_detect_findings` after `resolve_overlaps`. Totals are never altered by it.
- **New UI disclosures** (RecommendationDetails.tsx): honest notes for `overlap_superseded_by_ri`,
  `overlaps_with_ri`, and `mutually_exclusive_with_reservation` — previously these flags were computed but
  not surfaced.
- **Tests:** `test_financial_integrity.py` — 5 new tests for the disclosure pass (same-family flagged,
  different-family not flagged, cross-subscription not flagged, aggregate-reservation wildcard, no-reservation
  no-flags), alongside the existing RI-vs-VM de-overlap tests.

## 6. Residual risks / follow-ups

- **Non-VM reserve/right-size overlap remains a disclosed upper bound**, not a netted figure (by design —
  §4). If Azure later exposes resource-level reservation attribution, this can become numeric.
- The disclosure matches on **subscription** (aggregate reservation = wildcard). Within a very large single
  subscription mixing reserved and right-sized resources of the same family, the flag is applied broadly
  (conservative — favours disclosing over missing).
