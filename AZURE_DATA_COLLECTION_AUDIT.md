# Azure Data Collection Audit (Batch 2)

_Last updated 2026-08-13 (hardening pass: single two-level concurrency model + process-wide cap;
metrics-failure → explicit REVIEW; shared retry stats; full-pipeline stress test; simultaneous-assessment
test). Scope: Azure API reliability — throttling, retries, pagination, concurrency, partial-data
detection, and assessment completeness. Financial-integrity rules from Batch 1 are unchanged (see
`FINANCIAL_INTEGRITY_AUDIT.md`)._

## The overriding rule

**Missing data is not zero.** Throttled data is not zero. Failed pricing is not a price. Failed billing
is not zero spend. Failed metrics are not zero utilisation. A partial collection is not a complete
assessment. Every stage now records what it actually collected vs what failed, and the run computes an
explicit **COMPLETE / PARTIAL / FAILED** data-quality state.

## 1. Azure API call inventory & call-flow

```
run_assessment(subscription_ids)                 [assessment.py]
 ├─ 1. INVENTORY        collect_inventory ──► client.query_resource_graph  (POST ARG, per bucket; ~20 buckets)
 │                       _gather_inventory_summary ──► query_resource_graph (count-by-type)
 ├─ 2. METRICS          enrich_{vms,asps,sql_dbs,sql_mis,disks} ──► client.get_metric (Azure Monitor; many per resource)
 ├─ 3. ADVISOR + RI     _gather_advisor ──► get_advisor_cost_recommendations (per sub, paged)
 │                       _gather_reservation_recs ──► get_reservation_recommendations (per sub, Consumption, paged)
 ├─ 4. BILLING          _gather_cost_and_consistency ──► query_cost_management (per sub, monthly history, paged)
 │                       _gather_spend_baseline ──► query_cost_management (per sub, service totals / daily run-rate)
 ├─ 5. PRICING          FindingsEngine ──► PricingEngine.query (Azure Retail Prices, cached, paged)   [findings.py]
 └─ 6. PERSIST          totals + data-quality state
```

| Azure API | Client method | Per | Pagination | Concurrency |
|---|---|---|---|---|
| Resource Graph | `query_resource_graph` (POST) | bucket × query | `$skipToken` | ~20 tasks → global cap |
| Resource Graph (count) | `query_resource_graph` | run | `$skipToken` | 1 |
| Azure Monitor metrics | `get_metric` (GET) | resource × metric | single response | metric semaphore (15) → global cap |
| Advisor | `get_advisor_cost_recommendations` | subscription | `nextLink` | per-sub gather → global cap |
| Consumption reservations | `get_reservation_recommendations` | subscription | `nextLink` | per-sub gather → global cap |
| Cost Management | `query_cost_management` (POST) | subscription | `properties.nextLink` | per-sub gather → global cap |
| Retail Prices | `PricingEngine.query` | SKU/region | `NextPageLink` | cached (24h) |

## 2. Root cause(s) of throttling

1. **Unbounded Resource Graph fan-out (primary).** `collect_inventory` fired ~20 ARG queries with a bare
   `asyncio.gather`, exceeding ARG's per-tenant rate limit (~15 queries / 5s) → 429 storms.
2. **Cost Management** is inherently throttle-heavy (returns `Retry-After`); the per-resource monthly
   query is the single most throttled call (already retried patiently, isolated from the breaker).
3. **Metrics sweeps** on large fleets (each VM = ~4 metric calls) could pile up (was bounded to 15 per
   type, but with no global ceiling several stages could still overlap).

**Fix:** a single **global concurrency semaphore** in `AzureClient._send` (`settings.azure_max_concurrency`,
default 8) — *every* outbound Azure request passes through it, so no caller's fan-out can exceed the cap.
The slot is held only for the HTTP round-trip; retry backoff waits release it.

## 3. Retry strategy (`resilience.py` — centralized)

- Retries **429** (throttle) and transient **5xx** (500/502/503/504) and httpx transport errors.
- Prefers Azure's **`Retry-After`** header; otherwise **exponential backoff with full jitter**.
- **Bounded**: `max_retries` (4 default; 8 for Cost Management/Consumption) — never an unbounded loop.
- **Each wait capped** at `MAX_BACKOFF_SECONDS` (60) so an absurd `Retry-After` can't stall the run.
- A **circuit breaker** trips after repeated hard failures (used for fast ARM calls; Cost Management is
  isolated from it so a busy metrics run can't fail-fast the billing query).
- **Logging** names the API/resource (`GET Microsoft.ResourceGraph/resources`), attempt N/max, reason
  (throttled / server error / transport), and the wait — **never** the token/headers.
- **`RetryStats`** (shared with the client) counts throttles, retries, server errors, transport errors and
  exhaustions; exhaustion is recorded (never silently treated as success) and folds into the run report.

## 4. Concurrency strategy — exactly TWO documented limits, no hidden per-stage caps

Concurrency is controlled at **one layer only — the `AzureClient`** — with two complementary,
configurable limits. Every outbound request acquires **both** (process-wide, then per-run). The former
per-type metrics semaphore (a hidden third limit) was **removed**.

| Limit | Setting | Default | Scope | Why |
|---|---|---|---|---|
| **Per-run** | `azure_max_concurrency` | 8 | one assessment (`AzureClient._semaphore`) | ARG throttling is **per-tenant** (~15 queries/5s) and one run targets one tenant, so this protects a tenant's rate bucket; 8 = headroom + fast. |
| **Process-wide** | `azure_global_max_concurrency` | 24 (=3×per-run) | ALL concurrent assessments (a per-event-loop module semaphore) | So N simultaneous runs can't multiply the per-run cap into uncontrolled aggregate outbound traffic; ~3 run full-speed, more share the budget. |

- **Per-run vs simultaneous runs:** the per-run semaphore is per `AzureClient` (one per assessment), so
  without a second limit, 3 concurrent assessments could reach 3×8 in-flight. The **process-wide**
  semaphore bounds the aggregate. Verified by `test_simultaneous_assessments_respect_global_cap`
  (3 clients, global cap patched to 5 → aggregate in-flight never exceeds 5).
- **Tests:** `test_global_concurrency_limit_is_respected` (per-run cap), the multi-assessment test above
  (process-wide cap), and the full stress test (`test_stress_pipeline_partial_but_consistent`) asserts
  `max_in_flight ≤ azure_max_concurrency` across a realistic 120-VM run.

## 5. Pagination handling

All paginated Azure APIs are followed to exhaustion — the first page is never assumed complete:

| API | Token | Tested |
|---|---|---|
| Resource Graph | `$skipToken` | 1 page, empty, **4×1000-row** pages |
| Cost Management | `properties.nextLink` | multi-page merge, single/empty |
| Advisor | `nextLink` | multi-page merge |
| Retail Prices | `NextPageLink` | (existing pricing tests) |

## 6. Data-quality / completeness model (`collection.py`)

`CollectionReport` accrues, per run: subscriptions requested; resources discovered (**None = count query
failed**, never a fabricated 0); inventory buckets total + failed; inventory-summary failed; metrics
requested + failed; advisor/reservation/billing failed subscriptions; billing-detail-unavailable; and the
`RetryStats`. `data_quality()`:

- **FAILED** — every inventory bucket failed (no defensible view of the environment).
- **PARTIAL** — any failure (a failed bucket, failed summary, failed metrics, a failed advisor/billing/
  reservation subscription, billing-detail-unavailable, or a retry exhaustion).
- **COMPLETE** — otherwise.

Stored on the assessment: `data_quality`, `data_quality_message` (the concise client line), and
`collection_diagnostics` (the detailed internal JSON — logs/debug only, **not** client prose).

## 7–10. Per-source: failure mode → retry → partial-data → effect on financial results

- **Resource inventory (ARG)** → 429/5xx/timeout → centralized retry (breaker) → a failed bucket is
  recorded (never "0 of that type"), run PARTIAL; **all** buckets failed → FAILED, findings shown = none.
  → *Financials:* fewer resources analysed, run flagged; totals only from what was collected.
- **Resource count (ARG summary)** → error → retry → `resources_discovered = None` (UNKNOWN, not 0),
  PARTIAL. → *Financials:* none (metadata only), but the run is not presented as clean.
- **Metrics (Azure Monitor)** → 429/5xx → retry → a *failed* metric (exception) is recorded, the VM left
  **un-classifiable** (`max_cpu=None`, `metrics_failed=True`) — **never** read as 0% / idle — and it
  surfaces as an aggregated **REVIEW finding** (`vm_metrics_unavailable`, evidence_state=review, 0
  saving, excluded from every total) so the evidence model explicitly records "discovered, metrics
  unavailable" with the affected VMs + reason. Run flagged PARTIAL. A genuine 403/404/empty is distinct
  ("no data", not a failure → no REVIEW). → *Financials:* no false idle/right-sizing; utilisation-
  dependent savings for those VMs are REVIEW / Not quantified.
- **Billing (Cost Management)** → 429/5xx → patient retry (8) → a *failed* sub is counted
  (`billing_failed_subs`), PARTIAL. Per-resource-empty-but-total-present → `billing_detail_unavailable`
  (Batch-1 degraded state: ungrounded findings withheld). A **403 = no access** is legitimate "no data",
  not a failure. → *Financials:* missing billing never becomes "zero spend"; grounded findings that can't
  be grounded become REVIEW / are withheld (Batch 1).
- **Pricing (Retail Prices)** → error → serve last-known-good cache, else **unavailable** (`None`).
  → *Financials:* an unpriced finding becomes **REVIEW / "Not quantified"** or SUPPRESSED (Batch 1) —
  never a fabricated price, discount, or reservation rate. (Not a data-quality trigger precisely because
  it cannot fabricate a number.) Verified: `test_pricing_failure_never_fabricates_a_quantified_saving`.

### End-to-end layer-by-layer propagation (verified)

| Failed layer | State | Test |
|---|---|---|
| Resource discovery (a bucket) | run PARTIAL (all buckets → FAILED) | `test_inventory_bucket_failure_marks_run_partial`, `test_all_inventory_failed_marks_failed_no_false_complete` |
| Resource count query | count UNKNOWN (not 0), PARTIAL | `test_inventory_summary_failure_leaves_count_unknown_not_zero` |
| Metrics | affected VMs → REVIEW, run PARTIAL, never idle | `test_metrics_failure_produces_review_not_idle`, `test_metrics_failure_marks_partial_not_idle` |
| Billing | REVIEW/withheld + PARTIAL, never zero spend | `test_billing_transient_failure_marks_partial`, `test_pipeline_degraded_billing_marks_findings_review` |
| Pricing | REVIEW / Not quantified, never fabricated | `test_pricing_failure_never_fabricates_a_quantified_saving` |

## 11. Hide-failure anti-patterns audited & fixed

Searched for `except → []/0`, `if isinstance(...)` swallows, silent skips:

- `_gather_inventory_summary` returned `(0,0,[])` on failure → now returns `ok=False` → count UNKNOWN + PARTIAL.
- `_gather_advisor / _cost_and_consistency / _service_costs / _spend_baseline / _reservation_recs` silently
  dropped failed subscriptions (`if isinstance(...)`) → now count the failed subscription into the report.
- `collect_inventory` failed bucket → `[]` + logged-only → now recorded into the report (PARTIAL/FAILED).
- `enrich_*_with_metrics` swallowed a metrics exception into `None` → now records the failure (PARTIAL) and
  marks the resource `metrics_failed` (still never misclassified as idle).
- **Legitimate** empties are preserved as-is: a 403 (no access) and a genuinely-empty Azure result are
  "Azure returned zero", distinct from "we failed to retrieve" — only the latter flags PARTIAL/FAILED.
- **Shared retry stats (hardening).** The `AzureClient`'s `RetryStats` is now the SAME object as the
  run's `CollectionReport.retry`, so every throttle/5xx/exhaustion counts — including the reservation
  path, which absorbs a persistent 429 into a partial result rather than raising. A retry **exhaustion**
  therefore flags the run PARTIAL, closing the last silent-partial gap.

## 12. Client-facing experience

Clean by default. When `data_quality != complete`, one concise professional line only (existing banner
visual language): _"Some Azure data could not be collected for this assessment. Affected recommendations
have been excluded from quantified savings; re-run shortly for complete results."_ (FAILED variant for a
total inventory failure.) No HTTP codes, stack traces, raw Azure exceptions, or developer terminology in
the UI — those live in `collection_diagnostics` + logs. The empty-findings state no longer says
"well-optimised" when a throttle/failure withheld the data.

## 13. Tests added

`tests/test_azure_reliability.py` (16): transient 500/503 retry + stats; throttle/exhaustion stats;
transport-error counting; backoff bounded by cap; `Retry-After` clamped; **global concurrency limit
respected**; Cost Management `nextLink` (multi/single/empty); Advisor `nextLink`; ARG `$skipToken`
(4×1000 pages, empty); completeness COMPLETE/PARTIAL/FAILED + concise client message.

`tests/test_assessment_pipeline.py` (5): inventory bucket failure → PARTIAL + message; summary failure
→ count UNKNOWN (not 0) + PARTIAL; metrics failure → PARTIAL, **not idle**; billing transient 500 →
PARTIAL; **all** inventory failed → FAILED, 0 findings, no false "complete".

`tests/test_financial_integrity.py` (hardening): metrics failure → REVIEW (`vm_metrics_unavailable`),
never idle; genuinely-empty metrics → not REVIEW; pricing failure → never a fabricated quantified saving.

`tests/test_azure_stress.py` (2): **full-pipeline stress** (120 VMs, 3 ARG pages, injected 429 +
Retry-After + transient 5xx that recover, one permanently-failed bucket) asserting all nine hardening
properties — concurrency ≤ cap, retries recover, pagination complete, resources preserved, failed ≠ zero,
run PARTIAL, no fabricated QUANTIFIED, no lost resources, totals consistent with the evidence model; and
**simultaneous assessments** (3 clients) respecting the process-wide cap.

Plus existing `test_resilience.py` (429/Retry-After/circuit breaker) and `test_resource_graph.py`
(`$skipToken`, `collect_inventory` bucket isolation). **Full suite: 304 backend tests pass; frontend
typecheck + production build green. No Batch-1 financial-integrity tests were weakened or removed.**

## 14. Remaining risks / follow-ups

1. **Concurrency defaults** (per-run 8, process-wide 24) are safe estimates for ARG's ~15/5s per-tenant
   limit; tune `azure_max_concurrency` / `azure_global_max_concurrency` per observed tenant behaviour.
2. **Pricing failures don't flag PARTIAL** — deliberate (they can't fabricate a number; Batch-1 turns them
   into REVIEW/SUPPRESSED). A "some prices unavailable" signal could be added via a per-run engine counter.
3. **Metrics-failure REVIEW is aggregated** (one `vm_metrics_unavailable` finding listing the affected
   VMs) rather than one card per VM — deliberate, to keep the client UI clean on large fleets; the
   evidence model still records each affected VM in the finding's details.
4. **Cross-process aggregate concurrency**: the process-wide cap bounds one process (one event loop). A
   multi-process/replica deployment would need a shared (e.g. Redis) limiter to bound aggregate Azure
   traffic across replicas — out of scope here (single-process worker).
