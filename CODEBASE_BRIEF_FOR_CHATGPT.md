# Azure Cost Assessment Tool (CAT) — Codebase Brief

> **Purpose of this document:** I'm using ChatGPT to help me draft prompts/instructions that I then
> give to Claude Code (Anthropic's coding assistant), which is the tool actually writing code in this
> repo. This doc gives you (ChatGPT) enough context on what the app is, how it's built, and what
> state it's in, so you can help me write clear, well-scoped instructions for Claude. You are not
> editing code directly — you're helping me plan what to ask Claude to do.

---

## 1. What this tool does

Azure CAT is a **multi-tenant SaaS web app** that scans a customer's Azure subscriptions and finds
cost-saving opportunities (idle VMs, unattached disks, orphaned IPs, oversized resources, Reserved
Instance candidates, etc.), then produces a PDF report with estimated monthly/annual savings.

**Flow:**
1. User signs in with their own Microsoft/Azure account (delegated auth — no client secrets, the app
   only sees what the signed-in user already has access to).
2. User picks one or more Azure subscriptions they want assessed.
3. Backend runs a multi-stage background job: pull resource inventory → pull utilization metrics →
   pull Azure Advisor recommendations → pull actual historical costs → calculate live prices →
   detect findings → generate report.
4. Frontend polls for progress and shows a live status bar, then a results table.
5. User downloads a PDF report, or dismisses findings they don't care about.

This is aimed at consultants/MSPs or an internal team running cost assessments for multiple client
Azure tenants — hence "multi-tenant," "tenant isolation," "audit logging," etc. below.

---

## 2. Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + Vite + TypeScript, Material UI v6, Recharts, MSAL (Microsoft auth), React Query |
| Backend | Python 3.14, FastAPI, SQLAlchemy ORM, Alembic (migrations), httpx (async HTTP), pytest |
| Database | SQLite for dev (`cat.db`), designed to swap to Postgres via `DATABASE_URL` |
| Auth | Microsoft Entra ID (Azure AD) via MSAL — delegated, multi-tenant, no app secrets |
| Reports | ReportLab (PDF) |

No Docker/Kubernetes in this repo. It runs as a plain FastAPI process (serving the built React app as
static files in production) plus a SQLite file. Deploy target mentioned in docs is Azure App Service.

---

## 3. Folder structure

```
CAT/
├── memory.md                    # Running engineering log — every past change, decision, and
│                                 # tradeoff is recorded here by Claude. THIS IS THE SOURCE OF TRUTH
│                                 # for "what have we already built." Always check this first.
├── README.md, SETUP.md          # User-facing setup docs
│
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app entrypoint, middleware, exception handlers
│   │   ├── config.py            # All environment-variable-driven settings (pydantic Settings)
│   │   ├── database.py          # SQLAlchemy engine/session setup
│   │   ├── logging_config.py    # Structured JSON logging w/ request/user/tenant context
│   │   ├── middleware.py        # Request-ID + timing middleware
│   │   │
│   │   ├── api/
│   │   │   ├── dependencies.py  # get_current_user — decodes the bearer token, extracts user/tenant
│   │   │   └── routes/
│   │   │       ├── assessments.py   # Create/list/get assessments, dismiss finding, download reports
│   │   │       └── subscriptions.py # List the signed-in user's Azure subscriptions
│   │   │
│   │   ├── models/
│   │   │   ├── db.py            # SQLAlchemy tables: Assessment, Finding, InventoryItem, AuditLog
│   │   │   └── schemas.py       # Pydantic response models (what the API returns as JSON)
│   │   │
│   │   ├── security/
│   │   │   ├── rate_limit.py    # Per-user/tenant sliding-window rate limiter
│   │   │   └── rbac.py          # Confirms caller actually has Reader access to requested subs
│   │   │
│   │   └── services/            # ALL the actual business logic lives here
│   │       ├── azure_client.py      # Thin async HTTP client for every Azure REST API we call
│   │       ├── kql.py               # KQL (Kusto) query strings sent to Azure Resource Graph
│   │       ├── inventory.py         # Runs all KQL queries in parallel, returns inventory buckets
│   │       ├── metrics.py           # Pulls Azure Monitor CPU metrics for VMs
│   │       ├── pricing.py           # Live pricing via Azure Retail Prices API + caching
│   │       ├── cost_management.py   # Actual historical cost + validates estimates against it
│   │       ├── findings.py          # THE CORE ENGINE — turns inventory+metrics+advisor into findings
│   │       ├── assessment.py        # Orchestrates the whole pipeline end-to-end (the "conductor")
│   │       ├── state_machine.py     # Assessment status/progress state machine
│   │       ├── resilience.py        # Retry-with-backoff + circuit breaker for Azure API calls
│   │       ├── audit.py             # Writes audit log entries for sensitive actions
│   │       ├── report.py            # Generates the PDF report file
│   │       └── cache.py             # Generic in-memory TTL cache (used by pricing.py)
│   │
│   ├── alembic/versions/        # Database migration files (001 → 004 so far)
│   ├── tests/                   # pytest suite — 96 tests, all passing, all Azure calls mocked
│   ├── requirements.txt
│   └── .env.example              # Every environment variable the backend reads, documented
│
└── frontend/
    └── src/
        ├── App.tsx               # Routes + MSAL auth gate
        ├── pages/
        │   ├── Login.tsx
        │   ├── SelectSubscriptions.tsx  # Pick subscriptions, kick off an assessment
        │   └── Results.tsx              # Progress bar while running, then results view
        ├── components/
        │   ├── FindingsTable.tsx        # The main findings grid (filters, sort, expand rows)
        │   ├── SummaryCards.tsx         # Top summary tiles (total savings, counts, etc.)
        │   └── Layout.tsx
        ├── services/api.ts       # All backend API calls (axios + MSAL token attach)
        ├── types/index.ts        # TypeScript types mirroring the backend's Pydantic schemas
        └── theme.ts               # Dark enterprise MUI theme
```

---

## 4. The assessment pipeline (this is the heart of the app)

When a user clicks "Run Assessment," `services/assessment.py::run_assessment()` runs as a background
task and drives an explicit state machine through these phases (each persisted to the DB so the
frontend can show real progress, not a fake spinner):

```
QUEUED
  → FETCHING_RESOURCES     (Resource Graph KQL queries — server-side filtered, e.g. "give me only
                             UNATTACHED disks," not "give me all disks and I'll filter in Python")
  → FETCHING_METRICS       (Azure Monitor: avg CPU over 7 days for running VMs)
  → RUNNING_ADVISOR        (Azure Advisor's own cost recommendations)
  → CALCULATING_PRICES     (Azure Retail Prices API — live, region-aware pricing, cached 24h)
  → DETECTING_FINDINGS     (the findings engine combines all of the above into scored findings)
  → GENERATING_REPORT
  → COMPLETED / FAILED
```

At `FETCHING_RESOURCES` the app stamps a `snapshot_at` timestamp — this is the "as of" time shown in
reports, because a resource's state can drift between when we scan it and when the report is read.

### What counts as a "finding" and how it's scored

Each finding (e.g. "this disk is unattached and costs $19.71/mo") carries:
- **severity**: critical / high / medium / low — driven by dollar amount, can be raised by Advisor's
  own impact rating.
- **confidence** (0–1): how much to trust the number. Lower when: pricing had to fall back to a
  static estimate, or the estimate significantly exceeds the resource's actual measured cost.
- **validation_status**: `validated` / `needs_review` / `unvalidated` — we cross-check every dollar
  estimate against the resource's *actual* historical spend (via Cost Management API). If an
  estimate implies saving more than the resource has ever cost, it's flagged `needs_review`.
- **advisor_recommendation_id**: if Azure Advisor already flagged the same resource, we link to it.
- **debug_reason**: a DEV-ONLY plain-English explanation of exactly why this finding fired (e.g. "avg
  CPU 2.1% over 7 days, below the 5% idle threshold"). Off by default in production
  (`DEBUG_FINDINGS_REASONING=false`); toggled on in the frontend findings table behind a switch.

Categories detected: unattached managed disks, orphaned public IPs, idle App Service Plans,
deallocated VMs, paused/inactive SQL databases & managed instances, idle VMs (low CPU), oversized VMs
(moderate CPU — rightsizing candidate), Reserved Instance candidates (steady CPU, cheaper on a 1yr
RI than pay-as-you-go), plus Azure Advisor's native cost recommendations re-scored the same way.

---

## 5. Security model

- **Auth**: delegated MSAL tokens. The backend never has its own Azure credentials — every Azure API
  call uses the signed-in user's own token, so the app can only see what that user can already see.
- **Tenant isolation**: every assessment/finding is tagged with the user's Azure AD tenant ID. Every
  read/write query filters on `(user_id, tenant_id)` — a user literally cannot fetch another tenant's
  data even by guessing an ID (returns 404, not 403, to avoid leaking existence).
- **RBAC**: before running an assessment, the backend confirms the caller actually has Reader access
  to every subscription they asked for (by checking Azure's own subscription-list API).
- **Rate limiting**: sliding-window, per (tenant, user) key, on the "create assessment" endpoint.
- **Audit log**: a dedicated `audit_logs` DB table records every assessment run, finding dismissal,
  and report download — who, when, what, from which request.
- **Resilience**: all Azure API calls go through retry-with-exponential-backoff (for 429/503
  throttling) plus a circuit breaker (fails fast if Azure is having a bad time, instead of hammering
  it). Unhandled errors never leak stack traces to the client — they're logged with a request ID and
  the client gets a generic message + that request ID to reference.

---

## 6. Database (SQLite, via SQLAlchemy + Alembic)

**`assessments`** — one row per assessment run: who ran it, which subscriptions, current
state-machine status + progress %, rollup totals (monthly/annual savings, findings count, how many
need review), snapshot/completion timestamps.

**`findings`** — one row per detected finding: category, resource identity, dollar estimates,
severity, confidence, validation status/variance, correlated Advisor ID, debug reason, dismissal
state (who dismissed it and when).

**`inventory_items`** — raw resource data collected during the scan (kept for audit/debugging).

**`audit_logs`** — security event trail (see above).

Migrations are in `backend/alembic/versions/` (currently 001 → 004). **Known gotcha:** an
old/pre-existing local `cat.db` created before a migration was added won't have the new columns
automatically — `Base.metadata.create_all()` only creates missing tables, it never alters existing
ones. There's a one-off reconciler script at `backend/scripts/reconcile_db.py` for that situation.

---

## 7. Current state of the project (as of this writing)

This app started as a working MVP (login → pick subscriptions → run assessment → see results →
download report) and has since gone through a large "make it enterprise-grade" upgrade pass. That
upgrade is **complete**:

- ✅ Live, region-aware Azure pricing (was: hard-coded 2023 price tables)
- ✅ Server-side KQL filtering in Resource Graph (was: fetch-everything-then-filter-in-Python)
- ✅ Cost Management API integration to validate every savings estimate against actual spend
- ✅ Explicit state machine + real progress % (was: a fake spinner)
- ✅ Findings engine with severity/confidence/Advisor-correlation/debug-reasoning
- ✅ Tenant isolation, RBAC, rate limiting, audit logging
- ✅ Retry/backoff + circuit breaker for Azure API throttling
- ✅ Reports now include an "as of" timestamp and a validation/variance section
- ✅ Frontend shows real progress, confidence scores, validation flags, and a debug-info toggle
- ✅ 96 backend tests passing (pytest, all Azure calls mocked — no live Azure access needed to test)
- ✅ Frontend type-checks and builds cleanly

**`memory.md` at the project root has the full, dated decision log for every one of the above** —
what was built, which files touched, why specific tradeoffs were made, and what's explicitly
deferred. If you (ChatGPT) are helping me plan new work, ask me to paste in relevant sections of
`memory.md` rather than assuming — it's the actual source of truth, not this summary.

### Known deferred / not-yet-done items
- Redis-backed cache/rate-limiter/circuit-breaker (currently in-process memory — fine for one
  instance, not for horizontal scaling)
- A proper SKU-ladder for "oversized VM" downsize savings (currently a 50%-of-current-cost estimate)
- Live retail pricing for App Service Plans (still a small static table; VM/disk/IP/RI are live)
- Finer-grained RBAC (currently "can list the subscription" as a proxy for "has Reader role")
- A dismiss-finding button in the UI (the API endpoint + backend logic exist, no button wired yet)
- Frontend bundle is a single >500kB chunk (no code-splitting yet)

---

## 8. How to help me write prompts for Claude

Claude Code is the one actually writing/editing code in this repo. It has full read access to
everything above, plus `memory.md`. When helping me draft an instruction for Claude:

- **Reference specific files/services by name** (e.g. "in `findings.py`'s `detect_vm_utilisation_findings`")
  rather than vague asks — Claude works best with concrete file/function references.
  Feel free to ask me to paste the current content of a file if you need to see it to advise me.
- **Tell it whether to write code or just investigate/plan.** If unsure, default to "investigate and
  propose a plan first."
- **Ask it to write/run tests** for anything nontrivial — this repo has an established pytest
  convention (mocked Azure APIs via `httpx.MockTransport`, fixtures in `tests/conftest.py` and
  `tests/azure_mocks.py`).
- **Ask it to update `memory.md`** as part of any nontrivial change — that's the established
  convention in this repo and keeps future sessions (or future me) oriented.
- If a request could affect production/security/tenant-isolation/pricing-accuracy behavior, flag
  that explicitly so Claude treats it carefully rather than as a quick tweak.

---

*This document is a snapshot summary. For anything time-sensitive or exact, the authoritative sources
are `memory.md` (decision history) and the actual code (current truth) — not this file.*
