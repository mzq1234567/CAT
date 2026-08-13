# Implementation Plan — Fixing the AHB & Cost-Basis Accuracy Issues

**Read `AHB_AND_COST_BASIS_ISSUES.md` first** — it describes the two problems with real data. This
document is the concrete plan to fix them, grounded in the actual code after inspecting it. Nothing has
been changed yet; this is for review.

**One-sentence summary of the recommendation:** Both issues are the *same* bug — the tool grounds every
saving in **one arbitrary/incomplete month** (`cost_map = last complete month`). The fix is a single new
primitive, a **representative monthly cost per resource**, that reuses data the tool already fetches, and
routing every detector (AHB, right-sizing, orphans) and the spend figure through it.

---

## 1. What I found in the code (the important facts)

| Fact | Where | Consequence |
|---|---|---|
| `cost_map` = each resource's **last complete month** (`costs[-1]`), MTD fallback | `cost_management.py::get_cost_map_and_consistency` | Stale on bursty envs; a fragment on partial billing → **both bugs** |
| Per-resource **mean, billed_months, cv, `stable`** already computed | `cost_management.py::cost_consistency` | The signal we need for the fix is **already there, just unused** |
| 6 months of history already fetched | `assessment.py::_gather_cost_and_consistency(months=6)` | We have the data to compute a representative figure |
| Run-rate baseline is **service-level daily**, not per-resource | `cost_management.py::get_runrate_baseline` | Fixes the spend card only, not per-resource grounding |
| Metrics are **daily grain** (`interval="P1D"`) | `azure_client.py::get_metric` | `cpu_datapoints` = *days on*, **not hours/day** → too coarse for true uptime |
| AHB VM list has **no metrics attached** | `assessment.py` (line ~332): `detect_windows_ahb(inventory["windows_vms_without_ahb"])` — never enriched | A metric-based uptime approach would need new queries + enrichment |
| Cost query has **no ChargeType filter**, no `UsageQuantity` | `cost_management.py::build_cost_query` | One-time purchases (reservation buys, one-offs) are **not stripped** |
| AHB today = `actual_billed_cost × licence_fraction`, capped | `findings.py::detect_windows_ahb` | Correct *formula*; wrong *basis* (fragment on partial billing) |
| Hard cap "no finding > measured spend" uses `measured_monthly_spend = sum(service_costs)` | `findings.py::_finding`, `assessment.py` | Must stay consistent with whatever basis we adopt |

---

## 2. The key realization (why one primitive fixes both)

The customer asked, for AHB: *"base it on how much the VM actually runs."* That instinct is right — **but
the bill already encodes runtime.** Azure bills the Windows licence per running hour, so:

```
billed_windows_cost = running_hours × windows_hourly_rate
⇒ AHB saving = running_hours × licence_hourly_rate
             = billed_windows_cost × (licence_hourly / windows_hourly)
             = billed_cost × licence_fraction        ← exactly today's formula
```

So `actual_cost × licence_fraction` is already the "uptime" calculation — **the only thing wrong is that
`actual_cost` is a partial fragment on a new subscription.** If we replace the fragment with a
**representative full-month cost** (run-rated / averaged), AHB becomes correct *without any new metric
queries*. The same representative cost also fixes right-sizing/idle (Issue 2's bursty-env case).

That's why the recommended fix is a cost-basis primitive, not a metrics-uptime feature (see §5 for the
honest comparison).

---

## 3-A. Decision rules (AUTHORITATIVE — reviewer-approved refinement)

The system distinguishes **four separate numbers** and never conflates them:

| # | Concept | Definition | Nature |
|---|---|---|---|
| 1 | **Actual billed spend** | Last complete month, or current month-to-date (MTD) — real invoiced money | fact |
| 2 | **Historical representative spend** | Mean of the billed complete months (the existing `cost_consistency.mean`) | representative |
| 3 | **Current run-rate** | `MTD × (AVG_DAYS_PER_MONTH / day_of_month)` — the current month projected to a full month | estimate |
| 4 | **Estimated / potential savings** | Derived from the *savings basis* below; AHB is always conditional/"Potential" | estimate |

**Per-resource rules** (using `cost_consistency`: `billed_months`, `cv`, `stable = billed_months≥2 && cv≤0.25`):

| Situation | **Savings basis** (what savings are computed from) | **Displayed as current spend** | `is_estimate` | `variability` |
|---|---|---|---|---|
| **Stable** (billed ≥2, cv ≤ 0.25) | Last complete month | Last complete month | `false` | `low` |
| **Erratic** (billed ≥2, cv > 0.25) | **`min(historical_mean, current_run_rate)`** — never over-states, whether the spike is recent or old | **Current run-rate**, with **historical average shown alongside** when divergence is material | `true` | `high` |
| **Partial** (billed < 2, e.g. #72) | Current run-rate | Current run-rate (labelled "estimated, partial billing") | `true` | `high` |
| **No cost** | — (finding suppressed, never fabricated) | — | — | — |

Why `min(mean, run-rate)` for erratic (not just the mean): the reviewer correctly noted the mean can
**over-state**. History `5,6,5,7,6,80` → mean ≈ 18 but current is ~6 → savings on 18 would exceed
current spend. `min` always keeps the savings basis at or below the current pace → **never over-states**,
and it matches the reviewer's example (recent spike: `min(18, 80)=18` = the historical average).

**Divergence threshold (when to show both numbers + a warning):** the resource/subscription is flagged
**high-variability** and the current run-rate is shown next to the historical average whenever
`cv > 0.25` **or** `billed_months < 2`, AND the current run-rate differs from the historical mean by
**≥ 2×** (ratio outside `[0.5, 2.0]`). Example copy:

> Historical average: ₹18K/month · **Current run-rate: ₹80K/month** — high variability; current usage
> is significantly above historical levels. Savings use the conservative figure; re-run after a full
> month to confirm.

**Which number is used where (no conflation):**
- **Savings calculations** → the *savings basis* column above (grounds AHB, right-sizing, orphans).
- **"Current Azure spend" KPI** → actual last month (stable) or current run-rate (erratic/partial),
  with the historical average as secondary context when divergence is material.
- **Every estimate is labelled** — a finding carries `cost_basis` (`last_month`|`representative`|`run_rate`),
  `is_estimate`, `historical_monthly`, `current_run_rate`, `variability`; the UI shows a small
  "estimate" / "high variability" chip. Actual (last-month, stable) numbers carry no estimate chip.
- **AHB stays conditional** ("Potential / requires eligible licences") on top of all of the above, and
  remains capped so it never exceeds spend. Fixing the basis fixes the *magnitude* (₹117 → real), not
  the conditional nature.

## 3. Recommended approach — `representative_monthly_cost` per resource

Add one function that, for each resource, returns a **representative recurring monthly cost** plus a
**basis label** and a **variability flag**, computed from the 6-month history the tool already has:

```
representative_monthly_cost(history[rid], consistency[rid], observed_span_days) ->
    { amount, basis, is_estimate, variability }
```

Decision tree (per resource):

1. **Stable, complete history** (`stable == True`, i.e. billed ≥2 months, cv ≤ 0.25)
   → use the **last complete month** (representative and current). `basis="last_month"`, `is_estimate=False`.

2. **Erratic history** (billed ≥2 months but cv > 0.25 — the LABS/bursty case)
   → use the **mean of billed months**. `basis="6mo_mean"`, `is_estimate=True`, `variability="high"`.
   Also compute the current-month run-rate; if it diverges from the mean by > (say) 2×, set
   `variability="high"` and surface a "spend is running Nx vs typical" note.

3. **Partial billing / new subscription** (billed_months < 2 — the #72/AHB case)
   → **run-rate** the observed cost to a month: `mtd_cost × (AVG_DAYS_PER_MONTH / observed_span_days)`.
   `basis="run_rate"`, `is_estimate=True`. Reuses `AVG_DAYS_PER_MONTH` and the observed span the
   spend baseline already computes.

4. **No cost at all** → return `None` (unavailable). Detectors then **suppress** the finding (never
   fabricate), exactly as they do today.

Notes:
- v1 can apply a **subscription-level run-rate factor** (`AVG_DAYS_PER_MONTH / spend_period_days`,
  already available from `_gather_spend_baseline`) to each resource's MTD cost — cheap, no new query.
  v2 can fetch **per-resource daily** cost for a per-resource span (more accurate; heavier query) if the
  subscription-level factor proves too coarse.
- "Mean of **billed** months" (ignores off-months) vs "mean of **all** months" is a judgment call for
  bursty labs that are idle half the year — see Open Questions.

---

## 4. Where it plugs in (concrete changes, phased)

**Phase 1 — the primitive + one-time-charge stripping (foundation).**
- `cost_management.py`: add `representative_monthly_cost(...)` (pure, unit-tested) implementing §3.
- `cost_management.py::build_cost_query` / `build_monthly_history_query`: add a **ChargeType = Usage**
  filter (`filter: {dimensions: {name: "ChargeType", operator: "In", values: ["Usage"]}}`) so
  reservation purchases and one-off charges never enter the recurring baseline.
- `get_cost_map_and_consistency`: build `cost_map` from `representative_monthly_cost` instead of
  `costs[-1]`, and also return a parallel `cost_basis` map (label + is_estimate + variability per rid)
  so the UI can disclose it.

**Phase 2 — route detectors through it (behaviour fix).**
- `findings.py::detect_windows_ahb`: unchanged formula (`cost × licence_fraction`), but `cost_map` now
  carries the representative (run-rated) cost → CRA-VM goes from ₹117 to its real ~₹6–12k, still
  conditional/"Potential", still capped. Add `basis`/`is_estimate` to the finding details so the UI can
  show "run-rate estimate — re-run after a full month".
- `detect_vm_utilisation_findings`, disk/ASP/SQL right-sizing: already grounded in `cost_map`; they
  automatically improve. Keep the grounded-only guard (no cost → suppress).
- `_finding` hard cap: pass `measured_monthly_spend` = **sum of representative costs** (or the run-rated
  spend), not the stale last month, so the cap stays consistent with the new basis.

**Phase 3 — disclosure (never mislead).**
- Frontend: where a finding's basis `is_estimate`, show a small "run-rate estimate / partial billing"
  chip; where `variability == "high"`, show "spend varies ~Nx month-to-month — treat as a range".
  Reuse the existing "partial billing" banner styling. The headline number stays, the caveat is visible.

**Phase 4 (optional, later) — direct uptime metric.**
Only if we want AHB to work when a subscription has metrics but **zero** billing (a case we currently,
correctly, suppress). Add a `VmAvailabilityMetric` (or hourly-CPU) enrichment for the AHB VM list and
use `availability% × 730 × per-hour licence` as a *fallback* when there's no cost at all. Not required
for the reported bugs.

---

## 5. Honest comparison — why NOT lead with the "uptime from metrics" idea

| | Representative/run-rate **cost** (recommended) | Uptime from **metrics** |
|---|---|---|
| Fixes Issue 1 (AHB partial) | ✅ | ✅ |
| Fixes Issue 2 (bursty basis) | ✅ (same primitive) | ❌ (AHB-only) |
| New Azure queries | none (reuses cost data) | yes (availability or hourly CPU, per VM) |
| Authoritative source | the **actual bill** (hours × real rate) | metric-inferred hours × **list** rate |
| Works with today's data | yes | no — AHB VMs aren't metric-enriched; daily grain is too coarse |
| Accuracy on partial billing | high (real billed cost, projected) | good, but list rate ≠ customer's real rate |
| Complexity | low | medium-high (grain, lifetime, join, throttling) |

The bill is a **more authoritative** measure of "how much it ran" than metric-inferred uptime × list
price — because the bill is hours × the customer's *actual* rate. Metrics-uptime is a good *fallback for
the zero-billing edge case only* (Phase 4).

---

## 6. Tests to add (all pure/mockable, no network)
- `representative_monthly_cost`: stable → last month; erratic → mean + high-variability flag; partial →
  run-rate; no cost → None.
- ChargeType filter present in the built query bodies.
- **AHB partial-billing golden**: a VM billed ₹239 over ~12 observed days → run-rate ≈ ₹607/day-adjusted
  → AHB ≈ real licence share (not ₹117, not the full 24×7 licence), and still ≤ representative spend.
- **Bursty golden (LABS)**: 6-month history with a mix of quiet/busy months → basis = mean (not the
  quiet last month), `variability="high"`.
- **Reconciliation**: AHB total = Σ per-VM; dashboard/PDF unchanged path; hard cap uses representative
  spend so nothing exceeds it.
- **Guarantee preserved**: no finding exceeds representative measured spend; zero cost → suppressed.

---

## 7. Risks & edge cases (and how the plan handles them)
- **Run-rate on a 1–2 day-old subscription is noisy** → label `is_estimate`, low confidence, "confirm
  after a full month". Never presented as precise.
- **Bursty lab idle half the year**: mean-of-billed-months may over- or under-state depending on
  weighting → flag variability prominently; recommend re-running when representative. (Open question on
  weighting.)
- **A resource created mid-window** has fewer billed days than the subscription span → v1 subscription-
  level factor slightly over-states it; v2 per-resource span fixes it.
- **Customer already owns reservations**: their covered usage bills at `ChargeType=Usage` = $0 for the
  reserved hours; representative cost then reflects only non-reserved usage — correct, but worth noting.
- **AHB stays conditional** (licence ownership unknowable) and **capped at spend** regardless of basis.

---

## 8. Open questions for review
1. **Bursty weighting:** for a lab idle half the year, use mean of *billed* months (ignores idle months,
   higher) or mean of *all* months (includes zeros, lower)? Which is fair to quote a client?
2. **Divergence threshold:** at what ratio (2×? 3×?) between current run-rate and historical mean do we
   switch basis and/or flag "spending changed"?
3. **Run-rate granularity:** ship v1 (subscription-level factor) first, or go straight to per-resource
   daily cost (heavier query, more accurate)?
4. **One-time charges:** filter `ChargeType=Usage` only, or also surface stripped purchases (e.g. "you
   bought a ₹X reservation this month") as context?
5. **Phase 4 uptime metric:** worth building at all, given the bill is more authoritative and zero-
   billing is already safely suppressed?

---

## 9. Files that change
- `backend/app/services/cost_management.py` — new `representative_monthly_cost`; ChargeType filter;
  `get_cost_map_and_consistency` returns representative `cost_map` + `cost_basis` labels.
- `backend/app/services/assessment.py` — pass representative spend into the engine's `measured_monthly_spend`;
  thread `cost_basis` for disclosure.
- `backend/app/services/findings.py` — AHB & findings surface `basis`/`is_estimate` in details; no formula
  change (they inherit the corrected `cost_map`).
- `frontend/src/components/dashboard/*` — small "run-rate estimate" / "high variability" disclosure chips.
- Tests: `backend/tests/test_cost_management.py`, `test_findings.py`, `test_assessment_pipeline.py`.
