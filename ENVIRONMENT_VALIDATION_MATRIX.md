# Environment Validation Matrix

How the tool behaves across the environment shapes a real client subscription can present — and the
specific safeguard + test that guarantees each behaviour. The through-line: **missing data is never
treated as zero, and no financial number is ever fabricated.**

Legend for "Guarded by": test file (backend/tests) or code path that enforces the behaviour.

---

## A. Data-availability shapes

| # | Environment | Expected behaviour | Guarded by |
|---|---|---|---|
| A1 | **Empty subscription** (0 resources) | No findings; "well-optimized" shown **only** when collection was complete (not when a throttle/failure withheld data). No invented resources or savings. | `AssessmentDashboard` empty-branch guard (`!billing_detail_unavailable && !collectionIncomplete`); `test_assessment_pipeline` |
| A2 | **No Cost Management access** (billing 403/empty) | Current & projected spend **not shown** (placeholder, not 0); grounded findings withheld or shown as REVIEW; degraded banner; preflight flags Cost as *warning*, not blocking. | `financial_evidence` REVIEW path; `test_preflight` (cost warning); `ExecutiveSummary` caveat; `test_financial_integrity` |
| A3 | **Per-resource billing throttled** (subscription total OK, per-resource missing) | `billing_detail_unavailable=True`; grounded findings withheld (not estimated from list price); "re-run" banner. | `assessment._gather_*`; `AssessmentDashboard` degraded banner |
| A4 | **No metrics** (Insights throttled/unavailable) | Utilisation findings (idle/oversized) **not** emitted for affected VMs; a `vm_metrics_unavailable` REVIEW finding surfaces the gap; missing ≠ 0% util. | `findings._metrics_unavailable_review`; `collection` report; `test_azure_reliability` |
| A5 | **Partial data collection** (some Azure calls failed) | `data_quality = partial/failed` + client message; run never presents as a clean complete assessment; explicit COMPLETE/PARTIAL/FAILED status chip. | `collection.CollectionReport`; `_persist_findings_and_totals`; `AssessmentDashboard` status chip; `test_azure_reliability` |

## B. Billing-economics shapes

| # | Environment | Expected behaviour | Guarded by |
|---|---|---|---|
| B1 | **Sponsored / credited subscription** (compute ≈ free) | Flat-rate resources → REVIEW (reference list price only, never a tiny misleading saving); VMs flagged as anomaly ("verify billing"), saving withheld. | `SPONSORED_COST_FRACTION`, `_grounded_or_review`; `test_financial_integrity` (sponsored) |
| B2 | **New / recently-migrated subscription** (no full billing month) | Spend shown as an **estimated run rate** (labelled, with days billed); annualised savings from <14 days of run-rate → REVIEW ("insufficient billing history"). | `MIN_BILLING_DAYS_FOR_ANNUAL`; `spend_estimated`; `ExecutiveSummary`; `test_financial_integrity` (insufficient history) |
| B3 | **Savings exceed measured spend** (partial window) | Projected spend **withheld**; honest caveat ("treat savings as an upper bound"); never a negative/false projected number. | `ExecutiveSummary` reconcile gate; per-finding measured-spend cap |
| B4 | **Non-USD billing currency** | Severity bands currency-normalised (₹500 ≠ critical); every figure rendered in the subscription's billing currency. | `severity_from_savings` currency arg; `currency.symbol`; `test_findings` (currency) |
| B5 | **Part-time / lab VMs** (low effective uptime) | Low utilisation is informational, not an anomaly; grounded saving scales with real runtime; not flagged as suspect data. | `LOW_UTILISATION_FRACTION` vs `ANOMALOUS_UPTIME_FRACTION`; `test_findings` |

## C. Recommendation-conflict shapes

| # | Environment | Expected behaviour | Guarded by |
|---|---|---|---|
| C1 | **RI + right-sizing on same VM** | Counted once (larger wins); both displayed; total never sums them. | `resolve_overlaps`; `test_financial_integrity` (overlap) |
| C2 | **Reserved capacity + right-sizing (SQL/disk/App Service)** | Disclosed as mutually exclusive ("upper bound, not a sum"); never silently double-counted, never fabricated de-overlap. | `flag_reservation_rightsizing_overlaps`; `RECOMMENDATION_CONFLICTS_AUDIT.md` |
| C3 | **AHB applicable** | Conditional — excluded from headline total, surfaced separately as "potential"; realised only with owned licences. | `CONDITIONAL_CATEGORIES`, `counts_toward_total`; `test_findings` (AHB) |
| C4 | **Two findings on one resource** | Deduped to the higher-savings one. | `_dedupe`; `test_financial_integrity` (dedupe) |
| C5 | **Reservations available** | Only Azure's usage-based engine numbers used; a generic list rate is never turned into a reservation saving. | `commitments_from_recommendations`; `test_financial_integrity` (RI authoritative) |

## D. Scale & reliability shapes

| # | Environment | Expected behaviour | Guarded by |
|---|---|---|---|
| D1 | **Large subscription** (1000s of resources) | Near-linear detection (~0.01 ms/resource; 3000 resources ≈ 28 ms — see below); ARG/metrics/cost paginated; bounded concurrency. | `test_performance`; pagination + `azure_max_concurrency` (Batch 2) |
| D2 | **Azure throttling (429)** | Retry with backoff + `Retry-After` (capped); shared retry stats; partial-data detection if exhausted. | `resilience` retry; `test_azure_stress` |
| D3 | **Concurrent assessments** (many tenants at once) | Process-wide + per-run concurrency limits; per-tenant isolation. | `azure_global_max_concurrency`; `test_azure_stress`; `test_security_pentest` |
| D4 | **Multi-subscription / multi-tenant** | Same subscription-id in different tenants isolated; findings/assessments scoped to owner. | `_owned_assessment`; `test_security_pentest` (isolation) |

---

## Performance measurement (detection engine, Azure stubbed)

Measured by `tests/test_performance.py` — one full detection pass (VM utilisation + AHB + unattached disks
+ orphaned IPs + overlap reconciliation) over N of each type, grounded with a per-resource cost map:

| Resources (N×3 types) | Total time | Per resource | Findings |
|---|---|---|---|
| 30 (N=10) | ~1.6 ms | 0.053 ms | 18 |
| 300 (N=100) | ~3.3 ms | 0.011 ms | 168 |
| 1,500 (N=500) | ~14.5 ms | 0.010 ms | 835 |
| 3,000 (N=1000) | ~27.7 ms | 0.009 ms | 1,668 |

**Interpretation.** Detection compute is **near-perfectly linear** and negligible (sub-30 ms for 3,000
resources); per-resource time is flat as N grows, so there is no hidden O(n²) path. In production the
wall-clock is dominated by **Azure API round-trips**, which are bounded independently by the
pagination + bounded-concurrency + retry layer (see `AZURE_DATA_COLLECTION_AUDIT.md`), not by the engine.
The test asserts each size finishes within a generous ceiling and that per-resource time at N=1000 stays
within near-linear bounds of N=10, so a regression into super-linear scaling fails CI.

## Notes / follow-ups

- The perf test isolates **compute**; an end-to-end wall-clock benchmark against a live tenant (network
  included) is an environment-specific measurement best captured during a real client run.
- Rows above map to existing tests; the matrix is the single index a reviewer can walk to confirm each
  environment shape is covered.
