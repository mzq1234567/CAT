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

## 4. Deploy to Azure App Service

**Build frontend:**
```bash
cd frontend
npm run build        # outputs to frontend/dist/
```

**Deploy backend + frontend/dist to App Service:**
```bash
cd backend
# The FastAPI app serves frontend/dist as static files automatically
# Set Startup Command: bash startup.sh
# Set App Settings:
#   AZURE_CLIENT_ID = <your-client-id>
#   CORS_ORIGINS = ["https://your-app.azurewebsites.net"]
```

Add `https://your-app.azurewebsites.net` as a redirect URI in the App Registration.

---

## Assessment Flow

1. User signs in → MSAL redirects to Microsoft login
2. User selects subscriptions → clicks **Run Assessment**
3. Backend starts background task; returns assessment ID immediately
4. Frontend polls every 4 s until status = `completed`
5. Results page shows summary cards + filterable findings table
6. User downloads PDF report
