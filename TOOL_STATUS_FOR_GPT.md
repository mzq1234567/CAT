# Azure Cost Assessment Tool (CAT) — Current Status & Known Issues

_Hand-off brief for ChatGPT. Written 2026-08-12. Self-contained: assumes no prior context._

---

## 1. What the tool is

A **multi-tenant SaaS** that analyses a client's Azure subscriptions and produces a
client-presentable cost-optimisation report (dashboard + downloadable PDF). Users sign in with
their **own** Azure credentials (delegated MSAL auth, Reader-only, nothing is ever created or
deleted in their environment). Branding: **TPT (Tech Plus Talent)**.

The product promise that drives every design decision: **the numbers must never be wrong or
fabricated.** It is better to show "unavailable" or withhold a finding than to present a guessed
number a client could challenge.

## 2. Tech stack & architecture

- **Frontend**: React 18 + Vite + Material UI v5 + Recharts. **Light** enterprise theme (teal brand
  `#0AA6BA`, savings green `#12B886`). Entry views: dashboard (`AssessmentDashboard`), executive
  KPI cards, charts, category tiles, opportunity cards, right-side detail drawer.
- **Backend**: FastAPI + SQLAlchemy + SQLite (`DATABASE_URL` configurable). Async `httpx` to Azure.
- **Reports**: ReportLab (PDF).
- **Key backend files** (`backend/app/services/`):
  - `azure_client.py` — all Azure REST calls (ARM, Resource Graph, Advisor, Cost Management,
    Consumption reservationRecommendations, Monitor metrics), with retry/backoff + a circuit breaker.
  - `assessment.py` — the orchestrator (`run_assessment`): inventory → metrics → advisor →
    reservations → cost → findings → persist.
  - `findings.py` — `FindingsEngine`: every detector + the central `_finding()` that grounds,
    validates and caps each saving.
  - `cost_management.py` — per-resource cost map, representative-cost logic, run-rate baseline,
    currency detection.
  - `pricing.py` — live Azure Retail Prices API (cached, daily refresh), in the billing currency.
  - `reservations.py` — parses Azure's reservation recommendations.
  - `report.py` — PDF generation.
- **DB**: `Assessment`, `Finding`, `InventoryItem`, `AssessmentEvent`, `AuditLog`. Tables created via
  `Base.metadata.create_all`; **Alembic exists but is not used in dev** — a small idempotent
  `ensure_runtime_columns()` shim ALTERs in any newly-added columns on startup.
- **Tests**: ~259 pytest tests, all passing. `cd backend` before running (cwd resets to repo root).
  Backend must be **restarted** to pick up code changes; the frontend build is `npx vite build`.

## 3. Core philosophy — grounding (read this to understand every number)

Every saving = **the resource's ACTUAL billed cost** (from Cost Management) × a ratio
(discount % / licence fraction / vCore reduction). `_finding()` then caps it at (a) the resource's
own actual cost and (b) the subscription's **measured monthly spend** (an absolute global ceiling —
no single finding can exceed what the client actually pays).

- A finding that **cannot be grounded** in real billing is **excluded** and counted separately —
  never priced at list. (This killed two historical bugs: a ₹888K AHB figure and a ₹318K oversized-VM
  figure that were list-price fantasies.)
- **Representative monthly cost**: stable resource → last complete month; erratic (coefficient of
  variation > 0.25) → `min(historical mean, current run-rate)` so it never over-states; partial
  billing (< 2 complete months) → run-rate = avg daily × 30.4375 over the observed span; no cost →
  suppress. Findings disclose `cost_basis`, `cost_is_estimate`, `cost_variability`, and surface BOTH
  the historical mean and the current run-rate when they diverge materially.
- **Four distinct numbers** are kept separate and never conflated: (1) actual billed cost,
  (2) historical representative cost, (3) current run-rate, (4) estimated saving.
- **Currency**: detected from Cost Management (e.g. INR); retail prices fetched in that same currency;
  every figure rendered in it. `spend_estimated` / `spend_period_days` flag a projected run-rate.

## 4. Data sources & what each authoritatively provides

| Source | Provides | Notes |
|---|---|---|
| Resource Graph | inventory (VMs, disks, IPs, SQL, NAT, Bastion, etc.) | authoritative for existence/state |
| Azure Monitor metrics | 30-day CPU/memory | drives idle / oversized detection |
| Cost Management (per-resource) | billed cost per resource | **grounds** findings; throttles hard |
| Cost Management (per-service) | subscription total spend | headline spend; separate query |
| Consumption / reservationRecommendations | **the only** RI source | Azure simulates real usage at real prices |
| Azure Advisor | Microsoft's own cost recs | shown, but NOT branded as our feature |

## 5. Finding categories — current state

- **Grounded-only** (suppressed if no billed cost): idle/oversized VM right-sizing, deallocated VMs,
  **Windows AHB**, idle NAT gateways, and other idle/orphaned resources where billing is required.
- **List-price fallback** (can show without billing, marked `unvalidated`): unattached disks, orphaned
  IPs, Bastion hosts, snapshots, etc. — but these are now **withheld** on a degraded billing run
  (see §6).
- **Reservations (RIs)**: authoritative-only — from `Microsoft.Consumption/reservationRecommendations`.
  The old retail-estimate detector was **retired**. Look-back now requests `Last30Days` (default was
  the strictest `Last7Days`).
- **SQL AHB**: **retired** — the retail API exposes only one compute meter per vCore (no base/AHB
  split), so it can't be quantified without guessing. Windows AHB is the only AHB.
- **Windows AHB**: conditional — only realised if the client already owns eligible Windows Server
  licences with Software Assurance. Grounded per-VM as `actual billed × (Windows−Linux)/Windows`.

## 6. Recent changes (this working session, 2026-08-12)

1. **UI/UX redesign** — removed the search bar; simplified recommendation cards (category · impact ·
   one confidence label · one-sentence summary · savings + affected count · "View details →");
   replaced the giant modal with a **right-side detail drawer** (Fluent-inspired, ~260ms slide-in,
   dashboard dimmed) with a clean fixed hierarchy; introduced a reusable **ResourceList** primitive;
   moved raw metrics behind a "Supporting metrics" disclosure; stopped branding Azure Advisor.
2. **Conditional AHB split out of the headline** — AHB savings are licence-conditional, so they are
   **excluded from the headline "Estimated Savings" total, the donut, category tiles and waterfall**,
   and shown separately as "+ ₹X/yr potential · Hybrid Benefit (needs licences)". (Previously AHB was
   folded into the total, where it dominated — e.g. 90% of one lab sub's headline.)
3. **Runtime-aware cost anomaly** — the "verify billing" red banner previously fired on any VM billing
   under 10% of its full-month list price. That false-fires on **part-time / mostly-deallocated VMs**.
   Now it computes effective uptime = `actual_cost / Linux_list`: **< ~1%** = genuine anomaly
   (compute effectively free → sponsored/credit/currency issue); **1–50%** = a soft "runs
   intermittently" note, no alarm.
4. **Graceful degraded-run handling** — when Cost Management returns the subscription total but **no
   per-resource billed cost** (a throttle on the heavier query): (a) retry the cost-map query once
   after a 25s back-off; (b) **withhold** ungrounded list-price findings (so no misleading number like
   a Bastion capped to the whole subscription spend); (c) show a prominent amber banner ("billing
   detail unavailable — results incomplete, re-run"); flagged by a new
   `Assessment.billing_detail_unavailable` column.
5. **Reservation look-back** set to `Last30Days` with per-subscription diagnostic logging.

## 7. Known issues & open problems

1. **Cost Management throttling → non-deterministic runs.** The per-resource cost query throttles
   aggressively; two runs minutes apart on the same subscription can differ dramatically (one grounded,
   one degraded). Mitigated (retry + withhold + banner), but the underlying throttling is external and
   still occurs. Re-running after a few minutes is the practical remedy.
2. **RIs almost never appear in the current test environment.** Across the entire local DB, **zero**
   assessments have ever produced an authoritative Azure RI. Root cause is the environment, not a bug:
   the test subscription is a **sponsored/lab sub whose VMs run ~1–2% of the time** (deallocated most
   of the month), so Azure correctly declines to recommend a 1–3yr reservation. Needs validation
   against a **real client environment with steady 24/7 workloads** to confirm RIs surface.
3. **Lab-environment skew generally.** The main test sub is training/session VMs (names like
   `TPT-Session-0`, `M365-SH-0`, `JugalVM`, `Attendance`). Low, intermittent usage makes every
   cost-based figure small and every projection shaky. Real-world validation is the biggest gap.
4. **Partial billing on new/migrated subscriptions.** With < 1 complete billing month, spend and
   savings are run-rate projections (avg daily × 30.44). Correct and clearly flagged, but annualising
   a few days of ephemeral usage is inherently uncertain.
5. **`billing_detail_unavailable` and finding-level flags are computed at run time.** Old assessments
   keep their stored state (e.g. a stale "verify billing" banner) until **re-run**. Frontend-derived
   aggregates (headline, donut, categories) recompute client-side and update on refresh, but
   backend-stored details do not.
6. **Frontend bundle size** — single JS chunk > 500 kB (Vite warning). Cosmetic; no code-splitting yet.
7. **Dev DB migrations** — Alembic is present but inert; schema changes rely on `create_all` +
   `ensure_runtime_columns`. Fine for dev; production needs real Alembic migrations.
8. **`debug_reason`** field on findings is dev-only scaffolding and must be gated/removed before prod.

## 8. Operational notes (to reproduce / validate)

- **After code changes**: restart the backend (uvicorn) — it does not hot-reload unless run with
  `--reload`. Then **re-run** an assessment for finding-level detail changes; **refresh** the browser
  for client-derived dashboard aggregates.
- **To validate RIs / right-sizing**: run against a subscription with steadily-running (24/7)
  production VMs and full Cost Management access — the lab sub cannot exercise these paths.
- **To confirm a degraded run**: check the assessment's event log for
  `Per-resource billed cost unavailable (Cost Management throttled)` vs `Matched billed cost for N
  resources`.

## 9. One-line summary

A grounded, defensible Azure cost-assessment tool whose core engine and presentation are solid; the
main open risk is **validation against a real steady-state client environment** (the current test sub
is a mostly-idle lab), plus the ever-present **Cost Management throttling** that occasionally degrades
a run (now handled gracefully rather than silently).
