# Azure Cost Assessment Tool (Azure CAT)

A multi-tenant enterprise SaaS platform that analyses Azure subscriptions for cost optimisation opportunities. Users sign in with their own Azure credentials — the tool reads their resources and surfaces actionable findings with estimated monthly and annual savings.

---

## How it works

1. User signs in via Microsoft (MSAL delegated auth)
2. Selects one or more Azure subscriptions to assess
3. Backend runs a background assessment:
   - Collects resource inventory via Azure Resource Graph
   - Fetches Azure Advisor cost recommendations
   - Evaluates custom inventory-based findings (idle resources, oversized VMs, unattached disks, etc.)
4. Frontend polls every 4 seconds until the assessment completes
5. Results page shows summary cards, findings table with severity + estimated savings
6. User downloads a PDF or Excel report

---

## Architecture

```
frontend/          React + Vite + MUI + Recharts (dark enterprise UI)
backend/           FastAPI + SQLAlchemy + SQLite
  app/
    api/           REST routes (subscriptions, assessments)
    services/      Azure API client, inventory, findings engine, report generator
    models/        SQLAlchemy DB models + Pydantic schemas
    database.py    SQLite via SQLAlchemy
  alembic/         Database migrations
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

**API Permissions (delegated):**
- `https://management.azure.com/user_impersonation`
- `openid`, `profile`, `email`

Copy the **Application (client) ID**.

---

### 2. Backend

```bash
cd backend
cp .env.example .env
# Edit .env — set AZURE_CLIENT_ID=<your-client-id>

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

---

### 3. Frontend

```bash
cd frontend
cp .env.example .env
# Edit .env — set VITE_AZURE_CLIENT_ID=<your-client-id>

npm install
npm run dev
```

Open **http://localhost:5173**

---

## Environment Variables

### backend/.env

| Variable | Description |
|----------|-------------|
| `AZURE_CLIENT_ID` | App Registration client ID |
| `DATABASE_URL` | SQLite path (default: `sqlite:///./cat.db`) |
| `CORS_ORIGINS` | JSON array of allowed origins |

### frontend/.env

| Variable | Description |
|----------|-------------|
| `VITE_AZURE_CLIENT_ID` | Same App Registration client ID |
| `VITE_API_URL` | Backend base URL (default: `http://localhost:8000`) |

---

## Authentication

This tool uses **delegated auth only** — no client secrets, no background access.

- The signed-in user's own Azure token is used to call Azure APIs
- The app can only see what that user already has access to
- Users need **Reader** role on the subscriptions they want assessed
- Nothing is created or deleted in the client's Azure environment

### Multi-tenant client onboarding

For enterprise clients whose tenants require admin consent, send their IT admin:

```
https://login.microsoftonline.com/<their-tenant>/adminconsent?client_id=<your-client-id>&redirect_uri=http://localhost:5173
```

Once approved, all users in that tenant can sign in without prompts.

---

## Production Deployment (Azure App Service)

```bash
# Build frontend
cd frontend
npm run build   # outputs to frontend/dist/

# Deploy backend — FastAPI serves frontend/dist as static files automatically
# Startup command: bash startup.sh
# App Settings:
#   AZURE_CLIENT_ID = <your-client-id>
#   CORS_ORIGINS    = ["https://your-app.azurewebsites.net"]
```

Add `https://your-app.azurewebsites.net` as a redirect URI in the App Registration.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, Vite, Material UI v5, Recharts, MSAL React, React Query |
| Backend | FastAPI, Uvicorn, SQLAlchemy, Alembic, aiohttp, httpx |
| Database | SQLite (local), easily swappable via `DATABASE_URL` |
| Auth | Microsoft MSAL (delegated, multi-tenant) |
| Reports | ReportLab (PDF), openpyxl (Excel) |