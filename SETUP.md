# Azure Cost Assessment Platform — Setup

## 1. Azure App Registration

Create a **Multi-Tenant** App Registration in Azure Entra ID:

- **Name**: Azure Cost Assessment Tool
- **Supported account types**: Accounts in any organizational directory (Any Azure AD directory - Multitenant)
- **Redirect URI** (SPA): `http://localhost:5173` (add your prod URL later)
- **API permissions** (delegated):
  - `https://management.azure.com/user_impersonation`
  - `openid`, `profile`, `email` (Microsoft Graph)
- No client secret needed — uses delegated auth only.

Copy the **Application (client) ID**.

---

## 2. Backend

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

## 3. Frontend

```bash
cd frontend
cp .env.example .env
# Edit .env — set VITE_AZURE_CLIENT_ID=<your-client-id>

npm install
npm run dev
```

Open http://localhost:5173

---

## 4. Deploy to Azure App Service (CI/CD)

Deployed as ONE Linux App Service (`tpt-azure-cat`, resource group `Ayush_RG`, plan `azure-cat-plan` B1,
Python 3.12) — FastAPI serves `frontend/dist` as static files. **Every push to `feature` deploys** via
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml): type-check + build the frontend, run the
backend tests, zip the repo-root layout (`backend/` + `frontend/dist/` + root `requirements.txt` shim),
deploy with the publish profile, then smoke-test `/api/health`.

**One-time setup (already done for `tpt-azure-cat`; repeat only for a new app):**
```bash
az appservice plan create -n azure-cat-plan -g Ayush_RG -l eastus --is-linux --sku B1
az webapp create -n tpt-azure-cat -g Ayush_RG -p azure-cat-plan --runtime "PYTHON:3.12"
az webapp config appsettings set -n tpt-azure-cat -g Ayush_RG --settings   AZURE_CLIENT_ID=04b07795-8ddb-461a-bbee-02f9e1bf7b46   CORS_ORIGINS='["https://tpt-azure-cat.azurewebsites.net"]'   VERIFY_TOKEN_SIGNATURE=true TOKEN_ENFORCE_AUDIENCE=true DEBUG_FINDINGS_REASONING=false   DATABASE_URL=sqlite:////home/data/cat.db LOG_LEVEL=INFO SCM_DO_BUILD_DURING_DEPLOYMENT=true
az webapp config set -n tpt-azure-cat -g Ayush_RG --startup-file "bash backend/startup.sh" --always-on true
az webapp update -n tpt-azure-cat -g Ayush_RG --https-only true
az webapp config set -n tpt-azure-cat -g Ayush_RG --generic-configurations '{"healthCheckPath": "/api/health"}'
# Enable SCM basic auth (needed by the publish profile), then download the profile:
az resource update -g Ayush_RG --namespace Microsoft.Web --parent sites/tpt-azure-cat   --resource-type basicPublishingCredentialsPolicies -n scm --set properties.allow=true
az webapp deployment list-publishing-profiles -n tpt-azure-cat -g Ayush_RG --xml
```
Paste the XML as the GitHub secret **`AZURE_WEBAPP_PUBLISH_PROFILE`** (repo → Settings → Secrets and
variables → Actions).

**Notes**
- `AZURE_CLIENT_ID` is Microsoft's Azure CLI public client (the device-code flow's default) — no app
  registration or redirect URI is needed. Swap in your own client id if you register one.
- The SQLite DB lives at `/home/data/cat.db` — outside `wwwroot` (wiped on deploy), on App Service's
  persistent `/home` share. Tables are created at startup (`create_all` + `ensure_runtime_columns`).
- Single instance only: login sessions, rate limiter and pricing cache are in-process. Do not scale out
  without moving to Postgres (`DATABASE_URL`) + a shared cache.

---

## Assessment Flow

1. User signs in → MSAL redirects to Microsoft login
2. User selects subscriptions → clicks **Run Assessment**
3. Backend starts background task; returns assessment ID immediately
4. Frontend polls every 4 s until status = `completed`
5. Results page shows summary cards + filterable findings table
6. User downloads PDF report
