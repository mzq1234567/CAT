# Azure CAT — Engineering Memory

> Single source of truth for the enterprise upgrade. Append a concise entry after each step:
> what was built, files touched, key decisions/tradeoffs, assumptions, and anything deferred.
> Keep it bullet-point terse. Newest entries at the bottom of the Decisions Log.

---

## Overview

**Goal:** Upgrade the existing Azure Cost Assessment Tool (CAT) from an MVP into a production-ready,
enterprise-grade multi-tenant SaaS platform.

**Baseline (before this work):**
- Frontend: React 18 + Vite + MUI v6 + Recharts (dark enterprise UI), MSAL delegated multi-tenant auth.
- Backend: FastAPI + SQLAlchemy + SQLite. Async Azure calls via httpx.
- Azure APIs used: Subscriptions, MS Graph (identity claims only), Azure Advisor (cost recs).
- Inventory collected via Resource Graph, but **filtered client-side in Python** after fetching everything.
- Pricing was **hard-coded 2023 tables** in `findings.py`.
- Findings: Advisor-based + inventory-based orphan detection. Flat status (pending/running/completed/failed).

**Target upgrades (12 ordered steps):**
1. memory.md (this file)
2. Pricing engine — live Azure Retail Prices API + 24h cache + fallback
3. Resource Graph KQL layer — server-side filtering + pagination
4. Cost Management API client + Advisor cross-validation
5. Assessment state machine + progress tracking
6. Findings engine — idle/oversized/unattached/RI, severity + confidence + Advisor correlation + debug_reason
7. Security / RBAC / audit logging middleware
8. Error handling — retry/backoff on 429, circuit breaker, safe user errors
9. Reports (PDF/Excel) — as-of timestamp, validation + confidence sections
10. Frontend — debug reasoning toggle + real progress UI
11. Full test suite — unit + integration with mocked Azure responses
12. Final memory.md review pass

**STATUS: all 12 steps complete.** Backend suite **96 tests green** (pytest); frontend `tsc` clean +
`vite build` OK.

**New backend modules (app/services/ unless noted):** `cache.py`, `pricing.py`, `kql.py`,
`metrics.py`, `cost_management.py`, `state_machine.py`, `resilience.py`, `audit.py`,
`app/logging_config.py`, `app/middleware.py`, `app/security/{rate_limit,rbac}.py`.
**Rewritten:** `inventory.py`, `findings.py`, `assessment.py`, `report.py`, `azure_client.py`.
**Migrations:** 002 (assessment progress/tenant), 003 (finding scoring), 004 (audit + dismissal).

**Data-flow (per assessment):** ARG server-side-filtered inventory + running-VM candidates →
Azure Monitor CPU metrics → Advisor recs → Cost Management actuals → live Retail prices →
FindingsEngine (severity + confidence + Advisor correlation + validation + gated debug_reason) →
rollup totals + needs_review_count → PDF/Excel. State machine persists progress + "as of" snapshot.

**How to run:**
- Backend tests: `cd backend && .venv/Scripts/python -m pytest -q`
- Backend dev: `uvicorn app.main:app --reload --port 8000`
- Frontend: `cd frontend && npm run dev` (build: `npm run build`)
- Apply migrations: `cd backend && alembic upgrade head`

**Flip debug reasoning OFF for prod:** set `DEBUG_FINDINGS_REASONING=false` (default). All
`debug_reason` generation is gated by this one flag; grep `TODO: remove or gate behind admin-only`
for the ~4 touch points (config, findings engine, db model, schema, frontend table).

---

## Decisions Log

_(append newest at the bottom)_

### Step 1 — memory.md scaffold
- **Built:** This file, with Overview / Decisions Log / Assumptions / Pending-Deferred sections.
- **Files:** `memory.md` (new, project root).
- **Decision:** Keep the log append-only and terse so a future session can reconstruct intent
  without re-reading every diff.
- **Assumptions:** None yet.
- **Pending:** Steps 2–12.

### Step 2 — Pricing engine (live Retail Prices API + cache + fallback)
- **Built:**
  - `cache.py` — `CacheBackend` Protocol + `InMemoryTTLCache` (thread-safe, per-key TTL,
    `get_stale()` for last-known-good). Swappable for Redis later.
  - `pricing.py` — `PricingEngine`: VM hourly/monthly, Reserved 1yr/3yr (amortised monthly),
    managed-disk by tier (size→P/E/S tier), public IP. Cached 24h; serves last-known-good on
    API failure; `PricingUnavailableError` only when API down AND no cache. `disk_tier_for_size()`
    pure helper. `get_pricing_engine()` singleton + `daily_refresh_loop()`.
  - Wired `lifespan` in `main.py` to start the daily refresh (guarded by `PRICING_DAILY_REFRESH`).
- **Files:** `app/services/cache.py` (new), `app/services/pricing.py` (new),
  `app/config.py` (+pricing/debug settings), `app/main.py` (lifespan + refresh task),
  `requirements.txt` (+pytest, pytest-asyncio), `pytest.ini` (new), `.env.example` (+pricing/debug),
  `tests/__init__.py`, `tests/azure_mocks.py` (new, MockTransport-based), `tests/test_pricing.py` (new).
- **Decisions / tradeoffs:**
  - Used httpx `MockTransport` for tests (no `respx`/network dep) → exercises real
    request/pagination code. Injected via `transport=` ctor arg (None in prod).
  - Retail API version `2023-01-01-preview` (needed for `reservationTerm`/`savingsPlan`).
  - Reservations: `retailPrice` = full term cost → monthly = total / (12 or 36).
  - Disks are billed per *tier* (per-disk/month), not per-GB → `disk_tier_for_size` maps size to
    the Azure spec breakpoints (stable, safe to hard-code). Per-GB `STATIC_FALLBACK` only when both
    API and cache miss (findings will down-weight confidence for this path in Step 6).
  - Month = 730 hours (Azure standard).
  - Expired cache entries are retained (not evicted) so `get_stale` works — bounded key set, OK.
- **Assumptions:** Retail Prices API is public/no-auth, USD default, `armRegionName` lower-case
  (e.g. `eastus`). Engine trusts server-side `$filter` for SKU scoping (verified via mock).
- **Tests:** 22 passing — tier mapping, cheapest-Linux selection (excludes Windows/Spot), RI
  amortisation, caching (1 network call for repeat), pagination (NextPageLink), last-known-good
  fallback, PricingUnavailableError, daily refresh.
- **Pending:** Redis backend; findings engine consuming this (Step 6).

---

### Step 3 — Resource Graph KQL layer (server-side filtering + pagination)
- **Built:**
  - `kql.py` — pure KQL builder functions. FILTERED queries push the predicate into ARG
    (`unattached_disks`, `orphaned_public_ips`, `idle_app_service_plans`, `deallocated_vms`,
    `paused_sql_databases`, `stopped_sql_managed_instances`). CANDIDATE query `running_vms`
    narrows to running VMs that need a metrics cross-check (idle/oversized/RI). `filtered_inventory_queries()` registry.
  - Rewrote `inventory.py`: `collect_inventory` now returns `(inventory, errors)` — dispatches all
    registry queries in parallel; a failed bucket is isolated (empty list + recorded error), never
    crashes the run.
  - `azure_client.py`: added `transport` ctor arg + `_client()` helper for test injection; ARG
    pagination via `$skipToken` documented/kept.
- **Files:** `app/services/kql.py` (new), `app/services/inventory.py` (rewritten),
  `app/services/azure_client.py` (transport injection), `tests/azure_mocks.py` (+ARG handlers),
  `tests/test_resource_graph.py` (new).
- **Decisions / tradeoffs:**
  - Bucket names map 1:1 to finding categories the Step 6 engine will consume.
  - `collect_inventory` signature changed to return `(inventory, errors)` → **assessment.py &
    findings.py still read the OLD keys until rewritten in Steps 5–6** (intentional intermediate
    state; app stays importable, just yields fewer inventory findings meanwhile).
  - Oversized/idle/RI need Azure Monitor metrics (ARG has no CPU data) → ARG only supplies
    candidates; metrics client + final decision land in Step 6.
- **Assumptions:** ARG surfaces VM power state at `properties.extended.instanceView.powerState.displayStatus`.
- **Tests:** 11 passing — each builder asserts the server-side `where`; ARG `$skipToken` pagination
  (1001 rows over 2 pages); `collect_inventory` routing; per-bucket failure isolation.
- **Pending:** wire new buckets into rewritten assessment/findings (Steps 5–6); metrics client.

### Step 4 — Cost Management API + Advisor cross-validation
- **Built:**
  - `azure_client.query_cost_management()` — POSTs an ActualCost query, follows `properties.nextLink`,
    treats 403/404 as "no data". `COST_MANAGEMENT_API=2023-11-01`.
  - `cost_management.py` — `build_cost_query()` (ActualCost grouped by ResourceId over trailing
    window), `parse_cost_rows()` (index columns by NAME, normalise cost to monthly ×30/days, sum
    per rid), `get_actual_cost_by_resource()`, and `validate_savings()`.
- **Files:** `app/services/azure_client.py` (+cost query), `app/services/cost_management.py` (new),
  `tests/azure_mocks.py` (+cost handler), `tests/test_cost_management.py` (new).
- **Decisions / tradeoffs:**
  - **Validation rule (concrete + testable):** actual monthly cost is the reference "trend". If an
    Advisor estimate exceeds actual cost by >tolerance (default 10%) → `needs_review` (you can't
    save more than you spend). Within tolerance → `validated`. No cost data / no resource id →
    `unvalidated`. Zero actual cost but non-zero estimate → `needs_review`.
  - Chose actual-cost-as-ceiling over a symmetric ±10% band because a legit saving is often *less*
    than the resource's full cost (rightsizing), so under-estimates must NOT be flagged.
  - `variance_pct = (estimate − actual)/actual×100` retained for the report's variance column.
  - Cost normalised to monthly so it's directly comparable to monthly savings estimates.
- **Assumptions:** Cost Management Query API available to Reader+CostReader; resource ids compared
  case-insensitively (ARM ids vary in case).
- **Tests:** 10 passing — query shape, column-by-name parsing, monthly normalisation+sum, all four
  validation branches, live fetch via mock, 403 no-access → empty. Full suite: **43 passing**.
- **Pending:** findings engine attaches ValidationResult per finding (Step 6); reports surface it (Step 9).

### Step 5 — Assessment state machine + progress tracking
- **Built:**
  - `state_machine.py` — `AssessmentState` enum (QUEUED→FETCHING_RESOURCES→FETCHING_METRICS→
    RUNNING_ADVISOR→CALCULATING_PRICES→DETECTING_FINDINGS→GENERATING_REPORT→COMPLETED, +FAILED),
    `PIPELINE`, `PROGRESS` (0–100), `MESSAGES`, `next_state()`, `is_valid_transition()`,
    and `ProgressTracker` (persists status/progress/message; stamps `snapshot_at` once at
    FETCHING_RESOURCES; sets `completed_at` on terminal; `.fail()` records error).
  - DB `Assessment`: +`tenant_id`, `progress`, `status_message`, `needs_review_count`,
    `snapshot_at`; default status now `queued`.
  - Migration `002_assessment_progress.py` (batch_alter for SQLite; adds cols + tenant index).
  - `schemas.AssessmentSummary`: +progress, status_message, needs_review_count, snapshot_at, tenant.
  - `dependencies.get_current_user`: extracts `tid` (tenant) claim.
  - `assessments.create_assessment`: sets status=queued/progress=0/tenant_id.
  - `conftest.py`: in-memory SQLite `db_session` fixture (StaticPool).
- **Files:** `app/services/state_machine.py` (new), `app/models/db.py`, `app/models/schemas.py`,
  `alembic/versions/002_assessment_progress.py` (new), `app/api/dependencies.py`,
  `app/api/routes/assessments.py`, `tests/conftest.py` (new), `tests/test_state_machine.py` (new).
- **Decisions / tradeoffs:**
  - `snapshot_at` = time resource fetching begins → the report's trustworthy "as of" time.
  - Added `tenant_id`/`needs_review_count` columns now (used in Steps 6/7) so only ONE more
    migration (Finding cols) is needed in Step 6.
  - Forward-skip transitions allowed (a phase can be skipped) but backward/terminal ones rejected.
  - Status values CHANGED (pending→queued, running→granular). **Frontend still expects old values
    until Step 10** — Results page will be updated there.
- **Assumptions:** delegated token carries `tid`. Naive UTC datetimes (matches existing columns).
- **Tests:** 9 passing — pipeline order, next_state, monotonic progress, valid/invalid transitions,
  tracker row updates, snapshot-set-once, completed_at, fail(). Full suite: **52 passing**.
- **Pending:** orchestrator (`assessment.py`) rewired to drive the tracker in Step 6.

### Step 6 — Findings engine (idle/oversized/unattached/RI + scoring + debug_reason)
- **Built:**
  - `azure_client.get_metric()` — Azure Monitor time-series (Percentage CPU), 403/404→[].
  - `metrics.py` — `get_vm_avg_cpu()`, `enrich_vms_with_metrics()` (parallel; per-VM failure →
    avg_cpu=None, not fatal). Datapoint count feeds confidence.
  - `findings.py` (full rewrite) — `FindingsEngine` with detectors:
    unattached disks, orphaned public IPs, idle App Service Plans, deallocated VMs, paused SQL,
    stopped SQL MI (ARG-authoritative); idle/oversized/RI via `detect_vm_utilisation_findings`
    (CPU thresholds 5% idle / 20% oversized / ≥20% steady RI); `advisor_findings()` re-scored.
    Pure scorers: `severity_from_savings` (critical≥300/high≥100/medium≥20/low; Advisor High can
    raise), `metrics_confidence` (0.3→0.95 by datapoints), `combine_confidence` (pricing miss ×0.7,
    needs_review caps 0.5, validated +0.05). `build_advisor_index` for correlation.
  - DB `Finding`: +confidence, advisor_recommendation_id, validation_status,
    validation_variance_pct, actual_monthly_cost, debug_reason. Migration `003_finding_scoring.py`.
  - `schemas.FindingResponse`: +new fields.
  - `assessment.py` (full rewrite) — drives the state machine through the pipeline; wires
    inventory→metrics→advisor→cost→pricing→engine; `_dedupe` drops our RI when Advisor covers the
    same VM; `_persist_findings_and_totals` rolls up totals + `needs_review_count`; strips
    engine-only keys to Finding columns via `_FINDING_COLUMNS`.
- **Files:** `app/services/azure_client.py` (+get_metric), `app/services/metrics.py` (new),
  `app/services/findings.py` (rewrite), `app/services/assessment.py` (rewrite),
  `app/models/db.py`, `app/models/schemas.py`, `alembic/versions/003_finding_scoring.py` (new),
  `tests/azure_mocks.py` (+metrics handler), `tests/test_findings.py` (new), `tests/test_metrics.py` (new).
- **Decisions / tradeoffs:**
  - `debug_reason` is set only when `debug=True` (from `DEBUG_FINDINGS_REASONING`); TODO-comment
    markers at the model column, engine builder, and schema. Easy to strip pre-prod.
  - Oversized savings = 50% of PAYG (one-tier-down heuristic) — flagged lower confidence; a proper
    SKU ladder is deferred.
  - App Service Plan price still a small static estimate table (retail ASP meter mapping deferred).
  - Deallocated VM / paused SQL / stopped MI carry $0 estimate (waste is disk/vCore, surfaced
    elsewhere) but still reported with high confidence + clear recommendation.
  - Advisor wins over our computed RI for the same VM (dedupe).
- **Assumptions:** CPU metric "Percentage CPU" at P1D over 7d; region defaults to eastus if missing.
- **Tests:** 18 (findings) + 4 (metrics) passing — severity/confidence bands, idle/oversized/RI,
  RI-not-emitted, no-metrics skip, advisor correlation both directions, validation caps/validated,
  debug gating, output keys ⊆ model columns. Full suite: **70 passing**.
- **Pending:** oversized SKU ladder; live ASP pricing; reports surface confidence/validation (Step 9);
  frontend shows debug/confidence (Step 10).

### Step 7 — Security / RBAC / audit logging
- **Built:**
  - `logging_config.py` — `JsonFormatter` (one JSON object/line) + contextvars request_id/user_id/
    tenant_id; `configure_logging()`.
  - `middleware.py` — `RequestContextMiddleware` sets request id, times request, emits access log,
    echoes `X-Request-Id`.
  - `security/rate_limit.py` — `RateLimiter` (sliding window, thread-safe, injectable clock) keyed
    `tenant:user`; `enforce_assessment_rate_limit` dependency → 429 + Retry-After.
  - `security/rbac.py` — `verify_subscription_access` (ARM only returns subs the caller has a role
    on → used as the Reader check); 403 on any inaccessible sub.
  - `services/audit.py` + `AuditLog` model — durable audit trail + structured log for
    assessment_run / finding_dismissed / report_downloaded.
  - Routes: `_owned_assessment()` enforces tenant+user isolation on every read/report/dismiss;
    `list_assessments` filters by tenant; create adds rate-limit dep + RBAC + audit; new
    `POST /{id}/findings/{fid}/dismiss` (marks dismissed_* + audits); downloads audited.
  - DB `Finding`: +dismissed/dismissed_by/dismissed_at. Migration `004_audit_and_dismissal.py`.
  - `main.py`: `configure_logging()` + `RequestContextMiddleware`.
- **Files:** `app/logging_config.py` (new), `app/middleware.py` (new), `app/security/` (new pkg:
  `rate_limit.py`, `rbac.py`), `app/services/audit.py` (new), `app/models/db.py` (+AuditLog,
  dismissal cols), `app/api/routes/assessments.py`, `app/config.py` (+rate/log settings),
  `app/main.py`, `alembic/versions/004_audit_and_dismissal.py` (new), `.env.example`,
  `tests/test_rate_limit.py`, `tests/test_rbac.py`, `tests/test_security_routes.py` (new).
- **Decisions / tradeoffs:**
  - Tenant isolation enforced by comparing BOTH user_id AND tenant_id on every owned lookup — a
    guessed assessment id from another tenant returns 404, not 403 (no existence leak).
  - RBAC via "can you list it" is pragmatic given delegated auth (no separate permissions call);
    documented. A per-action `Microsoft.Authorization/permissions` check is deferred.
  - Rate limiter + audit store are in-memory / DB now; Redis + external SIEM sink deferred.
  - `dismissed` stored as Integer 0/1 for SQLite friendliness.
- **Assumptions:** token `tid` claim is the tenant boundary; audit trail lives in app DB.
- **Tests:** 4 (rate limit) + 3 (rbac) + 5 (routes: cross-tenant read 404, list scoped, dismiss+
  audit, cross-tenant dismiss 404, create rate-limited 429 + 2 audited) passing. Full suite: **82**.
- **Pending:** Redis-backed limiter; external audit sink; fine-grained permission API check.

### Step 8 — Error handling (retry/backoff, circuit breaker, safe errors)
- **Built:**
  - `resilience.py` — `retry_request()` (retries 429/503 with exponential backoff + full jitter,
    honours `Retry-After`; transport errors retried then raised), `CircuitBreaker`
    (closed→open after N fails→half-open probe after cooldown→closed), `CircuitOpenError`.
    `_sleep` module-level for test patching.
  - `AzureClient` routed ALL requests through `_send()` (retry + per-client breaker); ctor gains
    `max_retries`/`base_delay`/`breaker`. 403/404 short-circuits preserved (non-retryable).
  - `main.py` exception handlers: `CircuitOpenError`→503 friendly; catch-all `Exception`→500 generic
    ("An unexpected error occurred", + request_id) with full detail logged, never leaking traces.
  - Config `azure_max_retries`/`azure_retry_base_delay`; wired into the assessment's AzureClient.
- **Files:** `app/services/resilience.py` (new), `app/services/azure_client.py` (resilient _send),
  `app/main.py` (handlers), `app/config.py`, `app/services/assessment.py`, `.env.example`,
  `tests/test_resilience.py` (new).
- **Decisions / tradeoffs:**
  - `retry_request` returns the final response (caller still calls `raise_for_status`) so existing
    403/404 handling in advisor/cost/metrics is unchanged.
  - Breaker is per-AzureClient (per assessment run) → a throttled run fails fast without poisoning
    other runs. Full-jitter backoff to avoid thundering herd.
  - Broad `Exception` handler is safe because FastAPI handles `HTTPException` separately, so 4xx
    business errors (401/403/404/429) keep their specific messages.
- **Assumptions:** ARM throttles via 429 + optional Retry-After; 503 also transient.
- **Tests:** 8 passing — 429 retry→success, give-up-returns-last, Retry-After honoured, transport
  retry+raise, breaker open/half-open/close, CircuitOpenError, AzureClient 429 retry. Full: **90**.
- **Pending:** none for this step (Redis-shared breaker state deferred with rate limiter).

### Step 9 — Reports (PDF/Excel) with as-of + validation + confidence
- **Built:** `report.py` full rewrite.
  - PDF: header shows "Data as of <snapshot_at>" + italic staleness note; new **Validation Summary**
    paragraph (validated/needs_review/unvalidated counts); findings table adds **Conf.** (%) and
    **Validation** (OK/REVIEW +variance) columns; REVIEW cells highlighted; supports "critical".
  - Excel: Summary sheet +snapshot + validation counts; Findings sheet +Confidence, Confidence
    Level, Validation, Variance %, Actual Monthly Cost, Advisor Rec ID; new **Validation** sheet
    listing needs-review findings (estimate vs actual + note).
  - Helpers: `_as_of`, `_confidence_label`, `_validation_short`, `_validation_counts`.
- **Files:** `app/services/report.py` (rewrite), `tests/test_report.py` (new).
- **Decisions:** confidence shown both numeric (0.95) and banded (High/Med/Low ≥0.8/≥0.5); validation
  note pulled from `details["validation_note"]`; graceful when snapshot_at is None ("N/A").
- **Tests:** 3 passing — PDF magic bytes + size, Excel 3 sheets w/ confidence+validation headers +
  snapshot + needs-review row, missing-snapshot PDF. Full suite: **93 passing**.
- **Pending:** none for reports.

### Step 10 — Frontend (debug toggle + progress + new fields)
- **Built:**
  - `types/index.ts` — `AssessmentStatus` now mirrors the backend state machine; `Severity` adds
    "critical"; `Finding` gains confidence/validation_*/actual_monthly_cost/advisor_recommendation_id/
    debug_reason; `AssessmentSummary` gains progress/status_message/snapshot_at/needs_review_count.
  - `Results.tsx` — `ProgressView` now uses real `progress` % + `status_message` from polling (no
    more fake mapping); `STATUS_CHIP`/`PHASES` cover all pipeline states; terminal check unchanged.
  - `FindingsTable.tsx` — **"Show debug info" toggle** (only rendered when findings actually carry
    debug_reason; off by default) reveals a dashed "Why this finding triggered (debug)" panel in the
    expanded row. Added **Confidence** column (colored % badge) and **Validation** column (OK/Review
    +variance chip). Shows correlated Advisor rec id in the expander. colSpans 7→9.
  - `api.ts` — added `dismissFinding()`.
- **Files:** `frontend/src/types/index.ts`, `frontend/src/pages/Results.tsx`,
  `frontend/src/components/FindingsTable.tsx`, `frontend/src/services/api.ts`.
- **Decisions:** debug toggle auto-hides in prod (no debug_reason → `hasDebug` false → no switch),
  reinforcing the single-flag gating. Confidence colored ≥0.8 green / ≥0.5 amber / else red.
  Kept dismiss as API method only (no button) to stay in scope; endpoint already audited/tested.
- **Tests:** `tsc --noEmit` clean + `vite build` succeeds (1970 modules). No JS unit-test harness in
  repo; build/typecheck is the gate.
- **Pending:** dismiss button UI; code-splitting (bundle >500kB warning, pre-existing).

### Step 11 — Full test suite (unit + integration)
- **Built:** `test_assessment_pipeline.py` — end-to-end: one composite `MockTransport` routes ALL
  Azure APIs by host+path (Retail Prices, subscriptions, ARG by KQL marker, Advisor, Cost Mgmt,
  Monitor metrics); in-memory DB via `monkeypatch` of `SessionLocal`/`AzureClient`/
  `get_pricing_engine`. Drives `run_assessment` and asserts state→COMPLETED, 3 findings
  (unattached disk/orphaned IP/idle VM), totals ($93.44/mo from live retail prices), all validated,
  inventory persisted; plus a needs_review scenario and a failure→FAILED scenario.
- **Files:** `tests/test_assessment_pipeline.py` (new).
- **Full suite breakdown (96 tests):** pricing 22, findings 14, resource_graph 11, cost_management
  10, state_machine 9, resilience 8, security_routes 5, metrics 4, rate_limit 4, assessment_pipeline
  3, rbac 3, report 3. All green. Frontend: `tsc` clean + `vite build` OK.
- **Decisions:** integration test asserts real dollar totals derived through the whole chain
  (ARG→metrics→pricing→validation→rollup), so a regression anywhere in the pipeline fails it.
- **Pending:** none.

### Post-ship fix — DB schema drift on existing dev cat.db
- **Symptom:** 500 on create assessment — `sqlite3.OperationalError: table assessments has no column
  named tenant_id`.
- **Cause:** the running `cat.db` was built by `Base.metadata.create_all()` on the OLD models and
  was never Alembic-tracked. `create_all` only creates missing *tables*, never adds columns to
  existing ones → assessments/findings kept their old columns while `audit_logs` (a new table) was
  created. So a plain `alembic upgrade` would also fail (004 re-creates existing audit_logs).
- **Fix (non-destructive, preserves rows):** `scripts/reconcile_db.py` — idempotent; ADDs only the
  missing columns to assessments/findings + `ix_assessments_tenant_id`. Then `alembic stamp head`
  (DB now at 004). Verified the failing INSERT succeeds.
- **Going forward:** a FRESH DB gets the full current schema from `create_all`; existing/old DBs use
  `python -m scripts.reconcile_db && alembic stamp head`. New schema changes → new Alembic migration
  (005+) + `alembic upgrade head`.
- **Files:** `backend/scripts/reconcile_db.py` (new).

### Post-ship — Executive dashboard v2 (REAL data only; mock removed)
- **Correction to first pass:** the initial redesign shipped a mock dataset + a public `/demo` route +
  a "current spend → optimized" hero. User (rightly) flagged that showing fabricated numbers is a
  client/job risk and that the CEO wants 1-glance clarity. **All mock removed.** Deleted:
  `mock/assessmentDemo.ts`, `pages/DashboardDemo.tsx`, `/demo` route (App.tsx reverted),
  `CostBreakdown.tsx`, `SavingsOpportunities.tsx`, `OptimizedSpend.tsx`.
- **What the dashboard shows now (100% real, from the `Assessment` API response):**
  - Hero = **Identified annual savings** (`total_savings_annual`) + monthly + findings/high-impact
    counts. The current-spend/optimized/%-reduction hero is GONE — those aren't truthfully knowable
    without a total-spend source, so instead there's an **honest one-line note**: "Total current
    spend and % reduction require Azure billing (Cost Management) access, not yet connected."
  - 4 real KPI tiles: Monthly Savings, Total Findings, High Impact, Needs Review.
  - **Where the Savings Are** — findings grouped into Compute/Storage/Databases/Network/Other via
    `area.ts` (category→area map), summing real `estimated_savings_annual`; clickable to filter.
  - **Recommendations** — real findings, expandable; "Why" = finding.description, "Recommended
    action" = finding.recommendation, real Confidence + Validation + Advisor-confirmed chips (fake
    "difficulty" dropped). Impact filter + search, sorted by savings.
- **Files:** new `area.ts`, `AreaBreakdown.tsx`; rewritten `tokens.ts`, `badges.tsx`,
  `ExecutiveSummary.tsx`, `RecommendationCard.tsx`, `DetailedFindings.tsx`, `AssessmentDashboard.tsx`
  (now takes `assessment: Assessment`), `primitives.tsx` (SectionHeader index dropped). `Results.tsx`
  passes real `assessment`; empty-findings → success state.
- **Verify:** tsc clean, build OK, no dangling refs. Still can't screenshot (no headless browser) —
  user views it by running a real assessment to completion.
- **Biggest remaining gap (what the tool lacks):** total current Azure spend + per-service spend
  (needs Cost Management "ActualCost" across ALL services, not just the finding slices). Until then
  the dashboard is savings-led, not spend-vs-savings. This is the honest limitation surfaced in the UI.

<details><summary>superseded first-pass notes (mock dashboard)</summary>
- **Built (all under `frontend/src/components/dashboard/` unless noted):**
  - `mock/assessmentDemo.ts` — sample dataset shaped like a future API response (12 findings across
    Compute/Storage/Network; category current+savings; `deriveSummary()` rollups). Swap this for real
    data later, nothing else changes.
  - `tokens.ts` — story colors (spend=blue, savings=green — stable everywhere), category accent hues
    (labeled tags only, never color-alone), currency/percent formatters, impact/difficulty tokens.
  - `primitives.tsx` — `SectionHeader` (numbered eyebrow), `StatTile` (KPI, `emphasis` wash),
    `StackedBar` (rounded ends + 2px surface gaps per dataviz), `LegendItem`.
  - `badges.tsx` — ImpactChip / DifficultyChip / CategoryTag.
  - `ExecutiveSummary.tsx` — hero spend flow (Current → −Savings → Optimized + % badge + before/after
    bar) and the 6 KPI cards.
  - `CostBreakdown.tsx` — 3 clickable category cards (Current/Savings/Optimized on a shared scale);
    clicking filters the whole story to that category.
  - `SavingsOpportunities.tsx` — top-4 opportunity cards (rank, savings, impact, summary).
  - `RecommendationCard.tsx` — expandable card; detail panel = Why (highlighted) + Current config +
    Recommended change + Expected impact + Estimated savings + difficulty/type/region chips.
  - `DetailedFindings.tsx` — recommendation list + impact chips + search.
  - `OptimizedSpend.tsx` — closing "after optimization" summary with strikethrough current.
  - `AssessmentDashboard.tsx` — composes the 5 numbered sections + holds the category filter + a
    "Sample data" disclosure chip.
  - `pages/DashboardDemo.tsx` + **public `/demo` route** (outside AuthGate) so the dashboard is
    viewable with no login and no backend — the point being to demo value before Azure integration.
  - `Results.tsx` completed-state now renders `<AssessmentDashboard/>` (replaced SummaryCards +
    FindingsTable; removed the now-unused by-category query). `SummaryCards.tsx`/`FindingsTable.tsx`
    kept in repo but no longer imported → main bundle dropped ~1.25MB → ~862KB (Recharts dropped).
- **Design decisions:** reused the existing dark theme as the design system (didn't swap in the
  dataviz placeholder palette); 2-color semantic (spend/savings) is CVD-safe + lightness-distinct;
  category identity always carries a text label. Charts kept minimal (stat tiles + 2 magnitude bars),
  no pie/dual-axis. Business-friendly `why` copy, not Azure jargon.
- **Verification:** `tsc --noEmit` clean; `vite build` OK; dev server boots and `/demo` returns 200.
  Could NOT screenshot (no headless browser in env) — user should eyeball `/demo` in a browser.
- **Pending/next:** wire the dashboard to REAL assessment data (map backend findings→category +
  add current-spend/optimized figures the API doesn't yet return); optional code-splitting; the PDF/
  Excel report still reflects real findings, not this mock story.
</details>

### Post-ship — Real total spend + per-area breakdown (Cost Management)
- **Context:** Correcting an earlier misstatement — the Cost Management client was ALREADY built
  (Step 4) and runs every assessment, but only for per-resource *validation*; total spend was never
  summed/stored/surfaced. This change surfaces it.
- **Backend built:**
  - `cost_management.py`: `build_service_cost_query` (ActualCost grouped by ServiceName),
    `parse_service_cost_rows`, `get_actual_cost_by_service`, `area_for_service` (keyword map →
    Compute/Storage/Databases/Network/Other), `spend_by_area`.
  - `assessment.py`: `_gather_service_costs` across subs; `_persist_findings_and_totals` now sets
    `current_monthly_spend`/`current_annual_spend`/`spend_by_area`/`cost_data_available` — ONLY when
    Cost Management returned data (403/no-access → cost_data_available=0, spend stays null).
  - DB `Assessment`: +current_monthly_spend, current_annual_spend, spend_by_area(JSON),
    cost_data_available. Migration `005_actual_spend.py` (applied to cat.db; alembic now at 005).
  - `schemas.AssessmentSummary`: +those fields.
- **Frontend built:**
  - `types`: +spend fields on AssessmentSummary.
  - `ExecutiveSummary.tsx`: **two modes** — if `cost_data_available` → real spend story
    (Current → −Savings → Optimized + real savings % + before/after bar + 6 KPI tiles incl. current
    spend). Else → savings-led hero + honest note "billing (Cost Management) data wasn't available…
    grant Cost Management Reader." No fabricated numbers in either mode.
  - `AreaBreakdown.tsx`: shows each area's real annualized spend + savings-as-%-of-spend when
    available; falls back to %-of-savings otherwise.
- **Key point (why findings work without cost access):** findings come from Resource Graph + Monitor
  metrics + Advisor + public Retail Prices — none need billing access. Cost Management only adds
  validation + total-spend. So a scan yields real savings even when the user lacks cost access.
- **Caveats (documented honestly in UI):** service-grouped ActualCost ≈ bill but not the exact
  invoice (marketplace/tax/support edge cases); area mapping is keyword-based best-effort.
- **Tests:** +9 (cost_management: area map, service query/parse, spend_by_area, live fetch) and
  pipeline: asserts current_monthly_spend=48000/annual=576000/spend_by_area + a no-cost-access case
  (findings still produced, spend null). Full backend suite: **102 passing**. tsc + build clean.
- **Still can't screenshot** (no browser in env) — user verifies by running a real assessment.

### Post-ship — Fix: savings could exceed spend (negative optimized / >100%)
- **Bug (seen on a real $30k/yr single-sub assessment):** identified savings ($33k) > measured
  spend ($30k) → hero showed "Optimized −$3.7K / 112% reduction". Impossible + credibility-killing.
- **Root causes:** (1) per-finding savings were list-price estimates never clamped to the resource's
  actual billed cost, so on small/dev subs they out-total the bill; (2) the UI rendered the
  impossible number instead of catching it.
- **Fixes:**
  - **Backend cap** (`findings.py::_finding`): when a resource's actual monthly cost is known
    (from the validation cost_map), clamp `estimated_savings_monthly` to it — "you can't save more
    than it costs". Sets `details.savings_capped_at_actual_cost`. Validation status/variance still
    reflect the ORIGINAL estimate (so the overage is recorded). Tests: +2 (cap applies / no cap when
    estimate below actual). Suite: **104 passing**.
  - **Frontend guardrail** (`ExecutiveSummary.tsx`): only show the Current→Optimized→% story when
    `savingsAnnual <= currentAnnual`. If savings exceed spend → savings-led view + real current-spend
    tile + amber caveat ("identified savings exceed measured spend… treat as upper bound"). Never
    shows negative optimized or >100%. `AreaBreakdown` caption guarded the same way.
- **IMPORTANT for existing assessments:** the cap only applies to assessments run AFTER this change.
  Old rows (e.g. #13) keep their uncapped savings in the DB — refreshing them now shows the honest
  caveat (no more negative/112%), but a NEW assessment is needed to get corrected/capped savings.

### Post-ship — Diagnosis + fixes for inflated savings (assessment #14)
- **Observed:** $33k savings vs $30k spend. Swing factor = 3× `Standard_D16s_v3` "HyperV-Demo" VMs,
  each $6,728/yr, with NO matched actual cost (so the Step-cap couldn't apply). Smaller VMs that DID
  match actual cost were validated + sensible (e.g. "Cost-validated −38%"). So inflation = idle-VM
  findings priced at LIST price when the resource's actual cost isn't found in Cost Management.
- **Fixes:**
  - `assessment.py::_dedupe` rewritten → **at most one finding per resource_id** (keep highest
    savings), case-insensitive. Prevents our idle/oversize/RI finding + an Advisor finding on the same
    VM from both counting. (was: only ri_vm-vs-advisor.) Test added.
  - `badges.tsx::ValidationChip` → un-validated findings with savings>0 now show a muted
    **"Not cost-validated"** chip (was: nothing), so list-price-only estimates are visibly flagged vs
    "Cost-validated (−X%)" ones. Directly answers "which of these numbers can I trust."
- **Root cause is partly environmental (can't fix in code):** those 3 VMs likely (a) cost less than
  list price (reservation / Azure Hybrid Benefit), (b) were recently created so trailing-30-day actual
  is low while savings annualize current run-rate (time-window mismatch), or (c) a resource-id
  mismatch (less likely — other VMs matched). User to verify those 3 VMs' actual cost in Cost Analysis.
- **Only applies to NEW assessments.** Tests: **105 passing**; tsc+build clean.
- **Possible next step (not done):** headline could show a "cost-validated savings" subtotal vs total
  upper-bound; or discount unvalidated estimates from the headline. Deferred pending user call.

### Post-ship — VM utilisation methodology fix (peak CPU / 30 days)
- **User (correct) critique:** engine flagged VMs "idle → delete" off AVERAGE CPU over 7 days. Real
  data: HyperV-Demo avg 1.24% but PEAK 43.6%/30d; HyperV-Demo2 max 31%; HyperV-Demo3 max 23%; CRA-VM
  max 88%. Averaging hid scheduled/batch spikes → false "idle" on VMs doing real work → dangerous
  "delete" advice AND the inflated $33k.
- **Fix:** classify on **peak (max) CPU over 30 days**, not average.
  - `metrics.py`: window 7→30; `get_vm_cpu_stats()` fetches BOTH Average and Maximum (2 Monitor
    calls/VM); enrich attaches `avg_cpu` + `max_cpu`. `max_cpu` = highest peak across window.
  - `findings.py`: `detect_vm_utilisation_findings` now:
    peak <5% → idle (deallocate, full-cost saving); 5–40% → oversized (downsize one tier, ~50% saving);
    **≥40% → NO finding** (well-utilised; RI/steady-state left to Azure Advisor). Removed the weak
    home-grown RI heuristic (CPU isn't the right RI signal). Debug/descr text now cite peak+avg+30d.
  - `azure_mocks.metrics_handler` returns the series matching the requested aggregation; pipeline +
    findings + metrics tests updated (idle now = low PEAK; added spiky-VM-not-idle + well-utilised
    no-finding tests; removed RI tests).
- **Effect on the real case:** HyperV-Demo (peak 44%) & CRA-VM (peak 88%) → no longer flagged (removes
  the big false savings); HyperV-Demo2/3 (peak 31/23%) → oversized/downsize with partial, actual-cost-
  capped savings. Total identified savings will drop to a defensible number.
- **Tests: 105 passing**; frontend build clean. Applies to NEW assessments only.

### Post-ship — Real rightsizing: CPU + memory, named target SKU, real price delta
- **Design driven by user, refined through discussion (not just a code request):** three requirements —
  (1) unify CPU AND memory signals (not CPU alone — a memory-bound VM, e.g. a cache, can be CPU-idle
  while doing real work); (2) name a SPECIFIC target SKU, not vague "downsize"; (3) savings = REAL
  price delta (current SKU price − target SKU price), not a flat 50% heuristic.
- **Metric correction mid-build:** initially planned to fetch "Available Memory Bytes" + convert via
  SKU RAM lookup (assumed guest-agent-only metric). User showed a live portal screenshot: **"Available
  Memory Percentage" is a DIRECT metric**, same simple ARM metrics call as CPU, unit %, no agent/byte-
  conversion assumption needed. Corrected before building — `metrics.py` fetches it directly.
- **Threshold design (discussed before coding):** idle = peak CPU ≤5% AND peak memory used ≤10%
  (flat bars — memory bar looser than CPU's because OS baseline overhead alone often sits 5–15%).
  Downsize = NOT a flat pre-resize %; it's a **unified 70% headroom ceiling checked on the CANDIDATE
  SKU** for both CPU and memory independently (not simultaneous peaks — worst-case per metric).
  Walk the same-series ladder largest→smallest, take the smallest that clears both.
- **Built:**
  - `vm_specs.py` (new) — curated vCPU/RAM table (D/E/Fsv2/v3/v5, B-series), `get_spec()`,
    `smaller_same_series()` (ladder, largest→smallest). Documented tradeoff: static table, not the
    live `Microsoft.Compute/skus` API (deferred, same pattern as other static tables in this repo).
  - `metrics.py` rewrite — `get_vm_cpu_stats` (avg+max, unchanged), NEW `get_vm_memory_stats` (fetches
    "Available Memory Percentage", Minimum aggregation = worst-case moment, `peak_used = 100 - min`),
    `VmUtilisation` dataclass-like holder with `.memory_available` flag, `get_vm_utilisation()`
    combines both in parallel. `enrich_vms_with_metrics` attaches peak_memory_used_pct +
    memory_available per VM. Missing memory data ⇒ `None`, never assumed 0%.
  - `findings.py` — `find_downsize_target()` (pure: cores/GB used → walk ladder → smallest SKU
    clearing 70% ceiling on both, memory check skipped if unknown). `detect_vm_utilisation_findings`
    rewritten: idle requires BOTH signals confirmed low; else ladder-walk for a target; savings =
    `get_vm_monthly_price(current) − get_vm_monthly_price(target)` (two real pricing calls); no
    candidate fits ⇒ no finding; target price unavailable or non-positive delta ⇒ skip (never guess).
    **Memory-unavailable policy (the fork flagged during design):** NOT suppressed, NOT treated as
    idle — CPU-only ladder-walk still runs, confidence reduced ×0.7, `details.memory_verified=False`,
    description/debug_reason carry an explicit caveat. Removed the old flat-50%-heuristic and the
    weak CPU-only RI branch entirely (RI/steady-state left to Azure Advisor, which already does it
    properly with Microsoft's own data).
  - Mocks: `azure_mocks.metrics_handler` now routes by `metricnames`+`aggregation` (CPU average/max,
    memory minimum via `memory_available_values`); pipeline composite handler updated to match.
  - Frontend `RecommendationCard.tsx`: new "Recommended size change" block shows the literal
    `current_sku → recommended_sku` (monospace) + peak CPU/memory line, only when the backend
    supplied a real target (`details.current_sku`/`recommended_sku`); "Memory not verified" warning
    chip when `details.memory_verified === false`.
- **Verified against the real incident:** re-ran the exact HyperV-Demo (peak CPU 44%) and CRA-VM
  (peak CPU 88%) numbers as test cases — both now correctly produce **no finding** (well-utilised;
  even the next-smaller SKU can't absorb that peak), instead of the old "idle → delete $6.7K". Also
  added the motivating "Redis cache" case (CPU 2%, memory 85%) as a test — correctly NOT idle, NOT
  downsizable (memory doesn't fit anywhere smaller either) — proves the two-signal fix works both ways.
- **Tests: 120 passing** (7 vm_specs, 9 metrics, 19 findings incl. all worked examples from the
  design discussion + the memory-unavailable case, 5 pipeline). Frontend tsc + build clean.
- **Pending:** live `Microsoft.Compute/skus` API (replace static vm_specs table); asymmetric
  CPU/memory downsize ceilings (memory stricter, e.g. 60%) if real-fleet data supports it; cross-
  series downsize recommendations (currently same-series ladder only, by design — safer/simpler
  in-place resize).

### Post-ship — Full test + security/pentest pass (bugs fixed)
- **Scope:** unit + integration + penetration testing across the whole tool; fix everything found.
  Backend suite grew 120 → **158 passing** (17 files); frontend tsc+build clean.
- **🔴 CRITICAL fixed — tenant-isolation bypass via unsigned JWT.** `get_current_user` decoded tokens
  with `verify_signature:false`; read/report/dismiss endpoints authorize on `oid`/`tid` alone (never
  call Azure), so a forged token with a victim's claims read their data. **Fix:** `security/token.py`
  `TokenVerifier` — fetches Azure AD JWKS (common OIDC metadata), verifies RS256 signature + expiry
  (+ optional audience), pins RS256 (blocks alg=none / HS256-confusion), caches keys w/ rotation
  refresh. `verify_token_signature` setting **defaults TRUE** (secure by default); dependencies.py
  uses it; main.py logs a loud warning if disabled. Tests: `test_token_auth.py` (9) + HTTP-layer
  `test_security_pentest.py` (17: forged→401, cross-tenant→404, same-oid-diff-tenant→404, etc.).
- **🟠 MEDIUM fixed — no input validation on `subscription_ids`.** Added pydantic `field_validator`
  (GUID format, max 50, dedupe, non-empty) → junk/injection/oversized payloads now 422 before
  reaching ARM URLs / fan-out. Covered by parametrized pentest cases (path/sql-ish/bad-hex/too-many).
- **🟡 LOW fixed — token error message leaked exception detail** → now generic "Invalid or expired
  token"; **rate-limiter unbounded memory** → opportunistic eviction of aged-out keys past 1024.
- **Error handling** extracted to `errors.py` (`register_error_handlers`) + `test_error_handling.py`
  proving 500/503 never leak secrets/exception types/tracebacks (only generic msg + request_id).
- **Dependency audits:** `npm audit` = 2 moderate react-router + esbuild(dev-server-only) advisories;
  all require BREAKING major upgrades (vite8 / react-router7). Assessed **not exploitable in this
  app** (no SSR; navigation only to hardcoded internal paths; esbuild issue is dev-server-only, not
  in the prod bundle) → deliberately NOT force-upgraded. `pip`: `cryptography` pinned in
  requirements (already present) for RS256.
- **Files:** `app/security/token.py` (new), `app/errors.py` (new), `app/api/dependencies.py`
  (rewrite), `app/config.py` (+token/security settings), `app/main.py` (register handlers + warning),
  `app/models/schemas.py` (+subscription validator), `app/security/rate_limit.py` (eviction),
  `requirements.txt`, `.env.example`; tests: `jwt_helpers.py`, `test_token_auth.py`,
  `test_security_pentest.py`, `test_api_integration.py`, `test_error_handling.py` (all new).
- **Config note:** `VERIFY_TOKEN_SIGNATURE=true` by default — real Azure ARM tokens verify against
  Microsoft JWKS (network at first request, then cached). Set false ONLY for local/offline dev.

### Post-ship — UI redesign: light "turbo360-inspired" theme
- **Ask:** current dark UI felt basic; make it like turbo360.com. Fetched turbo360.com → light theme,
  cyan/teal brand accent, deep-navy text, white cards, soft shadows, generous whitespace, blue→teal
  gradients.
- **Approach:** the app's styling is centralized in `theme.ts` (`colors` token object + MUI overrides
  that components consume), so flipping the tokens cascades to ~everything. Rewrote `theme.ts`:
  light palette (bg #F4F7FB, white surfaces, #E6EBF2 borders, brand teal `accentBlue`=#0AA6BA, navy
  text #0F1B2E), `mode:"light"`, soft card shadows (replaced dark borders-only), teal gradient
  buttons, light table heads, navy tooltips, exported `gradients.brand`
  (`linear-gradient(135deg,#16C8DA→#0A97B6)`), SEVERITY retuned for light (tinted fill + dark text).
- **Component fixes beyond the cascade:** replaced the 3 hardcoded `#3B82F6→#6366F1` gradients
  (Login logo, Layout logo+avatar, Results progress icon) with `gradients.brand`; Layout sidebar
  → white (`surface`) with border, user card → `surfaceElevated`; SelectSubscriptions row hover had
  hardcoded dark slate (`#475569`/`#2c3e54`) → teal-tint. Dashboard `SPEND_COLOR` pinned to a
  distinct data-blue `#3B82F6` (decoupled from the now-teal brand) so spend-vs-savings stays clear.
- **Files:** `theme.ts` (rewrite), `components/dashboard/tokens.ts`, `pages/Login.tsx`,
  `pages/Results.tsx`, `pages/SelectSubscriptions.tsx`, `components/Layout.tsx`.
- **Verify:** tsc + build clean. Grep confirms no stray dark hexes remain in live components (only
  the intentional data-blue). **Could not screenshot** (no browser) — user reviews by running dev.
- **Unused legacy:** `SummaryCards.tsx`/`FindingsTable.tsx` still in repo (not rendered) — left as-is.

### Post-ship — Immersive, chart-rich dashboard (visualize every detail)
- **Ask:** turbo360 visualizes each detail; make the UX immersive/excellent, not just recolored.
- **Principle:** the engine already captures WHY each finding fired (30-day peak CPU/mem, current→
  target SKU, real prices) → turn that evidence into per-finding charts. Grounded in the dataviz
  skill (form-by-job, existing theme palette, labeled legends, thin marks).
- **Backend (small):** downsize finding `details` now also carries `current_vcpu/current_memory_gb/
  recommended_vcpu/recommended_memory_gb/downsize_ceiling_pct` (from the spec objects already known
  in `find_downsize_target`) so the frontend can draw the before/after without a SKU table. Test
  updated to assert them.
- **Frontend chart primitives** (`components/dashboard/charts/`): `Meter` (div bar + threshold
  marker + "not measured" state), `Donut` (Recharts ring + center value + tooltip), `HBars`
  (horizontal bar list), `CompareRow` (before→after proportional bars).
- **Executive insights row** (`InsightsRow.tsx`, added to AssessmentDashboard): "Savings by area"
  donut (legend + $ + %) and "Findings by impact" bars (severity colors). `rollupByArea` helper
  moved to `area.ts` and shared (AreaRollup interface too; AreaBreakdown imports it now).
- **Per-finding evidence panel** (`FindingEvidence.tsx`, in expanded RecommendationCard) — the
  centerpiece: idle/oversized VMs show **30-day peak CPU + peak memory meters** (70% ceiling marker
  on downsize, "not measured" when memory absent); downsize adds a **current→recommended SKU
  headline** + **capacity before/after bars** (vCPU, RAM); orphaned/idle resources with a known
  actual cost show a **cost-impact bar** ("% of this resource's cost eliminated"). Replaced the old
  plain "recommended size change" text block.
- **Files:** `findings.py` (+specs in details), `tests/test_findings.py`; new
  `charts/{Meter,Donut,HBars,CompareRow}.tsx`, `InsightsRow.tsx`, `FindingEvidence.tsx`; edited
  `area.ts`, `AreaBreakdown.tsx`, `AssessmentDashboard.tsx`, `RecommendationCard.tsx`.
- **Verify:** backend **158 passing**; tsc + build clean (bundle grew ~ Recharts now used —
  code-splitting deferred). Could not screenshot — user reviews by running dev.
- **Ideas if they want more:** spend-vs-savings grouped bar per area (when cost data present),
  a savings % gauge, confidence ring on cards, sparklines. Deferred pending review.

### Post-ship — Broad resource coverage + DRY refactor
- **Ask:** identify every Azure resource, not just high-hitters (VM/App Service/VPN); optimize the tool.
- **Full inventory:** new `all_resources_summary_query()` (`Resources | summarize count() by type`);
  `_gather_inventory_summary()` in the orchestrator → `total_resources` + `resource_type_count`
  stored on Assessment (migration 006, schema, types). Frontend shows a coverage banner: "Scanned N
  Azure resources across M types · F opportunities identified."
- **Broadened detection (7 new resource types)** via server-side-filtered ARG queries in `kql.py`,
  added to the registry: orphaned snapshots, empty (Standard) load balancers, idle NAT gateways,
  Azure Bastion (review), orphaned NICs, orphaned NSGs, orphaned route tables.
- **Optimization = DRY rule engine:** instead of a bespoke method per type, added `OrphanRule`
  dataclass + `ORPHAN_RULES` table + one `FindingsEngine.detect_orphans(bucket, rows)` method.
  Cost-bearing rules (snapshot=size×$0.05/GB, empty LB=$18, NAT gw=$32, bastion=$138) carry fixed
  estimates (grounded by the actual-cost cap + validation); hygiene rules (NIC/NSG/route table) carry
  $0, low severity — reported for completeness, not savings. New categories added to CATEGORY_DISPLAY
  and to the frontend `area.ts` map (network/storage).
- **Files:** `kql.py`, `findings.py`, `assessment.py`, `models/db.py`, `models/schemas.py`,
  `alembic/versions/006_inventory_totals.py` (new, applied), `frontend types/index.ts`,
  `components/dashboard/area.ts`, `AssessmentDashboard.tsx`; tests: `test_resource_graph.py` (+registry
  +new-query filters +summary), `test_findings.py` (+5 orphan-rule tests), `test_assessment_pipeline.py`
  (+summary rows → total_resources assertion).
- **Honest caveats (documented):** the new orphan-detection ARG property paths are BEST-EFFORT
  (e.g. NSG `subnets`/`networkInterfaces`, LB `backendAddressPools`) — can't validate against live
  Azure here; verify against a real environment. Fixed cost estimates for LB/NAT/bastion are
  approximations. Retail pricing for these types (like ASP) is a future enhancement.
- **Verify:** backend **165 passing**; tsc + build clean. Adds ~7 more parallel ARG queries/assessment
  (protected by the retry/circuit-breaker layer).

### Post-ship — Coverage/UX fixes from user review
- **Donut tooltip overlapped center label** → removed the Recharts `<Tooltip>` from `Donut.tsx`
  (the legend beside it already lists every value + %); dropped the now-unused `formatter` prop +
  InsightsRow arg.
- **Dropped $0 hygiene resources** (orphaned NICs/NSGs/route tables) — a *cost* tool shouldn't list
  no-cost resources. Removed their KQL queries + registry entries, `ORPHAN_RULES`, CATEGORY_DISPLAY,
  and `area.ts` mappings. Kept cost-bearing: snapshots, empty LBs, idle NAT gateways, Bastion.
- **Resource count over-counted (197 vs portal 172)** — Resource Graph lists child `/extensions`
  resources (e.g. the Azure Monitor Agent, ~1/VM) that the portal hides. `all_resources_summary_query`
  now filters `where type !endswith '/extensions'` so the count matches the portal. (A few hidden
  types may still differ slightly — acceptable/documented.)
- **Downsize threshold: LEFT UNCHANGED** at 70%-headroom-on-target per user's explicit instruction.
  Open decision for later: user floated "50% peak CPU & 50% available memory"; the key nuance is
  applying it to the *target* SKU (safe) vs the *current* VM (risky, since one tier down ~halves
  capacity). Revisit when they decide.
- **Verify:** backend **165 passing**; tsc + build clean. Tests updated (registry set, broad-coverage
  query asserts, summary excludes-extensions, NAT-gateway estimate replacing the removed NIC test).

### Post-ship — Dashboard interactivity + motion (felt too static)
- **Interactive donut (restored hover, bug-free):** rewrote `Donut.tsx` — hovering a slice (or its
  legend row) pops the slice (`activeShape` Sector) and swaps the CENTER label to that segment's
  name/value/%. No floating tooltip → the earlier center-overlap bug is gone by design. `activeIndex`
  is controlled by `InsightsRow` so slice↔legend hover is bidirectional (active row highlighted,
  others dimmed).
- **Count-up numbers:** new `charts/AnimatedValue.tsx` (rAF + easeOutCubic, honors
  prefers-reduced-motion). Applied to every headline number in `ExecutiveSummary` (current/optimized
  spend, savings, % reduction, findings, high-impact) — dashboard "comes alive" on load. `StatTile`
  `value` prop widened to `ReactNode`.
- **Motion + hover polish:** `charts/useMounted.ts` drives entrance grow-from-0 on the hero
  before/after `StackedBar`, the impact `HBars`, and the utilisation `Meter` (0.7–0.8s easing).
  Global subtle card-hover shadow in theme; `StatTile` gets a stronger translateY lift + accent
  border on hover; HBars brighten on hover.
- **Files:** `charts/{Donut,AnimatedValue,useMounted}.tsx/ts`, `InsightsRow.tsx`, `ExecutiveSummary.tsx`,
  `primitives.tsx` (StatTile ReactNode + hover + StackedBar grow), `charts/{HBars,Meter}.tsx`,
  `theme.ts` (card hover).
- **Verify:** tsc + build clean. Could not screenshot — user reviews live. Existing interactivity
  (clickable area filter, expandable finding cards w/ evidence charts) unchanged.

### Post-ship — Reserved Instances, Savings Plan, Windows AHB, Backup redundancy (independent)
- **Ask (with a spec table):** the tool should compute RI (1&3yr), Savings Plan, right-sizing, and
  Windows AHB itself, not just rely on Advisor (user's dev env → Advisor returns nothing).
- **User decisions:** (Q1 reservation basis) "include all three, integrate them" → configurable
  `reservation_basis` (combined default) that fuses measured-usage + always-on fallback + Advisor.
  (Q2 scope) build VM RI/SP/AHB/backup independently NOW; deeper services (SQL/ASP/MySQL/Cosmos/Files)
  stay via Advisor passthrough.
- **Built:**
  - Config `reservation_basis: combined|measured|always_on|advisor`.
  - `pricing.py`: `get_vm_windows_monthly_price` (Windows image = compute+licence, for AHB delta),
    `get_vm_savings_plan_monthly_price` (reads the retail item's `savingsPlan[term]` hourly rate).
  - `kql.py`: `windows_vms_without_ahb_query` (running Windows VMs where licenseType != Windows_Server),
    `geo_redundant_vaults_query` (best-effort via `properties.redundancySettings`). Registry +2.
  - `findings.py`:
    - `detect_vm_commitments(running_vms)` — one finding per steady VM; headline = 1yr RI saving,
      with 3yr RI + 1yr Savings Plan as alternatives in `details.reservation_options` (mutually
      exclusive → never summed). Basis gating: measured→steady only; combined→always but conf 0.85
      steady / 0.55 unconfirmed; always_on→always; advisor→none. Skips idle VMs; **Advisor wins**
      (skip if advisor_index has the VM). Uptime proxy = cpu_datapoints ≥ 0.8×window.
    - `detect_windows_ahb(windows_bucket)` — saving = Windows − Linux monthly price.
    - `geo_redundant_vaults` OrphanRule → `backup_redundancy` (nominal $25/mo, conf 0.4; real saving
      needs backup volume, unavailable from ARG).
    - CATEGORY_DISPLAY +windows_ahb, backup_redundancy; ri_vm/savings_plan_vm relabelled.
    - `FindingsEngine(reservation_basis=...)`.
  - `assessment.py`: wires `detect_vm_commitments` + `detect_windows_ahb`; passes basis.
  - Frontend `area.ts`: windows_ahb→Compute, backup_redundancy→Storage.
- **Coverage now:** RI (VM), Savings Plan (VM), Windows AHB, Backup redundancy computed independently
  + all existing orphan/right-sizing + Advisor passthrough for the deep services. Dedupe-by-resource
  prevents RI vs downsize vs idle double-counting on the same VM (keeps highest-value action).
- **Caveats (documented):** RI/SP savings use list pricing (no negotiated discounts); Savings Plan
  only appears when the retail `savingsPlan` array is present; AHB assumes the customer HAS eligible
  licences; backup-redundancy saving is a placeholder; GRS-vault + AHB KQL property paths are
  best-effort (verify against real Azure). Deep-service independent RI/right-sizing = future (Advisor
  covers them meanwhile).
- **Verify:** backend **176 passing** (+11); tsc + build clean.

### Post-ship 2 — real-env feedback (Assessment #22): AHB clutter + missing RI
- **What the user saw (real run, 185 res / 19 findings, $31K savings ≈ $31K spend):** ~12 separate
  per-VM "Windows Azure Hybrid Benefit" findings cluttering the list; Savings Plan findings present
  but **zero Reserved Instance** recommendations; long `$30,706` cramped in the donut centre on hover.
- **Root cause of missing RI (important):** Windows D-series VMs got BOTH a per-VM AHB finding and an
  RI finding, but **dedupe-by-resource keeps only the highest** → AHB ($6.4K) evicted the RI. And
  burstable B-series VMs (B2ms…) genuinely aren't RI-eligible, so they correctly showed only Savings
  Plan. Net effect: no RI anywhere. Also retail Reservation pricing is often absent per SKU/region,
  which would have dropped RI even without the dedupe collision.
- **Fixes (findings.py):**
  - `detect_windows_ahb` now returns **ONE aggregated finding** (resource_id=None → escapes dedupe,
    so per-VM RI findings survive). Lists every eligible Windows VM in `details.eligible_vms`
    (name/sku/region/monthly_savings), savings = summed Windows−Linux delta, conf 0.7. AHB (licence)
    is **additive** with RI (compute) on the same VM — that's why it must not dedupe against RI.
  - `detect_vm_commitments`: **RI is the headline** (1yr) with 3yr + Savings Plan as alternatives.
    Added an **estimate fallback**: for non-burstable VMs, if retail RI price is None →
    ri1=payg×0.60 (~40% off), ri3=payg×0.43 (~57% off), `details.ri_price_estimated=True`, confidence
    ×0.85. Burstable (spec.family startswith "B") never gets an RI estimate → falls to Savings Plan
    headline. So RI now reliably appears for D/E/F-series steady VMs.
- **Frontend:** `FindingEvidence.tsx` +2 panels: "Commitment options" (1yr RI / 3yr RI / Savings Plan
  each with $/yr + $/mo, best highlighted, est. note) and aggregated-AHB "eligible VMs" list.
  `InsightsRow` donut `format` → `fmtCompact` (center shows `$31K` not cramped `$30,706`).
- **Data note:** AHB numbers verified realistic (D16 Windows premium ≈16 vCPU×$0.046/hr×730 ≈ $537/mo
  = $6.4K/yr). Savings ≈ spend is honest (AHB assumes owned licences; RI/AHB are additive potential) —
  the existing savings>spend caveat covers it.
- **Verify:** backend **179 passing** (+3 net); tsc + build clean.
- **Still deferred (user aware):** SQL Server / SQL MI AHB eligibility (mentioned but not yet computed
  — needs licenseType=LicenseIncluded detection + SQL licence pricing); deep-service independent
  RI/right-sizing.

### Post-ship 3 — additional Azure APIs for accuracy (user: "use all apis you need")
Grounded against MS Learn docs before building. Sequenced; each verified.
- **#1 DONE — Consumption `reservationRecommendations`** (the headline accuracy win):
  - `azure_client.get_reservation_recommendations(sub, scope="Single")` — GET
    `/subscriptions/{id}/providers/Microsoft.Consumption/reservationRecommendations?api-version=2023-05-01&$filter=properties/scope eq 'Single'`; follows nextLink; 403/404→[] (same pattern as Advisor/Cost).
  - **Why authoritative:** Azure simulates ACTUAL hourly usage over 7/30/60 days, at the customer's
    REAL negotiated prices, and **excludes reservations already owned**, returning the quantity that
    maximises savings — per SKU/region, both terms. Covers VMs, SQL DB/DW, Managed Disk, MySQL/PG/
    MariaDB, Cosmos, Redis, App Service, BlockBlob. This replaces my retail `payg×0.6` RI estimate.
  - **`reservations.py` (new, pure/tested):** `parse_reservation_recommendations(items, sub)` →
    groups per (resourceType, SKU, region, scope), holding both terms. Handles **legacy** (flat
    decimals, subscription scope) AND **modern** ({currency,value} objects, billing scope).
    netSavings/costs are over the look-back window → normalise to monthly `×30/lookBackDays`; keep
    one entry per term preferring 30d>60d>7d. resourceType→category via `_RT_META`.
  - **`findings.commitments_from_recommendations(groups)`** → one finding/(SKU,region): 1yr headline,
    3yr in `details.reservation_options`; resource_id=None (SKU-level purchase → escapes dedupe);
    conf 0.9; `details.source="azure_reservation_recommendations"` + monthly_ondemand/reserved/qty.
    CATEGORY_DISPLAY +7 reserved-capacity labels (sql_db/sql_mi/managed_disk/mysql/cosmos/app_service/
    azure_files) — all already area-mapped in frontend area.ts.
  - **Orchestration (`assessment._detect_all`):** gather recs per sub → `commitments_from_recommendations`;
    run retail `detect_vm_commitments` ONLY when Azure returns no VM rec (fallback). Non-VM RIs
    (SQL/Cosmos/etc.) now come from Azure directly → **this also delivers the deferred deep-service RI.**
  - Frontend `FindingEvidence`: commitment panel shows Azure provenance + on-demand→reserved line when
    `source=azure_reservation_recommendations`.
  - Verify: backend **190 passing** (+11: test_reservations.py ×7, +3 findings, +1 pipeline e2e); build clean.
- **Other 3 APIs — user chose "stop here, run a fresh assessment first" (2026-07-29):**
  - **Backup-usage API — NOT building.** Confirmed via docs: `backupUsageSummaries` returns protected
    ITEM/INSTANCE COUNTS only, not storage GB. Real GRS→LRS $ needs backed-up GB → Log Analytics /
    Backup reports (heavy). Backup is a rounding error in this env (~$11). GRS→LRS stays the labeled
    low-conf estimate.
  - **Price Sheet API — NOT building.** Redundant: per-resource actual cost from Cost Management already
    caps/grounds every finding, and reservation recs already use negotiated prices. Also needs a
    billing-reader role the user may lack.
  - **Compute/skus live catalog — DEFERRED (worth doing).** Would let rightsizing cover every VM
    series/region instead of the curated D/E/B/F v3-v5 static table (currently non-covered series get
    NO downsize rec). Design sketched: build a `LiveSkuCatalog` (get_spec/smaller_same_series, family
    from API `family`, order by vCPU) injected into FindingsEngine, static fallback. Revisit after the
    user reviews a fresh run.

### Post-ship 4 — aggregate Reserved Instances into ONE finding (user, 2026-07-29)
User: RIs were showing as separate per-VM/per-SKU cards; wanted them "in one under single category
reserved instances 1 year/3 year" (same treatment as AHB).
- **`findings._aggregate_commitment_finding(category, kind, items, ...)`** — new shared helper: rolls
  many per-SKU/VM items into ONE resource-less finding (escapes dedupe). Counted headline = sum of
  1-year-preferred savings; 3-year alternative total shown alongside; every item listed in
  `details.reservation_items` [{name,sku,region,quantity,monthly_savings,monthly_savings_3yr,...}].
  `details.reservation_options` = [{1-year <kind>, total},{3-year <kind>, total}], plus
  aggregate/source/kind/item_count/total_1yr_monthly/total_3yr_monthly.
- **`commitments_from_recommendations`** now buckets Azure recs BY CATEGORY → one aggregate finding per
  category (all VM RIs together; SQL/Cosmos/etc. each their own).
- **`detect_vm_commitments`** (retail fallback) refactored: collects per-VM items → ONE aggregated
  `ri_vm` finding (RI-eligible VMs) + ONE `savings_plan_vm` finding (burstable). SP no longer listed as
  a 3rd option on RI-eligible VMs (RI preferred). `ri_price_estimated` now set at the aggregate level.
- **Frontend `FindingEvidence`**: added "N <kind>s in this recommendation" list panel rendering
  `reservation_items` (sku/qty/region → $/yr, with 3-yr subline). Commitment-options panel unchanged.
- Verify: backend **191 passing** (updated 4 commitment tests + added aggregate-multiple test); build clean.

### Post-ship 5 — savings > spend credibility fix + TPT report analysis (2026-07-29)
User attached 3 real TPT client cost-assessment reports (Ridge, IdeaSolutions, ContractPod/Pax8) and a
screenshot (Assessment #25: $47K identified savings > $32K measured spend). Complaint: tool "flat out
adds AHB + RI into total savings," making savings exceed spend. Reports = the target deliverable format.
- **Report analysis (key model insights):** (1) RI 1yr vs 3yr vs Savings Plan are ALTERNATIVES, never
  summed — TPT headlines one term. (2) Every $ figure is grounded in ACTUAL cost × a discount ratio,
  NOT list price (e.g. AHB "Current Annual Licence Cost $3,285 → save $3,010"; RI "Current $6,714 → RI
  $5,229"). (3) Reserve steady VMs, right-size UNDER-utilised (mem <50%) ones — different VMs, no double
  count. (4) Structured by pillar Compute/Storage/Network with per-resource tables. (5) Exec summary =
  hero 3yr/yearly/monthly savings + 3-year projection charts (Fixed/Linear/Conservative/Civo-25% growth);
  Environment Details table (tenant, subscription, #resources, major types, last-month consumption).
- **Root cause of savings>spend:** AHB computed as full retail (Windows−Linux)×730 at LIST price, then
  ADDED on top of RI/idle/orphans → on a partially-billed/discounted $32K env it balloons past spend.
- **Fix shipped (findings.py) — ground $ in actual cost, use retail only for the RATIO:**
  - `detect_windows_ahb`: per-VM AHB = actual_cost × (Windows−Linux)/Windows (licence fraction of REAL
    spend), capped at actual; retail-delta fallback only when Cost Management has no cost for that VM
    (`actual_cost_based` flag per item). This is THE inflation fix.
  - `detect_vm_commitments` (retail fallback): cap each option (s1/s3/sp) at the VM's actual monthly cost.
  - Reservation-recommendation RI left authoritative (Azure computes it on real usage/prices already).
  - Net effect: every finding ≤ its resource's real cost ⇒ Σ savings ≤ total spend structurally, so the
    exec summary's `reconciles` path (current→optimized→%) lights up instead of the "exceeds spend" caveat.
- Verify: backend **194 passing** (+3 grounding tests).
- **STILL TODO (user asked, Phase 2):** regenerate the PDF/Excel to match the TPT template (report.py is
  currently a generic landscape findings-table dump). Needs: cover, TOC, exec summary + projection charts,
  Purpose, Methodology, Evaluation Summary (env table), Cost Optimization by pillar with per-optimization
  tables (RI 1yr/3yr, AHB, Savings Plan, right-size, orphaned disks, public IPs). Open Qs: TPT logo asset,
  client-name source (email domain? ask?), whether to replicate all 4 growth-scenario charts.

### Post-ship 6 — TPT-format PDF report (2026-07-29)
Rebuilt `report.py` `generate_pdf` from a generic landscape findings-dump into a faithful replica of the
TPT client template (verified visually via pymupdf render — 7 pages, matches samples). User chose "full
TPT replica" + "derive automatically" (client name = tenant display name, logo dropped in repo).
- **Sections:** dark cover (logo or TECHPLUSTALENT wordmark + "Cost Assessment" + date + "Prepared for:
  <client>"); TOC (real page numbers via `_TPTDoc(SimpleDocTemplate).afterFlowable` + `doc.multiBuild`);
  1. Executive Summary (hero 3yr/yearly/monthly savings box + 4 growth-projection bar charts
  Fixed/Conservative0.07/Linear0.15/Civo0.25 — cumulative ACR/after/savings via `_projection`);
  2. Purpose; 3. Methodology (Phase 1/2); 4. Evaluation Summary (Environment Details table + savings by
  pillar); 5. Cost Optimization by pillar (Compute: options bar + RI 1yr/3yr/AHB/Savings-Plan/right-size/
  idle tables; Storage: orphaned disks etc.; Network: public IPs). `_PILLAR` maps category→pillar;
  `_by_category` + `_one()` (aggregated cats are single-element lists). generate_excel kept (+ tenant/
  spend rows, Validation sheet restored).
- **Report metadata capture (backend):** model +`tenant_display_name`,`subscription_names`(JSON),
  `major_resource_types`(JSON) + **alembic 007** (batch_alter_table). `azure_client.get_tenant_display_name`
  (GET /tenants). `assessment._capture_report_metadata` (tenant name + sub names + top-3 types via
  `_gather_inventory_summary` now returning major_types + `_friendly_resource_type`). All best-effort
  (never fail the run).
- **Logo:** `backend/app/assets/tpt_logo.png` (README placeholder present); cover/header fall back to
  wordmark if absent. `_logo()` gates on os.path.exists.
- Verify: backend **195 passing** (+richer PDF test `test_pdf_full_template_renders`). Sample PDF at
  scratchpad/sample_report.pdf.
- **ACTION FOR USER:** run `alembic upgrade head` (adds the 3 new columns to the existing DB), then
  re-run an assessment so tenant name / sub names / major types populate; drop tpt_logo.png for branding.

### Post-ship 7 — savings-model correctness (Assessment #27 diagnosis, 2026-07-30)
Diagnosed the real DB (cat.db assessment 27): savings $46K > spend $32K. **Root cause: 3 HyperV-Demo
Standard_D16s_v3 VMs are "VM running" in ARG but have NO Cost Management cost (not billing in the
window).** AHB fell back to full retail ($537.28/mo × 3 × 12 = $19.3K ungrounded) + they were in the
retail-estimate RI + one (Demo3) was flagged oversized. That fabricated ~$30K of savings on $0 VMs.
- **Fix — "can't save on what you're not paying for":** when per-resource cost is available
  (`bool(self._cost_map)`), SKIP AHB / commitment / idle-oversized for VMs with no measured cost.
  (`detect_windows_ahb`, `detect_vm_commitments`, `detect_vm_utilisation_findings`.) Falls back to
  retail only when NO per-resource billing exists at all.
- **Grand-total backstop** (`assessment._persist_findings_and_totals`): clamp total_savings to
  current_annual_spend when cost data present (rarely binds after grounding; guarantees never > spend).
- **Stop fabricating RIs** (user showed Azure calc: D16s_v3 has NO reserved option, only Savings Plan
  31%/53%): removed the `ri1=payg×0.6` estimate. Now `detect_vm_commitments` uses REAL retail RI when
  offered → ri_vm; else falls back to the real Savings Plan (fetches sp1 AND sp3) → savings_plan_vm.
- **Best case = 3-year (deepest discount) is the counted headline**, 1-year shown as the alternative
  (`_aggregate_commitment_finding` reworked: one_total/three_total, options list 3yr first). Best
  overall = AHB (licence) + 3yr commitment (compute), additive & both ≤ actual cost.
- **AHB description** rewritten premium/conditional: "swap Azure's licence charge for licences you
  already own with Software Assurance… only if you hold enough (1 licence = 16 cores)."
- **Donut wobble** (`Donut.tsx` ActiveSlice): a slice >200° (dominant, e.g. Compute 99%) no longer
  expands its outerRadius (that made the ring lurch); small slices still pop +7.
- **Report chart value labels**: `$%0.0f` barLabels on projection + options charts (match template).
- Downsizing confirmed to user: HyperV-Demo1 (peak CPU 43% → smaller SKU would exceed 70% ceiling) and
  HyperV-Demo2 (0% avail-mem = 100% used) correctly NOT downsized; Demo3 (low both) was.
- Verify: backend **197 passing**; frontend build clean; sample PDF re-rendered (labels present).
- Cover logo still needs the user's tpt_logo.png. User must RE-RUN assessment to see reconciled numbers.

### Post-ship 8 — multi-currency (auto from billing currency, 2026-07-30)
User chose "auto from billing currency". Azure retail pricing already fetches in the subscription's
currency (grounding uses currency-independent RATIOS, so it was already consistent); this adds display.
- **Detect:** `cost_management.extract_currency(payload)` reads the Cost Management `Currency` column;
  `get_service_costs_and_currency` returns (totals, currency) from the same service query.
- **Fetch retail in that currency:** `get_pricing_engine(currency)` returns a per-currency engine that
  SHARES the singleton cache backend (key already namespaces `retail:<currency>:<filter>`). Orchestrator
  detects currency in step 4, builds the engine before findings.
- **Persist:** model `assessments.currency` + **alembic 008**; `_persist_findings_and_totals(..., currency)`.
  Schema `AssessmentResponse.currency`; TS `Assessment.currency`.
- **Display:** frontend `tokens.setReportCurrency(code)` (module-level, set in `AssessmentDashboard`) →
  `fmtUSD`/`fmtCompact` render via Intl in the active currency (₹/£/CA$/A$/€). Report `report._set_currency`
  + `_usd` use **Helvetica-safe** symbols (INR→"Rs ", CAD→"CA$", AUD→"A$"; ₹ has no glyph in base PDF font);
  chart barLabels use the symbol too. Verified GBP PDF renders £ in charts+tables.
- Verify: backend **200 passing** (+currency tests); frontend build clean. **User: run `alembic upgrade
  head` (adds `currency` col) before next run.**

### Post-ship 9 — commitment grounding bug (Assessment #28, 2026-07-30)
DB diagnosis: savings=spend (₹32,343=₹32,343, "100%/₹0") AND 1yr==3yr everywhere. **Root cause: the
commitment grounding was `s = min(retail_saving, actual_cost)` — which caps at the WHOLE VM cost, so
the saving became 100% of cost and both terms collapsed to the same number** (CRA-VM: ondemand
₹13,230/mo, saving ₹239.61/mo for BOTH 1yr & 3yr = its full actual cost). Summed → raw ₹35K > spend →
grand-clamp → =spend.
- **Fix (`detect_vm_commitments`):** a commitment saves the DISCOUNT %, so `saving(term) = base ×
  discount_ratio(term)` where `discount_ratio=(payg−price)/payg` (currency-independent). `base` = the
  COMPUTE portion of actual cost: for a Windows VM not on AHB, strip the licence via
  `compute_fraction = payg_linux/payg_windows` (so RI/SP don't overlap AHB); Linux / already-AHB → 1.0.
  Takes `windows_no_ahb_ids` (passed from `_detect_all` = the windows_vms_without_ahb bucket). No
  billing data → base=payg (retail). item `ondemand`=actual (not retail). This fixes 1yr≠3yr AND
  savings<spend (est #28: SP 15.4K→~4.2K, RI 9.2K→~2.5K, AHB unchanged 10K → total ~17.3K/54% of spend).
- Verify: backend **201 passing**; updated test → `test_commitment_grounds_as_fraction_of_actual_cost`
  + `test_commitment_strips_windows_licence_from_base`.
- **AHB magnitude — user answered with METHODOLOGY (not actual-vs-list):** "cost should always be taken
  of last month" (e.g. on Aug 15 use ALL of July, NOT Aug 1-15) + "validate by confirming a VM costs the
  same each month (May=June=…) or has discrepancies." **The tiny ₹239/mo was because we used a trailing
  30-day window that includes the current, not-yet-fully-billed month (Azure posts with delay) → recent
  usage undercounted.**

### Post-ship 10 — last-complete-month cost basis + consistency validation (2026-07-30)
- **Cost basis = last COMPLETE calendar month** (`cost_management.build_cost_query` / `build_service_cost_query`
  → `timeframe:"TheLastMonth"`, dropped the trailing-window timePeriod; `parse_*` factor=1, no ×30/days).
  This is THE fix for the undercounted magnitude — pulls real full-month costs. User must re-run to see it.
- **Month-over-month consistency (validation):** `build_monthly_history_query(months=4)` (Monthly
  granularity, grouped ResourceId), `parse_monthly_history` → {rid:[costs]}, `cost_consistency` →
  {rid:{mean, billed_months, cv, stable}} (stable = ≥2 billed months & CV≤0.25). `get_cost_consistency`
  per-sub; `assessment._gather_cost_consistency` merges; passed to `FindingsEngine(cost_consistency=...)`.
  `detect_vm_commitments` now derives `steady` from real billing history when available (else metric
  proxy) → drives confidence; items carry `billed_months`/`cost_stable`.
- Verify: backend **204 passing** (updated cost tests + consistency tests). **User: re-run assessment.**

### Post-ship 11 — TheLastMonth broke cost data on a new sub (Assessment #30, 2026-07-30)
DB: #28 cost_avail=1 (INR ₹32K), but #29/#30 (after `TheLastMonth` change) cost_avail=0, USD, no spend,
$49K retail-fallback savings — SAME subscription (005e9433, "...Sponsorship - 2026", a NEW sub that only
started billing in July, so **June — the last complete month — is empty** → cost query returned nothing →
cost_data_available=0 → USD default + retail-list fallback ($49K, ungrounded AHB $6.4K etc.).
- **Fix (`cost_management`):** `build_cost_query(now, month_to_date=False)` now uses an explicit `Custom`
  last-complete-month date range (via `_month_bounds`), and `get_actual_cost_by_resource` /
  `get_service_costs_and_currency` **fall back to month-to-date when the last complete month is empty**
  (new subscription). Restores cost data → grounding → INR currency → savings < spend. `get_actual_cost_by_service`
  now delegates to the currency variant. (Root: named `TheLastMonth` returned empty for the new sub.)
- **Progress labels de-jargoned** (`Results.tsx` PHASES): "Retrieving Advisor recommendations" →
  "Reviewing optimisation signals"; "Fetching utilisation metrics" → "Analysing resource utilisation";
  "Detecting cost findings" → "Identifying savings opportunities". (User: customers shouldn't see "Advisor".)
- **Per-VM 1yr + 3yr both labelled** (`FindingEvidence` reservation_items): was 1yr unlabelled + "3-yr:"
  subline → now "3-yr: $X/yr" (best, prominent) + "1-yr: $Y/yr" (both explicit). Aggregate already showed both.
- **Explained (not bugs):** AHB $6.4K for D16s_v3 = exact retail Windows−Linux delta ($537.28/mo×12) =
  the calculator's licence line → correct at LIST; only inflated because cost data was lost (retail
  fallback). Once grounded it's the licence portion of actual cost. "Only 1 RI" = only D2as_v4 has a
  retail Reserved Instance; D16s_v3/D4s_v3/D2s_v3 offer only a Savings Plan (calculator confirms) → they
  route to SP correctly. Reservation-recommendations API returned nothing this run (retail_estimate).
- Verify: backend **206 passing**; frontend build clean. **User: RE-RUN — cost data now recovers via MTD.**

### Post-ship 12 — AHB "Linux" rename + licence-ownership framing (2026-07-30)
User confused why "Linux" appears in an AHB (Windows-only) calc. Clarified: the same-size **Linux price
is only a measuring stick for the compute-only (post-AHB) cost** — Windows − compute-only = the licence
charge (Azure's own calculator confirms: the "Azure Hybrid Benefit" price = the Linux price).
- **Renamed** in `findings.detect_windows_ahb`: local var `linux`→`compute_only`; comments + `reason` +
  finding description now say "compute-only (post-AHB)" not "Linux". Also updated the commitment-detector
  `compute_fraction` comment. Output shape unchanged; 206 tests still pass.
- **Licence-ownership point (user, correct):** AHB is "use a licence you OWN instead of renting Azure's";
  the saving is realised only if you already hold eligible Windows Server licences w/ SA. If you buy one,
  net = charge − amortised licence (but one-time licence vs monthly charge → ~2-month payback on an
  always-on D16; longer for part-time/small VMs). **Tool can't see licence entitlements (not in Azure).**
- **Decision (user):** keep **gross saving + clear caveat** (already shipped) — don't model licence
  purchase / break-even, don't make AHB opt-in. Description now states it's realised only if you own/buy
  eligible licences + the payback note. No further AHB code changes.

### Post-ship 13 — INTERMITTENT cost-data loss = Cost Management throttling (Assessment #32, 2026-07-30)
DB smoking gun: same sub/code, #28✓INR #29✗ #30✗ **#31✓INR (sav ₹18,826 < spend ₹32,343 — the CORRECT
result my fixes produce!)** #32✗. Not a query-logic bug — **intermittent throttling.** Cost Management +
Consumption throttle hard; failures were swallowed by `asyncio.gather(return_exceptions=True)` → empty
cost → cost_data_available=0 → USD default + retail-list fallback ($52K) + wrong "grant Reader" message.
The **shared circuit breaker** (`azure-arm`, 5-fail threshold) + my 3rd CM call (consistency) made it worse:
a busy metrics run trips the breaker → cost queries fail-fast.
- **Fix (`azure_client`):** `_send` now takes `max_retries` + `use_breaker`. `query_cost_management` and
  `get_reservation_recommendations` use **max_retries=8, use_breaker=False** → patient (honours
  Retry-After) + isolated from the shared breaker (billing throttles don't open it; a busy breaker
  doesn't kill billing). reservation-recs also break on 429 (never fatal).
- **Halved CM calls (`cost_management.get_cost_map_and_consistency`):** ONE monthly-history query now
  yields BOTH the last-month cost_map (`costs[-1]` per resource; `parse_monthly_history` sorts by month)
  AND consistency — replacing the separate resource-cost + consistency queries (3 CM calls/sub → 2).
  MTD fallback retained for new subs. `assessment._gather_cost_and_consistency` merges; removed
  `_gather_cost_map` / `_gather_cost_consistency` / the `get_actual_cost_by_resource` import.
- **Message fixed (`ExecutiveSummary`):** "billing data wasn't returned this run — usually a temporary
  throttle, re-run resolves it; if it persists confirm Cost Management Reader" (not "grant Reader").
- Verify: backend **207 passing**; frontend build clean. **User: re-run — now reliable under throttling.**

### Post-ship 14 — reliability over failure-handling (user clarified, 2026-07-30)
User: don't BLOCK the report / add DRAFT guards for the no-cost-data state — **make retrieval reliable
so it never gets there.** Reverted the half-added `_require_grounded` report gate (kept the original
status check). Doubled down on reliability instead:
- `resilience.RETRYABLE_STATUS` = {429, **500, 502, 503, 504**} — transient 5xx that Cost Management
  throws under load now retry with backoff instead of failing.
- (Already in post-ship 13: cost/consumption queries patient max_retries=8 honouring Retry-After,
  `use_breaker=False` isolated from shared breaker; combined cost+consistency into ONE monthly query.)
- Removed the savings==spend clamp (grounding already guarantees ≤ spend; clamp only produced a
  nonsensical 100%). Kept honest throttle-vs-no-access message.
- **Honest caveat to user:** no external API is literally 100%, but transient throttling/5xx now
  self-heal within the run via patient retries → for an account with billing access it should retrieve
  cost data every run (the intermittent failures were swallowed throttles, now waited out).
- Verify: backend **207 passing**; frontend build clean (message change from prior turn).

### Post-ship 15 — bulletproofing pass: correctness + reliability + docs (2026-07-30)
User: "optimize everything, perfect logic, never fail / never inaccurate, fix what's broken AND what
could break, update memory + README." In-depth review + fixes:
- **AHB × idle double-count (data bug):** an idle Windows VM was counted BOTH in idle-VMs (delete=full
  cost) AND AHB (licence, resource-less → escaped dedupe). Fix: `_detect_all` collects idle+deallocated
  ids → `detect_windows_ahb(vms, exclude_ids=...)` drops them (can't save a licence on a deleted VM).
- **Non-USD miscalibration (data bug):** `severity_from_savings` used USD bands ($300/$100/$20) applied
  to billing-currency amounts → INR runs marked ~everything "critical"; hardcoded orphan/ASP/IP/disk
  estimates were USD shown as ₹ (80× too low). Fix: new `services/currency.py` (`to_usd`/`from_usd`,
  static FX table USD/EUR/GBP/CAD/AUD/INR/…). Severity bands on USD-equivalent; orphan (`detect_orphans`),
  ASP (`detect_idle_app_service_plans`), and pricing static fallbacks (IP/disk) convert USD→billing.
  `FindingsEngine(currency=...)` threaded from `assessment` (detected billing currency).
- **Metrics fan-out (scale reliability):** `enrich_vms_with_metrics` fired all VM metric calls at once
  (hundreds × 3 → self-throttle). Bounded with `asyncio.Semaphore(15)`.
- **Mixed-currency multi-sub guard:** `_gather_service_costs` logs an ERROR if subs report >1 currency
  (summing would be nonsense); reports in the first. (Real orgs = one billing currency.)
- **Reverted** the report-block gate from the interrupted attempt (user wanted reliability, not failure-
  handling). Removed the savings==spend clamp already (post-ship 14).
- **README rewritten** to reflect what the tool is now (grounding methodology, accuracy principles, AHB
  explanation, all resource types, branded report, currency, reliability, architecture, `alembic upgrade
  head` warning). memory.md = this running log.
- Verify: backend **210 passing** (+AHB-exclude, +currency severity/orphan tests); frontend build clean.

### Post-ship 16 — validation + reservation-recs diagnosis (the two real gaps, 2026-07-30)
After a brutal-honesty review, user asked to fix the top 2: (a) reservation-recs never fire (always
`retail_estimate`), (b) no ground-truth validation. Can't hit live Azure here, so:
- **Reservation-recs observability + robustness (`azure_client`):** split into `_fetch_reservation_recs`
  returning (items, reason); `get_reservation_recommendations` logs status/count/reason and, if `Single`
  scope is empty, retries WITHOUT a scope filter (some tenants only surface Shared recs).
  `_gather_reservation_recs` logs raw-items→grouped counts (distinguishes "API empty" from "parser
  dropped"). **Likely root cause: this env's VMs run ~1% of list → Azure legitimately returns no recs
  (MS docs: "if resources are shut down regularly, no recommendation"). Not a bug; on a steady real
  client it should fire.** Next run's logs confirm definitively.
- **Audit trail (the validation basis + CFO-auditable derivation):** AHB `eligible_vms` now carry
  `windows_price / compute_only_price / licence_charge / licence_fraction / actual_monthly_cost`;
  commitment `reservation_items` carry `compute_base / discount_1yr / discount_3yr / grounded`. Always
  present (not debug-gated).
- **`scripts/reconcile_assessment.py`** (run `python -m scripts.reconcile_assessment [id]` from backend/):
  prints every finding's full derivation + integrity CHECKS (Σfindings==total, savings≤spend, no
  resource double-counted, reservations-from-Azure-engine, cost-data-present). Ran on #31 → all OK
  except it correctly FLAGGED source=retail_estimate. ASCII-only output (Windows cp1252 console).
- **GOLDEN test** `test_ahb_matches_azure_calculator_to_the_dollar`: D16s_v3 Windows $1,097.92 −
  compute-only $560.64 = $537.28 licence = $6,447.36/yr; grounded = actual × fraction. Proves AHB
  matches Azure's calculator exactly.
- Verify: backend **211 passing**; frontend build clean.
- **STILL OPEN (honest):** the reconcile tool is internal-consistency + hand-audit, NOT an independent
  oracle — true end-to-end validation still needs ONE real subscription checked against portal numbers
  by a human. Deep-service coverage (SQL/AKS/storage-tiering/Sentinel), live FX, live SKU catalog,
  job-queue/observability remain (see post-ship 15 review).

### Post-ship 17 — validation badge mislabelled grounded findings (2026-07-30)
User questioned the per-finding "Not cost-validated / list-price estimate — upper bound" badge. Real bug:
`validate_savings` keys off a single `resource_id`, but AHB/RI/SP are **aggregates with resource_id=None**
→ always UNVALIDATED → the badge called the tool's OWN grounded numbers "list-price upper bounds"
(exactly backwards). Fix:
- `_finding(..., grounded=False)`: when a saving is derived from actual cost, set validation=VALIDATED
  ("Grounded in the resource's actual billed cost") even without a per-resource match. Imported
  UNVALIDATED/VALIDATED constants.
- Pass `grounded=`: AHB → `cost_available and grounded_count==n`; `_aggregate_commitment_finding` gains
  `grounded` param → retail path `grounded=cost_available`, `commitments_from_recommendations`
  `grounded=True` (Azure = authoritative).
- Frontend `badges.tsx`: validated tooltip now "Grounded in your actual billed cost… not a list-price
  guess"; unvalidated relabelled "Estimate" with honest wording ("nominal rate for an orphaned
  resource… approximate"), not "list-price upper bound" (which was wrong for nominal estimates).
- Net: grounded AHB/RI/SP now read "Cost-validated" (green); only genuine nominal estimates (orphan
  NAT/LB/IP/Bastion/vault) and the true list-price fallback show "Estimate".
- Verify: backend **212 passing** (+grounded-reads-as-validated test); frontend build clean.

### Post-ship 18 — Assessment #37 UI batch: dashboard de-clutter + interactivity + copy polish (2026-07-30)
User feedback on live dashboard screenshots: redundant panes, no interactive graphs, un-clickable
1-year term, alarming "Needs review (+4859%)" badge, and over-wordy "why" copy. All addressed:
- **Clickable commitment terms** (`FindingEvidence.tsx`): single `CommitmentPanel` with ONE term
  control. Each aggregated commitment finding carries BOTH `reservation_options` (term totals) and
  `reservation_items` (per-SKU), so the first cut rendered two panels each with its own 1yr/3yr control
  → user reported "two toggles, only one works" redundancy. Merged into one panel: clicking a term
  (keyboard-accessible; 3-year keeps the "Best value" badge) highlights that option AND re-prices every
  SKU below it (sub-heading reads "N Reserved Instances · 3-year term"). Needed
  `import { useState } from "react"`. Removed the earlier `CommitmentOptionList` / `ReservationItemList`.
- **Exec summary redesign** (`ExecutiveSummary.tsx`, `reconciles` branch): removed the 4 redundant
  StatTiles (Current Annual Spend / Est. Annual Savings / Spend After Optimization / Savings %) that
  duplicated the top flow — the three headline numbers now live ONLY in the top flow row. Added
  **hover-for-full-amount**: `FlowNumber` gained a `full` prop → MUI `Tooltip` shows exact `fmtUSD`
  (e.g. hover ₹33K → ₹33,190); savings chip + % pill also get tooltips. Dropped unused
  `TrendingDown`/`Percent` icon imports.
- **New interactive chart** `charts/SavingsProjection.tsx` (Recharts AreaChart): cumulative-savings
  forecast over 36 months, crosshair + custom tooltip (exact saved-to-date + run-rate), Year 1/2/3
  markers. Honest straight run-rate (month N = N × monthly saving), no compounding. Replaces the
  removed tiles. Deliberately does NOT duplicate `InsightsRow` (savings-by-area donut + impact bars).
- **Calmer needs-review badge** (`badges.tsx`): the raw `+X%` variance is gone from the chip label
  (read as alarming, e.g. "+4859%"); chip is just "Needs review", variance moved into the tooltip in
  plain language ("about N× this resource's billed cost"). NB the root cause is already dead —
  `_finding` caps any estimate>actual to actual → VALIDATED — so needs_review is effectively unreachable;
  this is defensive UI only.
- **Copy polish** (`findings.py`): AHB description trimmed to 2 sentences; aggregated commitment
  description trimmed to 2 sentences (numbers via `rate_note`, no /yr redundancy). Fixed a real
  currency bug — Bastion orphan description hardcoded "~$138/mo" (wrong in INR/GBP tenants) → replaced
  with currency-neutral "bills whether or not it's used". Other per-resource descriptions were already
  concise single sentences (left as-is).
- Verify: backend **212 passing**; frontend `npm run build` clean.

### Post-ship 19 — removed the Excel export entirely (2026-07-31)
User: "kill the excel switch entirely. its not needed at all." Removed the whole XLSX path end-to-end,
PDF is now the only report format:
- Frontend: dropped the "Download Excel" button + `TableChartIcon` in `Results.tsx`; `downloading`
  state simplified from `"pdf" | "excel" | null` to a boolean; `handleDownload` takes no format;
  `api.ts` `downloadReport(id)` is PDF-only (no format arg); Login.tsx marketing line "PDF and Excel"
  → "PDF reports".
- Backend: deleted the `GET /{id}/report/excel` route (`assessments.py`) and `generate_excel` +
  its helpers (`_confidence_label`/`_as_of`/`_validation_counts`) and the openpyxl imports from
  `report.py`; module docstring updated to "PDF" only. Dropped `openpyxl==3.1.2` from
  `requirements.txt` (nothing else used it).
- Tests: removed `test_generate_excel_*` and the `io`/`openpyxl` imports from `test_report.py`;
  replaced `test_download_excel` with `test_excel_route_removed` (asserts the route now 404s).
- Docs: README, SETUP, CODEBASE_BRIEF all de-referenced Excel.
- Verify: backend **211 passing** (was 212; net −1 excel test); frontend build clean.

### Post-ship 20 — filter out ₹0-savings findings (2026-07-31)
User (screenshot): an "Azure Advisor Cost Recommendation" showing ₹0/yr ("Disable Front Door health
probes…") — "whys this here". Root cause: Azure Advisor returns some Category=Cost recs with no
`savingsAmount`, so `_extract_savings` yields 0 and `advisor_findings` surfaced them anyway; deallocated
VMs also emit 0.0 (residual disk cost unquantified). No filter dropped them. Fix: in `_detect_all`,
`findings = [f for f in findings if (f.get("estimated_savings_monthly") or 0) > 0]` right before
`_dedupe` — single choke point, so a zero-value line never reaches persist/report/findings_count.
Also removes deallocated-VM ₹0 lines (same noise). Added `test_zero_savings_findings_are_filtered_from_pipeline`.
Verify: backend **212 passing**. NOTE: deallocated VMs now suppressed entirely — if we later want them
back, quantify their disk cost into a real saving rather than emitting 0.0.

### Post-ship 21 — report growth projections now derived from the environment's real spend trend (2026-07-31)
User: the report's 3-year trajectory charts (Linear / Conservative growth) must NOT use hardcoded 7/15%
— in their manual method those come from the delta (actual spend trend); only Civo's 25% is a fixed
hypothetical. Chosen method (user: "go with the most accurate"): **best-fit straight line** through
recent monthly spend; Conservative = **half** of Linear.
- `cost_management.py`: `parse_monthly_totals(payload)` → {month: total across resources} (month-labelled
  so multi-sub merges align); `linear_growth_rate(monthly_totals)` → least-squares slope annualised vs
  the line's latest month → yearly growth fraction. Returns None if <3 complete months (hide charts),
  0.0 if flat/declining, clamped to [0,1.0]. `get_cost_map_and_consistency` now returns a 3-tuple
  (adds monthly_totals); the monthly-history query already excludes the current partial month.
- `assessment.py`: `_gather_cost_and_consistency` fetches **6 months**, sums per-sub monthly totals by
  month, computes one growth rate, returns (cost_map, consistency, growth); persisted via
  `_persist_findings_and_totals(observed_growth=...)`.
- `db.py` + alembic **009**: new nullable `observed_annual_growth` column (applied to cat.db).
- `report.py`: `_projection` is now **LINEAR (simple)** not compound (`spend*(1+growth*year)`) — matches
  the "Linear Growth" label (was a latent bug). `_scenario_growth` resolves per-scenario growth:
  `growth_source: measured` → the trend, `half_measured` → half, else fixed `growth`; None ⇒ skip that
  chart. `ctx["_growth"]` carries the measured rate.
- `report_template.yml`: Linear scenario → `growth_source: measured`, Conservative → `half_measured`;
  Fixed stays 0, Civo stays fixed 0.25. Captions updated.
- Per-environment by construction: a $1k-ACR env and a $12k-ACR env get entirely different charts.
- NOTE: the dashboard `SavingsProjection.tsx` is still a simple monthly run-rate (no growth) — NOT yet
  aligned to this measured-growth method. Align later if web/PDF consistency matters.
- Verify: backend **216 passing**; `alembic upgrade head` clean.

### Post-ship 22 — SQL Server Azure Hybrid Benefit detector (2026-07-31)
Closing the biggest quick-win gap (user picked "just SQL AHB for now" over storage). Grounded in MS
docs: AHB applies only to vCore *provisioned* SQL DB/MI (not DTU/serverless), toggling licenseType
LicenseIncluded→BasePrice; it removes only the SQL Server *licence* component, a fixed ~$0.1534/vCore-hr
(≈$112/vCore-month) that is the SAME dollar amount across GP/BC/Hyperscale. So the saving is
**vCores × per-vCore licence, capped at the resource's actual cost** — isolating the licence means it
never inflates on storage/backup.
- `kql.py`: `sql_ahb_eligible_query()` (DB+MI union, licenseType=LicenseIncluded, excludes serverless
  `_S_` SKUs, excludes paused/stopped, vCores = coalesce(properties.vCores, sku.capacity)); registered
  as bucket `sql_ahb_eligible`.
- `findings.py`: const `SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD = 112.0`; `detect_sql_ahb()` mirrors
  `detect_windows_ahb` — resource-less aggregate (escapes dedupe, additive with reservations), skips
  non-billing resources when cost data present, `from_usd()` for currency, reuses `eligible_vms` details
  key + `licence_kind="SQL Server"`. Category `sql_ahb`, display "SQL Server Azure Hybrid Benefit".
- `assessment.py` `_detect_all`: excludes paused-DB/stopped-MI ids, calls `detect_sql_ahb`.
- Report `_PILLAR`: sql_ahb→Compute (not _DEDICATED → renders as a row in the Compute table via
  `_pillar_rows` display_name fallback). Frontend `area.ts`: sql_ahb→**Databases**. `FindingEvidence.tsx`:
  AHB panel now category-aware (noun "SQL resource"/"VM", caveat "SQL Server"/"Windows Server licences").
- Tests: KQL query test, `detect_sql_ahb` grounding+cap test, skip/exclude test, registry bucket set
  updated. Backend **219 passing**; frontend build clean.
- Coverage verdict given: tool now covers ~85%+ of the standard Azure quick-win checklist. Remaining
  gaps flagged, NOT built (user deferred): storage-account redundancy GRS→LRS, disk Premium→Standard
  (needs per-disk IOPS metrics). True blob hot/cool/archive tiering is INFEASIBLE accurately (needs
  per-blob access data ARG doesn't expose) — explicitly out of scope.

### Post-ship 23 — AHB robustness, deallocated-VM quantification, confidence chip removed (2026-07-31)
Pre-demo hardening. User flagged AHB findings looked wrong + confidence % shouldn't be client-facing.
- **Confidence chip removed** from `RecommendationCard.tsx` (client dashboard). Backend `confidence` still
  drives sorting; just not shown. `ConfidenceChip` still exists in badges.tsx (unused there) + FindingsTable
  (internal/debug view).
- **SP vs RI verified**: `detect_vm_commitments` sorts each VM into ri_items XOR sp_items (if/elif on
  RI availability) — never both, so totals aren't double-counted. AHB (licence) + commitment (compute,
  licence stripped via compute_fraction) are additive, non-overlapping. Confirmed, no change needed.
- **Windows AHB bug found + fixed**: DB inspection of assessment 44 showed D-series all give a
  consistent ₹3,169.7/vCore licence (correct) but B2ms gave ₹275/vCore (~11x low) — `get_vm_windows_
  monthly_price` under-fetches B-series. Fix: the Windows licence is a flat per-vCore charge, so
  `detect_windows_ahb` now (1) samples (windows−linux)/vCPU per VM, (2) takes the **MEDIAN** as the
  per-vCore rate, (3) applies `licence = vCPU × median`, `windows_eff = linux + licence`,
  `fraction = licence/windows_eff`, saving = actual × fraction. Robust to a bad per-SKU fetch; single-VM
  and consistent-fleet cases are unchanged (median = the sample) so the golden test still passes. New
  helpers `_median`, `_vcpus` (get_spec else first-digit parse of SKU); added `vcpu` to eligible_vms.
- **Deallocated VMs re-quantified** (they were suppressed as ₹0 by the zero-filter): KQL now projects
  `osDiskId` + `dataDisks`; `detect_deallocated_vms` sums the attached disks' actual cost from cost_map
  (the disks keep billing; they're attached so the unattached-disk detector misses them). Saving =
  disk cost, grounded. Added `actual_cost_override` param to `_finding` so the per-VM cap uses the
  DISKS' cost basis, not the VM's ~0 compute (which would wrongly clamp it to 0). 0 without cost data →
  dropped by the zero-filter (acceptable).
- Verify: backend **222 passing** (+3 net: median-correction test, 2 deallocated tests; rewrote the
  2-VM AHB aggregate test to use realistic consistent per-vCore prices); frontend build clean.
- User re-runs the assessment tomorrow to see corrected AHB + deallocated VMs populate.

### Post-ship 24 — de-hardcoded prices: live retail for LB/NAT/Bastion/ASP + dated estimates table (2026-07-31)
Also aligned dashboard chart to PDF (Post-ship 24a below).
- **New `estimates.py`**: single dated table (`ESTIMATES_VERIFIED = "2026-07-31"`). Holds the genuinely-
  can't-fetch-live values (snapshot $/GB, GRS vault, SQL-AHB per-vCore — moved here from findings.py)
  AND the USD fallbacks for the now-live-priced items (LB/NAT/Bastion/ASP table). Comment explains they
  only feed "Estimate"-badged findings, never grounded headlines.
- **`pricing.py`**: new `_flat_hourly_monthly()` (serviceName+region+Consumption+'Hour' filter, min×730)
  and `_live_or_fallback()` — a **sanity band [0.25×,4×] around the dated fallback** so a wrong meter
  match (I can't test live meters from here) can NEVER produce a bad number; it degrades to the verified
  estimate and logs. Methods: `get_load_balancer_monthly_price`, `get_nat_gateway_monthly_price`,
  `get_bastion_monthly_price`, `get_app_service_plan_monthly_price` (normalises RG 'P1v2'→retail 'P1 v2').
- **`findings.py`**: `detect_orphans` + `detect_idle_app_service_plans` are now **async**; LB/NAT/Bastion
  call the live pricing methods, snapshot/vault stay static (via estimates). Removed `_SNAPSHOT_PER_GB`,
  `_ASP_COST`, the SQL constant (now imported from estimates). `_LIVE_PRICED_ORPHANS` set documents which.
- **`assessment.py`**: `await` the two now-async detectors.
- Tests: `FakePricing` gained `currency` + the 4 flat-rate methods (return `from_usd(fallback, currency)`);
  all `detect_orphans`/ASP callers now `await`; INR conversion test uses `FakePricing(currency="INR")`
  (conversion moved from the detector into the pricing engine). New `test_pricing.py` cases: live price
  accepted, empty→fallback, out-of-band→fallback. Backend **225 passing**.
- STILL hardcoded on purpose (documented as fine): tuning thresholds (IDLE/DOWNSIZE/METRIC_WINDOW),
  tolerance/cv, HOURS_PER_MONTH, API versions, Civo 25%, FX table (low-impact — severity + estimates only).

### Post-ship 24a — dashboard projection chart aligned to the PDF (2026-07-31)
Exposed `observed_annual_growth` in `AssessmentSummary` schema + frontend `Assessment` type. Reworked
`SavingsProjection.tsx` to project spend LINEARLY at the measured growth (same method as report's Linear
scenario) instead of a flat run-rate; flat when no growth measured (= report's Fixed scenario). Passes
`annualGrowth={assessment.observed_annual_growth}` from ExecutiveSummary. Confidence chip already removed
(Post-ship 23). Frontend build clean.

### Post-ship 25 — App Service Plan rightsizing detector (first of the rightsizing suite, 2026-07-31)
First of the "shrink it" detectors from the TPT template. Built conservatively (rightsizing can break
prod), mirroring the VM oversized pattern exactly.
- `kql.py`: `active_app_service_plans_query()` (numberOfSites>0, tier not Free/Shared/Dynamic);
  registered bucket `active_app_service_plans`.
- `metrics.py`: `enrich_asps_with_metrics` + `get_asp_utilisation` — peak (Maximum) `CpuPercentage` +
  `MemoryPercentage` over 30d, bounded concurrency (reuses `_METRIC_CONCURRENCY`).
- `findings.py`: `_ASP_SPECS` (sku→series,cores,mem_gb), `find_asp_downsize_target` (smallest same-series
  SKU where BOTH projected CPU AND mem clear the 70% `DOWNSIZE_HEADROOM_CEILING`; halving cores ~doubles
  util), `detect_app_service_rightsizing` (async). Saving = live price(current) − live price(target)
  via the ASP pricing method; skips if either price missing (no guessing); requires both CPU+mem (mem
  missing → CPU-only at reduced confidence, like VMs). Reuses the VM resize evidence panel by emitting
  `current_sku`/`recommended_sku`/`current_vcpu`/`recommended_vcpu`/`*_memory_gb`. Category
  `app_service_plan_rightsizing`.
- `assessment.py`: enrich active ASPs with metrics (step 2), `_detect_all` gains `active_asps` param +
  calls the detector. `report.py` `_PILLAR`: →Compute. Frontend `area.ts` already mapped it →Compute.
- Tests: `find_asp_downsize_target` headroom cases + `detect_app_service_rightsizing` (grounded price
  delta, skips busy/unmetered); registry bucket set updated. Backend **228 passing**.
- REMAINING rightsizing (same pattern, NOT yet built): SQL DB, SQL MI (metric cpu_percent/vcore; SQL
  vCore pricing + ladder), and disk Premium→Standard (needs per-disk IOPS metrics — different metric).

### Post-ship 26 — SQL Database rightsizing detector (2026-07-31)
Second of the rightsizing suite. Extra-cautious because SQL is stateful/performance-sensitive.
- `kql.py`: `rightsizable_sql_databases_query()` (vCore GP/BC/Hyperscale, not DTU/serverless, online,
  vCores>2); bucket `rightsizable_sql_databases`.
- `metrics.py`: `enrich_sql_dbs_with_metrics` + `get_sql_db_utilisation` — peak `cpu_percent`,
  `physical_data_read_percent`, `log_write_percent` (a SQL DB can be CPU-light but IO-bound, so all
  three, not CPU alone).
- `findings.py`: `_SQL_VCORE_LADDER` (GP/BC Gen5: 2..80), `find_sql_vcore_target` (smallest vCore < current
  where EVERY measured peak × (current/target) ratio clears the 70% ceiling), `detect_sql_db_rightsizing`
  (async). **Grounded-only** (returns [] if no cost_map — never guesses a downsize on a stateful DB);
  saving = `actual cost × (removed vCores / current vCores)`, capped at actual (self-calibrating on the
  real bill, no fragile SQL retail pricing needed). Confidence ×0.85 (extra caution). Reuses the resize
  panel via current_sku/recommended_sku/current_vcpu. Category `sql_db_rightsizing`.
- `assessment.py`: enrich SQL DBs (step 2), `_detect_all` gains `active_sql_dbs`. `report.py` _PILLAR
  →Compute. Frontend `area.ts` already had `sql_db_rightsizing: "Databases"`.
- Tests: ladder headroom cases, grounded saving (800×6/8=600, validated), requires-cost-data. Backend
  **231 passing**. NOTE: the run-from-CAT-root gotcha caused a spurious 111-fail run; must `cd backend`.
- REMAINING rightsizing: SQL Managed Instance (same pattern, MI `avg_cpu_percent` + vCores from
  properties.vCores) and disk Premium→Standard (needs per-disk IOPS metrics — different metric).

### Post-ship 27 — SQL MI + Disk Premium→Standard rightsizing (rightsizing suite COMPLETE, 2026-07-31)
Finished the 4-detector rightsizing suite (ASP, SQL DB done in 25/26; MI + disk here).
- **SQL MI** (`sql_mi_rightsizing`): `rightsizable_sql_managed_instances_query` (vCore, running, vCores>4);
  `enrich_sql_mis_with_metrics` (peak `avg_cpu_percent` only — MI has no per-DB IO-percent metrics);
  `find_sql_vcore_target` generalised to take a `ladder` param, MI uses `_SQL_MI_VCORE_LADDER` [4,8,16,24,
  32,40,64,80]; `detect_sql_mi_rightsizing` — grounded-only, saving = actual×(removed/current) capped.
- **Disk** (`disk_rightsizing`): metric names VERIFIED via MS Learn — `Composite Disk Read/Write
  Operations/sec` + `Composite Disk Read/Write Bytes/sec` at the `microsoft.compute/disks` resource
  level. `rightsizable_premium_disks_query` (attached Premium_LRS/ZRS, sizeGB>0); `enrich_disks_with_iops`
  (peak IOPS=read+write ops, peak MB/s=read+write bytes/1e6, upper-bound = safe direction);
  `detect_disk_rightsizing` — Premium→StandardSSD when peak IOPS≤350 AND peak MB/s≤42 (70% of Standard
  SSD's ~500 IOPS/60 MB/s baseline; `_STANDARD_SSD_BASELINE_*` consts); saving = price(Premium,size) −
  price(StandardSSD,size), capped at actual. Requires BOTH I/O signals (missing → skip; a wrong downgrade
  throttles I/O). Report already had disk_rightsizing scaffolding (_DEDICATED + `_disk_sku_items`, Storage
  pillar); area.ts already mapped it. Just added CATEGORY_DISPLAY.
- Pipeline: `_detect_all` now takes `active_sql_mis` + `premium_disks`; both enriched in step 2.
- Tests: MI ladder/CPU grounded test; disk premium→standard delta + skip-busy/unmetered. Backend **234
  passing**; frontend build clean.
- ALL FOUR rightsizing detectors now live: app_service_plan_rightsizing, sql_db_rightsizing,
  sql_mi_rightsizing, disk_rightsizing. CAVEAT for the real run: disk metrics are resource-level and
  aggregation support can vary — if disk findings never appear, the metric may only emit on the VM; if
  they do appear, spot-check IOPS against the portal. All grounded/capped, conservative 70% ceilings.

### Post-ship 28 — disk rightsizing latency safety + dismiss button wired (2026-07-31)
User caught a real flaw in the disk detector + asked to finish the dismiss button.
- **Disk latency safety**: low IOPS ≠ safe to downgrade — SQL/DBs need Premium's low, consistent LATENCY
  even at trivial IOPS (a SQL log disk commits txns on it), which the IOPS/throughput check missed. Fix:
  (1) `sql_virtual_machines_query()` → SQL-VM VM ids (bucket `sql_virtual_machines`); premium-disk query
  now projects `managedBy` (owning VM); `detect_disk_rightsizing(disks, exclude_vm_ids)` skips disks on
  SQL VMs; `_detect_all` builds sql_vm_ids and passes them. (2) Recommendation now carries an explicit
  "NOT for latency-sensitive workloads (databases/logs need Premium even at low IOPS) — verify the
  workload first" caveat for the undetectable cases. Test: SQL-VM disk excluded.
- **Dismiss button** (backend API + model already existed): exposed `dismissed` on `FindingResponse`
  schema + frontend `Finding` type. Dismiss endpoint now **re-rolls assessment totals** from surviving
  findings (total_savings_monthly/annual + findings_count) so the headline stays consistent (captured
  `assessment` from `_owned_assessment`). Frontend: `RecommendationCard` gains `assessmentId` + a
  "Dismiss" button (useMutation → api.dismissFinding → invalidate ["assessment", id]); `AssessmentDashboard`
  filters `!f.dismissed` at source so every view (rollups/donut/area/list) drops it. Test: dismiss sets
  flag + zeroes totals + response shows dismissed:true.
- Backend **235 passing**; frontend build clean.

## Assumptions (as of final state)

- Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`) is public, no-auth, USD
  default; region names lower-case (`eastus`). Engine trusts server-side `$filter` for SKU scoping.
- Delegated tokens carry `tid` (tenant) + `oid` (user) claims; `tid` is the tenant isolation boundary.
  Token signature is NOT verified in-app (Azure validates it on use) — pre-existing design.
- SQLite for dev; swappable to Postgres via `DATABASE_URL`. Migrations use `batch_alter_table` so
  they run on SQLite too.
- In-memory cache + rate limiter + circuit-breaker state are per-process (fine for single instance;
  need Redis for horizontal scale).
- Azure Monitor exposes VM CPU as "Percentage CPU" at P1D over a 7-day window.
- Cost Management "ActualCost" over the trailing window ≈ monthly spend (normalised ×30/days).

---

## Pending / Deferred (explicit, for a future session)

- **Redis backends** — cache (pricing), rate limiter, and circuit-breaker state are in-memory behind
  clean interfaces; swap to Redis for multi-instance deployment.
- **`debug_reason` scaffolding** — dev-only, gated by `DEBUG_FINDINGS_REASONING` (default false).
  Remove or make admin-only before prod; TODO markers in `config.py`, `findings.py`,
  `models/db.py`, `models/schemas.py`, `components/FindingsTable.tsx`.
- **Oversized VM savings** — uses a 50%-of-PAYG "one tier down" heuristic; a real SKU-ladder lookup
  (D2→D1 actual price delta) would be more accurate.
- **App Service Plan pricing** — still a small static estimate table (`_ASP_COST` in findings.py);
  live retail ASP meter mapping deferred (VM/disk/IP/RI are live).
- **RBAC granularity** — access proven via "can you list the subscription"; a per-action
  `Microsoft.Authorization/permissions` check is deferred.
- **External audit sink** — audit trail is in the app DB; shipping to a SIEM is deferred.
- **Frontend** — dismiss button UI (API method exists); bundle code-splitting (>500kB warning).
- **README** — not updated in this pass; user to review docs later.
