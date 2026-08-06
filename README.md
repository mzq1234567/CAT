# Azure Cost Assessment Tool (Azure CAT)

A multi-tenant SaaS **web app** that produces **consulting-grade Azure cost assessments**. A consultant
(or a client) signs in with their own Azure credentials (delegated auth — **read-only**, nothing is ever
created, modified, or deleted), points the tool at one or more subscriptions, and gets back a
client-ready report of concrete, currency-quantified savings — grounded in the client's **actual bill**,
not list price.

It replaces a manual, spreadsheet-driven assessment process and mirrors that methodology exactly: read
last month's real spend, validate it against several months of history, and recommend only what Azure
genuinely offers for each resource.

> **If you are ChatGPT reading this:** this is the complete, current brief. The section most likely being
> asked about is **Authentication flow** — specifically the "needs admin approval" prompt and whether the
> app registration / consent can be avoided. Everything needed is in that section.

---

## What it finds

Every resource type in the subscription (not just VMs), each a **single classified finding**, grounded
and quantified:

- **Reserved Instances (1yr & 3yr)** and **Savings Plans (1yr & 3yr)** — from Azure's own usage-based
  reservation engine (or a real-retail fallback). A VM goes into *either* RI *or* SP, never both.
- **Azure Hybrid Benefit** — Windows Server AHB (VMs) and SQL Server AHB (vCore SQL DB/MI).
- **Right-sizing** (metric-driven, conservative, grounded): VMs, App Service Plans, SQL Databases, SQL
  Managed Instances, and **disk Premium → Standard SSD** (disks on SQL VMs excluded — SQL needs Premium
  latency even at low IOPS).
- **Waste / orphans**: idle & deallocated VMs (quantified by their still-billing disks), unattached
  disks, orphaned public IPs, empty load balancers, idle NAT gateways, orphaned snapshots, GRS backup
  vaults, idle App Service Plans, paused SQL DBs, stopped SQL MIs, Bastion review.
- **Azure Advisor** cost recommendations, re-scored and validated.

Output: an interactive **executive dashboard** (drill-down per finding, exclude/restore support) and a
branded **PDF report** with a 3-year spend projection computed from the environment's *measured*
growth trend.

While a run is in flight the client watches a **live assessment screen** — an abstract flow
composition, the current stage, an eased progress filament, and discovered counts that surface one at
a time. Every figure on it is a measured scan result and the stage reflects the backend's real state,
so a slow subscription visibly holds the run where it is rather than advancing on a timer.

---

## How the numbers are trustworthy (the differentiator)

**Never show a fabricated number as if it were real.**

- **Grounded in actual cost** — every headline saving is a fraction of the resource's real Cost-Management
  bill (discount % for RI/SP, licence fraction for AHB, vCore-reduction ratio for rightsizing). List
  price is only a labelled fallback.
- **Last complete month**, validated against **6 months of history**; **savings can never exceed spend**
  (each finding is capped at its own resource's cost); **no double-counting** (AHB licence + RI compute
  are additive but never overlap; deletion candidates excluded from AHB).
- **Only what Azure offers**, **currency-correct** (USD/CAD/INR/GBP/AUD/…, severity normalised to
  USD-equivalent), **rightsizing is cautious** (peak not average, all load dimensions under a 70%
  ceiling, real price deltas only, SQL only where real cost exists).
- **Validation badges** — "Cost-validated" (grounded) vs "Estimate" (small nominal figures). Confidence
  is tracked for ranking but **not shown to clients**.

**AHB, plainly:** a Windows VM's price = compute + a Windows Server licence. The licence is a flat
per-vCore charge, estimated as the **median across the environment's Windows VMs** (robust to a bad
per-SKU price fetch) and applied to each VM's real bill — realised only if you own the licence with
Software Assurance. SQL AHB works the same way (fixed per-vCore SQL licence).

---

## Project architecture

A single-page web app talking to a Python API that orchestrates read-only Azure calls:

```
Browser (React SPA)  ──MSAL login──►  Microsoft Entra ID
   │  (Azure ARM token, delegated)
   ▼
FastAPI backend  ──background job──►  Azure REST APIs (as the user):
   │                                    Resource Graph, Monitor, Advisor,
   │                                    Cost Management, Consumption, Retail Prices
   ├── findings engine (grounding, dedup, scoring)
   ├── SQLite (assessments, findings, inventory, audit)
   └── PDF report (reportlab)
```

- **Stateless per request** on Azure — the user's token is used in-memory for the run, never persisted.
- **Background assessment** — POST returns `202`; the frontend polls status until `completed`. While
  it runs, the pipeline writes timestamped **events** as each real milestone is reached, so progress
  reflects work actually done rather than a timer.
- **Read-only** — verified: only GET/query calls, no writes.

---

## Folder structure

```
frontend/          React + Vite + MUI + Recharts (currency-aware, interactive charts)
  src/
    auth/          MSAL config (msalConfig.ts)
    services/      api.ts (typed API client + token acquisition)
    pages/         Login, SelectSubscriptions, Results
    components/assessment/  Running-assessment experience (FlowField, StageCaption,
                            ProgressThread, DiscoveryMetrics, stages)
    components/dashboard/   ExecutiveSummary, RecommendationCard, FindingEvidence, charts/
backend/           FastAPI + SQLAlchemy + SQLite
  app/
    main.py        App wiring, routers, CORS, serves built frontend
    api/
      routes/      assessments.py, subscriptions.py
      dependencies.py   Bearer-token extraction + JWKS verification
    services/
      azure_client.py     ARM/Consumption/Cost-Management client (retry + circuit breaker)
      inventory.py, kql.py  Resource Graph collection (server-side KQL)
      metrics.py            Azure Monitor: VM / ASP / SQL DB / SQL MI / disk-IOPS enrichment
      pricing.py            Retail Prices API (currency-aware, cached; live LB/NAT/Bastion/ASP)
      estimates.py          Dated table of must-stay estimates (snapshot, GRS vault, SQL licence)
      cost_management.py    Actual cost, currency, consistency, growth-rate trend
      reservations.py       Parse Azure reservation recommendations
      findings.py           The findings engine (all detectors, grounding, dedup, scoring)
      currency.py           USD<->billing-currency
      assessment.py         Orchestrator (state machine, background task)
      report.py             Branded PDF + trend-based 3-year projections
    security/        JWKS RS256 verification, RBAC, rate limiting, audit log
    models/          db.py (SQLAlchemy models), schemas.py (Pydantic)
    database.py      SQLite via SQLAlchemy
  alembic/           Migrations 001–011
  scripts/           reconcile_assessment.py (per-finding integrity validation harness)
```

---

## Assessment engine

`services/findings.py` is the core. For each detector it: (1) reads the resource's utilisation +
**actual cost**, (2) computes a **grounded** saving (discount/licence/vCore ratio × real cost), (3)
**caps** it at the resource's actual cost, (4) **dedupes** to one finding per resource, (5) scores
severity/confidence. Aggregates (AHB, RI, SP) roll many resources into one resource-less finding so they
escape dedupe and can be additive. `services/assessment.py` is the state machine that fans out inventory
→ metrics → advisor/reservations → cost, runs every detector, and persists. `scripts/reconcile_assessment.py`
re-derives every finding and asserts the integrity invariants (Σfindings = total, savings ≤ spend, no
double-count).

---

## Authentication flow

### Today
Browser web app: a **React SPA + MSAL** signs the user in against a **multi-tenant Entra ID app
registration**, requesting delegated `https://management.azure.com/user_impersonation` (+ `openid profile
email`). MSAL acquires an Azure ARM token; the SPA sends it as a Bearer header to FastAPI, which calls
Azure **as the user**. **Read-only** (verified — only GET/query, no writes). The token is used in-memory
and **not persisted/logged**. Tenant isolation: the backend verifies the token's **RS256 signature vs
Entra's JWKS** (blocks `alg=none`/HS256 confusion) and scopes records to the caller's `tid`+`oid`.

The app **registration** (client_id) lives in the **developer's** home tenant. When a user from a
**client** tenant first signs in, Entra **auto-creates a service principal (enterprise app)** in that
tenant — standard for any multi-tenant app, holds only the consent grant (no standing access, no
credential), and is admin-revocable.

### The "needs admin approval" prompt (and whether it can be removed)
This is a **tenant policy**, not the tool, and **cannot be turned off app-side.** It's the target tenant's
**user-consent setting**:
- *Allow user consent* → user self-consents, no admin needed.
- *Do not allow user consent* (Microsoft's current default) → because ARM `user_impersonation` is **not**
  "low-impact," an **admin must consent once**; after that, every user logs in with no prompt on their own
  **Reader** role. Admin-consent URL:
  `https://login.microsoftonline.com/<tenant-id>/adminconsent?client_id=<client-id>&redirect_uri=<uri>`
- Who can consent: an **Entra directory admin** (Global / Cloud App / Application Administrator). A
  subscription **"Reader" cannot** — that's an Azure RBAC role, unrelated to directory consent.

### Two open decisions
1. **Avoid app registration / consent entirely?** Not for a web app — a browser login *requires* its own
   client_id, and any multi-tenant app creates the SP + hits the consent policy. The **only** way to zero
   it out is a different architecture: a **read-only CLI / Cloud Shell script** the user runs in their own
   Azure (uses **Microsoft's own** built-in identity + their Reader, creates nothing, needs no consent)
   that exports a JSON this tool **ingests**. (Do **not** "borrow" the Azure CLI's client_id in a web app
   — unsupported, Microsoft blocks it.)
2. **The token the backend receives is write-capable** (inherits the user's RBAC — usually Owner). The
   code only reads, but for **client self-service** a security team will flag it. Structural fix (not
   built): a **read-only scoped identity** (client grants a service principal only Reader + Cost
   Management Reader via a Deploy-to-Azure template; the tool auths as *that*), or the agentless route.

---

## Azure resources required

The tool **provisions nothing** — it only reads. To run it you need:

- **An Entra ID app registration** (multi-tenant, delegated `user_impersonation` + `openid/profile/email`)
  — one, created once by you.
- **Azure RBAC roles** on the assessed subscription(s), held by the **signed-in user**:
  **Reader** (inventory/metrics/advisor) **+ Cost Management Reader** (the actual-bill grounding). Without
  Cost Management Reader the tool still runs but falls back to list-price estimates.
- Azure services *consumed* (read-only, no charge beyond normal API usage): **Resource Graph, Azure
  Monitor, Azure Advisor, Cost Management, Consumption (reservations)**, and the **public Retail Prices
  API** (no auth).
- *(Deployment only)* an **Azure App Service** (or any host) if you deploy it as a hosted web app.

---

## Setup instructions

**App registration:** multi-tenant, SPA redirect `http://localhost:5173`, delegated permissions above.
Copy the **Application (client) ID** into both `.env` files. `.env` holds only the **public** client ID
(non-secret) and is gitignored; there are **no client secrets** anywhere (SPA uses PKCE; backend rides on
the user's delegated token).

## Local development

```bash
# Backend
cd backend
cp .env.example .env                 # set AZURE_CLIENT_ID=<client-id>
python -m venv .venv && .venv\Scripts\activate      # (source .venv/bin/activate on *nix)
pip install -r requirements.txt
alembic upgrade head                 # REQUIRED after pulling changes (adds new columns/tables)
uvicorn app.main:app --reload --port 8000    # (or: .\start.ps1 on Windows — venv + deps + serve)

# Frontend (separate terminal)
cd frontend
cp .env.example .env                 # set VITE_AZURE_CLIENT_ID=<client-id>
npm install && npm run dev           # http://localhost:5173
```

> After pulling changes, **always run `alembic upgrade head`** — new columns and tables are added via
> migrations; skipping it 500s new assessments. `.env`, `.venv` and `cat.db` are gitignored, so each
> machine keeps its own and must migrate independently.

## Deployment

Deploys as a single **Azure App Service** (FastAPI serves the built React app):

```bash
cd frontend && npm run build         # → frontend/dist/ (served by FastAPI)
# App Service startup command:  bash startup.sh
# App Settings:  AZURE_CLIENT_ID=<client-id>
#                CORS_ORIGINS=["https://<your-app>.azurewebsites.net"]
#                VERIFY_TOKEN_SIGNATURE=true
# On deploy, run:  alembic upgrade head
```

Add `https://<your-app>.azurewebsites.net` as a redirect URI on the app registration. For scale beyond a
single instance, swap SQLite for Postgres (`DATABASE_URL`) and move the in-memory cache/rate-limiter to
Redis.

---

## Database

**SQLite** (dev; swappable to Postgres via `DATABASE_URL`) via SQLAlchemy, migrated with **Alembic
(001–011)**. Five tables:

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `assessments` | one per run | status/progress, `total_savings_monthly/annual`, `current_monthly/annual_spend`, `currency`, `cost_data_available`, `observed_annual_growth`, `spend_by_area`, `total_resources`, `major_resource_types` |
| `findings` | one per opportunity | `category`, `estimated_savings_monthly/annual`, `severity`, `confidence`, `validation_status`, `actual_monthly_cost`, `dismissed`, `details` (JSON) |
| `inventory_items` | scanned resources | `resource_id`, `resource_type`, `data` (JSON) |
| `assessment_events` | live pipeline trail | `timestamp`, `stage`, `message` — written as each milestone is genuinely reached |
| `audit_logs` | security trail | `event` (assessment_run / finding_dismissed / report_downloaded), user, timestamp |

> **Existing databases:** if `alembic upgrade head` fails with *"table assessments already exists"*,
> the `alembic_version` marker is missing rather than the schema being old. Check `alembic current`
> first — if it prints nothing, confirm which columns/tables already exist before stamping, since
> `alembic stamp head` would skip creating anything genuinely absent.

---

## API endpoints

All under `/api`, all require a valid Bearer token (verified vs Entra JWKS).

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/subscriptions/` | List the user's Azure subscriptions |
| POST | `/api/assessments/` | Start an assessment (returns `202` + summary; runs in background) |
| GET  | `/api/assessments/` | List the user's assessments |
| GET  | `/api/assessments/{id}` | Assessment detail (findings + live `events`) — polled until `completed` |
| GET  | `/api/assessments/{id}/findings` | Flat list of findings |
| GET  | `/api/assessments/{id}/findings/by-category` | Findings grouped by category (totals) |
| POST | `/api/assessments/{id}/findings/{finding_id}/dismiss` | Exclude a finding from savings (re-rolls totals; audited) |
| POST | `/api/assessments/{id}/findings/{finding_id}/restore` | Undo an exclusion (re-rolls totals; audited) |
| GET  | `/api/assessments/{id}/report/pdf` | Download the branded PDF |

---

## Azure SDKs used

**Backend: none.** It calls the Azure **REST APIs directly via `httpx`** (`management.azure.com` for
ARM/Consumption/Cost-Management, `prices.azure.com` for Retail Prices). This is deliberate — direct REST
gives full control over throttling/`Retry-After`/pagination/circuit-breaking, and avoids heavy SDK
dependencies for what are a handful of endpoints. Token verification uses `PyJWT` + `cryptography`
against Entra's JWKS.

**Frontend: MSAL only** — `@azure/msal-browser` + `@azure/msal-react` for the login + ARM token
acquisition. No other Azure SDK.

---

## Future roadmap

- **Auth for client self-service** — the read-only **scoped identity** (Deploy-to-Azure template →
  Reader + Cost Management Reader SP) and/or the **agentless Cloud Shell** collection script, so clients
  can run it without handing a write-capable token to the server and without the consent friction.
- **Scale** — SQLite → Postgres, in-memory cache/rate-limiter → Redis, a real job queue + observability
  (retry/resume, monitoring) for background assessments.
- **More coverage** — DTU-model SQL rightsizing (vCore is done), storage-account redundancy (GRS→LRS),
  a few remaining orphan types (NICs, unused gateways), dev/test auto-shutdown.
- **Product** — custom logo upload, historical trending across assessments, and one human end-to-end
  validation against a real steadily-running production environment.

*(Not feasible: true blob hot/cool/archive tiering — it needs per-blob access data Azure Resource Graph
doesn't expose, so guessing it could raise costs.)*

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, Vite, Material UI v6, Recharts, MSAL React, React Query |
| Backend | FastAPI, Uvicorn, SQLAlchemy, Alembic, httpx, PyJWT |
| Reporting | reportlab (PDF) |
| Database | SQLite (local), swappable via `DATABASE_URL` |
| Azure APIs (raw REST) | Resource Graph, Monitor, Advisor, Cost Management, Consumption, Retail Prices |
| Auth | Microsoft Entra ID (multi-tenant, delegated), MSAL + PKCE, JWKS RS256 verification |

Backend tests: **243 passing** (`cd backend && python -m pytest -q` — run from `backend/`, not the
repo root). Frontend: `npm run build`.
