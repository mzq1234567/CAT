# Azure Cost Assessment Tool (Azure CAT)

A multi-tenant SaaS platform that produces **consulting-grade Azure cost assessments**. A consultant
signs in with their own Azure credentials (delegated auth — Reader access, nothing is ever created or
deleted), points the tool at one or more subscriptions, and gets back a client-ready report of
concrete, dollar-quantified savings — grounded in the client's **actual bill**, not list price.

It was built to replace a manual, spreadsheet-driven assessment process, and it mirrors that
methodology exactly: read last month's real spend, validate it against several months of history, and
recommend only what Azure genuinely offers for each resource.

---

## What it finds

Across **every resource type in the subscription** (not just VMs), grouped into the three cost pillars
**Compute / Storage / Network**:

- **Reserved Instances (1-year & 3-year)** — per VM, only for SKUs Azure actually offers a reservation
  for; prefers Azure's own usage-based reservation-recommendation engine, falls back to a real-retail
  estimate.
- **Savings Plans (1-year & 3-year)** — for compute that a Reserved Instance isn't offered for.
- **Azure Hybrid Benefit** — the Windows Server licence portion of each Windows VM's bill, that you
  stop paying by applying a licence you already own.
- **Right-sizing** — VMs that peak (over 30 days, on **both** CPU and memory) low enough to fit a
  smaller SKU, with the exact target size and the real price delta.
- **Idle / deallocated VMs, unattached disks, orphaned public IPs, empty load balancers, idle NAT
  gateways, orphaned snapshots, geo-redundant backup vaults** — waste that can be removed.
- **Azure Advisor** cost recommendations, re-scored and validated consistently.

The output is an **executive dashboard** (interactive, drill-down per finding) and a downloadable
**PDF/Excel report** styled as a branded client deliverable — cover page, table of contents, executive
summary with 3-year spend projections, methodology, environment details, and per-optimisation tables.

---

## How the numbers are trustworthy (the important part)

A cost-assessment tool is only useful if a client can present its numbers without being wrong. The
design principle throughout is **never show a fabricated number as if it were real**.

- **Grounded in actual cost.** Every saving is a *fraction of what the resource really costs you* — the
  discount % (RI/SP) or licence fraction (AHB) applied to the resource's actual monthly bill from
  Cost Management. List price is only ever a labelled fallback, never the headline.
- **Last complete calendar month.** Cost is read from the last *complete* month (never the current,
  not-yet-fully-billed month), matching how a manual assessment reads "last month's bill." New
  subscriptions with no complete month fall back to month-to-date.
- **Month-over-month validation.** The tool pulls the last 4 complete months and flags each VM as
  *steady* (billed consistently → safe to reserve) or *erratic* (swings → lower confidence).
- **Savings can never exceed spend.** Because each finding is bounded by its own resource's real cost,
  the total is ≤ measured spend by construction — no impossible "you'll save more than you pay."
- **No double-counting.** One action per resource (you don't both delete a VM *and* reserve it); AHB
  (licence) and RI (compute) are additive but never overlap; a VM recommended for deletion is excluded
  from AHB.
- **Only what Azure offers.** RIs are only recommended for SKUs that actually have a reservation;
  everything else routes to a Savings Plan. Nothing is invented.
- **Currency-correct.** Figures render in the subscription's billing currency (USD/CAD/INR/GBP/AUD/…),
  and even severity banding is normalised to a USD-equivalent so an INR run doesn't mark everything
  "critical."
- **Reliable retrieval.** The heavily throttled billing APIs use patient, `Retry-After`-honouring
  retries isolated from the shared circuit breaker, so a transient throttle self-heals within the run
  instead of silently dropping cost data.

**Azure Hybrid Benefit, plainly:** a Windows VM's price = compute + a Windows Server licence. AHB lets
you use a licence you already own instead of renting Azure's, so you pay only the compute. The tool
finds the licence charge by subtracting the same-size **compute-only (Linux) price** from the **Windows
price** — the exact figure Azure's own pricing calculator shows — and applies it to the VM's real bill.
It's presented as a saving realised **only if you own (or buy) eligible licences with Software
Assurance**.

---

## How it works

1. Consultant signs in via Microsoft (MSAL delegated auth).
2. Selects one or more Azure subscriptions.
3. The backend runs a background assessment:
   - Inventory via **Azure Resource Graph** (server-side filtered, paginated).
   - Utilisation via **Azure Monitor** — peak CPU (Maximum) + peak memory (100 − min Available %) over
     30 days, concurrency-bounded.
   - **Azure Advisor** + Azure **reservation-recommendation** (Consumption) engine.
   - Actual cost via **Cost Management** — last complete month per resource + 4-month history +
     per-service spend and billing currency.
   - Live pricing via the public **Azure Retail Prices API**, fetched in the billing currency, cached
     24h with last-known-good fallback.
4. The findings engine grounds, validates, dedupes, and scores every opportunity.
5. Frontend polls until complete, then shows the dashboard.
6. Download the branded PDF / Excel report.

---

## Architecture

```
frontend/          React + Vite + MUI + Recharts (light, Turbo360-style UI; currency-aware)
backend/           FastAPI + SQLAlchemy + SQLite
  app/
    api/           REST routes (subscriptions, assessments, report download), auth deps
    services/
      azure_client.py     Azure ARM/Consumption/Cost-Management client (retry + circuit breaker)
      inventory.py, kql.py  Resource Graph collection (server-side KQL)
      metrics.py            Azure Monitor CPU/memory (bounded concurrency)
      pricing.py            Retail Prices API (currency-aware, cached, fallbacks)
      cost_management.py    Actual cost (last-month basis), currency, month-over-month consistency
      reservations.py       Parse Azure reservation recommendations
      findings.py           The findings engine (grounding, dedup, scoring, severity)
      currency.py           USD↔billing-currency for thresholds + nominal estimates
      assessment.py         Orchestrator (state machine, background task)
      report.py             Branded PDF (reportlab) + Excel (openpyxl)
    security/        JWKS RS256 token verification, RBAC, rate limiting, audit log
    models/          SQLAlchemy DB models + Pydantic schemas
    database.py      SQLite via SQLAlchemy
  alembic/           Database migrations (001–008)
```

---

## Prerequisites

- Python 3.12+ (3.14 works with pinned package versions)
- Node.js 18+
- An Azure account with permission to create App Registrations

---

## Setup

### 1. Azure App Registration

Create a **Multi-Tenant** App Registration in Azure Entra ID:

| Field | Value |
|-------|-------|
| Name | Azure Cost Assessment Tool |
| Supported account types | Accounts in any organizational directory (Multitenant) |
| Redirect URI (SPA) | `http://localhost:5173` |

**API Permissions (delegated):** `https://management.azure.com/user_impersonation`, `openid`,
`profile`, `email`. Copy the **Application (client) ID**.

For accurate, grounded results the signed-in user needs **Cost Management Reader** (in addition to
Reader) on the assessed subscriptions — that's what unlocks the actual-bill grounding.

### 2. Backend

```bash
cd backend
cp .env.example .env            # set AZURE_CLIENT_ID=<your-client-id>

python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on Linux/macOS)

pip install -r requirements.txt
alembic upgrade head            # apply DB migrations (required after pulling changes)
uvicorn app.main:app --reload --port 8000
```

> **After pulling changes, always run `alembic upgrade head`.** New columns (currency, report
> metadata, etc.) are added via migrations; skipping this causes a 500 on new assessments.

### 3. Frontend

```bash
cd frontend
cp .env.example .env            # set VITE_AZURE_CLIENT_ID=<your-client-id>
npm install
npm run dev                     # http://localhost:5173
```

### 4. Report branding (optional)

Drop the TechPlus Talent logo at `backend/app/assets/tpt_logo.png` to brand the report cover + header
(a text wordmark is used if absent). Client name on the cover is derived automatically from the Azure
tenant display name.

---

## Environment Variables

### backend/.env (key settings)

| Variable | Description |
|----------|-------------|
| `AZURE_CLIENT_ID` | App Registration client ID |
| `DATABASE_URL` | SQLite path (default `sqlite:///./cat.db`) |
| `CORS_ORIGINS` | JSON array of allowed origins |
| `PRICING_CURRENCY` | Default currency when billing currency can't be detected (default `USD`) |
| `RESERVATION_BASIS` | `combined` \| `measured` \| `always_on` \| `advisor` |
| `VERIFY_TOKEN_SIGNATURE` | Keep `true` outside local dev (JWKS signature check / tenant isolation) |
| `AZURE_MAX_RETRIES`, `AZURE_RETRY_BASE_DELAY` | Retry/backoff tuning |

### frontend/.env

| Variable | Description |
|----------|-------------|
| `VITE_AZURE_CLIENT_ID` | Same App Registration client ID |
| `VITE_API_URL` | Backend base URL (default `http://localhost:8000`) |

---

## Authentication

Delegated auth only — no client secrets, no background access. The signed-in user's own Azure token
calls Azure APIs, so the tool only ever sees what that user already has access to. Tenant isolation is
enforced by verifying the token's RS256 signature against Azure AD's JWKS (blocks `alg=none`/HS256
confusion) and scoping every record to the caller's `tid` + `oid`.

### Multi-tenant client onboarding

For clients whose tenants require admin consent, send their IT admin:

```
https://login.microsoftonline.com/<their-tenant>/adminconsent?client_id=<your-client-id>&redirect_uri=http://localhost:5173
```

---

## Production Deployment (Azure App Service)

```bash
cd frontend && npm run build     # outputs to frontend/dist/ (served by FastAPI)
# Startup command: bash startup.sh
# App Settings: AZURE_CLIENT_ID, CORS_ORIGINS=["https://your-app.azurewebsites.net"]
# Run migrations on deploy: alembic upgrade head
```

Add `https://your-app.azurewebsites.net` as a redirect URI in the App Registration.

---

## Testing

```bash
cd backend && python -m pytest -q      # 210 tests: findings, pricing, cost mgmt, reservations,
                                       # currency, security, and a full mocked assessment pipeline
cd frontend && npm run build           # type-check + production build
```

The findings engine, pricing, cost-management, currency, and reservation parsing are all pure and unit-
tested without network; the end-to-end pipeline test mocks every Azure API behind one transport.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, Vite, Material UI v5, Recharts, MSAL React, React Query |
| Backend | FastAPI, Uvicorn, SQLAlchemy, Alembic, httpx |
| Reporting | reportlab (PDF), openpyxl (Excel) |
| Database | SQLite (local), swappable via `DATABASE_URL` |
| Azure APIs | Resource Graph, Monitor, Advisor, Cost Management, Consumption (reservations), Retail Prices |
