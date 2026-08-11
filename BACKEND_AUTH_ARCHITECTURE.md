# Azure CAT — Backend Authentication & API Architecture

> **What this document is:** a complete, verified inventory of every Azure REST API the backend of the
> Azure Cost Assessment Tool (CAT) calls, how it authenticates, what permissions/roles each needs, and
> exactly what forces the "needs admin approval" consent prompt. Every fact is backed by a Microsoft
> documentation link — nothing here is guessed. Written to be readable by a human and complete enough
> for an AI (e.g. ChatGPT) to reason about.

---

## TL;DR (read this first)

- The backend **has no identity or secret of its own.** It simply **borrows the signed-in user's Azure
  token** and calls Azure *as that user*.
- **Almost everything is one Azure service: Azure Resource Manager (ARM)** at `https://management.azure.com`.
  The tool authenticates to ARM with **one delegated permission**: `user_impersonation`.
- **Two exceptions** need no login at all: the **Azure Retail Prices API** (public price list) and the
  **Microsoft public keys (JWKS)** the backend uses to check that the user's token is genuine.
- **There is no Microsoft Graph. There are no "application" (app-only) permissions.** Auth is 100%
  "on behalf of the signed-in user" (delegated).
- **The "needs admin approval" screen is caused by one thing:** the single ARM permission
  `user_impersonation`. Under Microsoft's *default* tenant setting, a normal user isn't allowed to
  approve that permission themselves, so a tenant admin must approve it **once**. After that, every user
  signs in with no prompt, limited by their own Azure role.
- **Everything already works with only the user's delegated token**, provided the user has two Azure
  roles: **Reader** + **Cost Management Reader**.

---

## How the authentication actually works (plain English)

1. The **frontend** (a browser single-page app) signs the user in with Microsoft's login library (MSAL)
   and asks for one Azure scope: `https://management.azure.com/user_impersonation` (plus the basic
   `openid profile email`). MSAL hands back an **Azure Resource Manager access token**.
2. The browser sends that token to the **backend** as an `Authorization: Bearer <token>` header.
3. The backend **verifies the token is real** — it downloads Microsoft's public signing keys (JWKS) and
   checks the token's signature. (This is the only reason it talks to `login.microsoftonline.com`.)
4. The backend then **calls Azure with that same token**, as the user, to read inventory, metrics,
   Advisor, cost, and reservations. It **never writes anything** — only GET/query calls.
5. Separately, it reads the **public Azure price list** (no token needed) to convert usage into money.

The user's token is used **in memory during the run and never stored or logged.**

Reference: [Azure REST API reference — acquiring a token & Bearer usage](https://learn.microsoft.com/rest/api/azure/#create-the-request) ·
[Manage Azure resources by using the REST API](https://learn.microsoft.com/azure/azure-resource-manager/management/manage-resources-rest) ·
[The `management.azure.com/user_impersonation` scope = Azure Resource Manager](https://learn.microsoft.com/entra/msal/msal-acquire-cache-tokens#scopes-when-acquiring-tokens)

---

## The complete endpoint inventory

There are **7 Azure Resource Manager (ARM) endpoints**, **1 public pricing endpoint**, and **1 public
key-verification endpoint**. That's the entire external surface.

For every endpoint below:
- **Auth method** — how the call is authorized.
- **RBAC role** — the Azure role the *user* needs (this is the access to the data).
- **Entra permission** — the app-consent permission the *sign-in* needs (this is what triggers consent).
- **Delegated / Application supported** — can it be called on behalf of a user / as an app identity.
- **Admin consent** — does it force the "needs admin approval" prompt.
- **Delegated-only OK?** — can it work using *only* the signed-in user's token (yes for everything).

---

### 1. List the user's subscriptions
- **What it does:** finds which subscriptions the user can assess.
- **Azure service / family:** Azure Resource Manager (ARM) — Subscriptions
- **Endpoint:** `GET https://management.azure.com/subscriptions?api-version=2022-12-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** any role on the subscription (`Microsoft.Resources/subscriptions/read`; **Reader** has it)
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes (service principal + RBAC)
- **Admin consent required:** Yes — *only because of the shared ARM scope* (see "The consent question")
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Reader role (`*/read`)](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/general#reader)

### 2. Get the tenant (organization) display name
- **What it does:** cosmetic — the client's name on the report cover.
- **Azure service / family:** ARM — Tenants
- **Endpoint:** `GET https://management.azure.com/tenants?api-version=2022-12-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** none (returns tenants the user is a directory member of)
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [ARM REST reference](https://learn.microsoft.com/rest/api/azure/#create-the-request)

### 3. Azure Advisor cost recommendations
- **What it does:** pulls Microsoft's own cost advice for the subscription.
- **Azure service / family:** **Azure Advisor** (called through ARM)
- **Endpoint:** `GET https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01&$filter=Category eq 'Cost'`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** **Reader** (`*/read` includes `Microsoft.Advisor/recommendations/read`); also in Cost Management Reader
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Cost Management Reader includes `Microsoft.Advisor/recommendations/read`](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#cost-management-reader)

### 4. Reservation purchase recommendations
- **What it does:** Azure's own "buy a reservation to save money" recommendations (VMs, SQL, etc.).
- **Azure service / family:** **Consumption** (called through ARM)
- **Endpoint:** `GET https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Consumption/reservationRecommendations?api-version=2023-05-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** **Cost Management Reader** (or **Billing Reader**) — `Microsoft.Consumption/*/read`
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Cost Management Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#cost-management-reader) ·
  [Billing Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#billing-reader) ·
  [reservationRecommendations API](https://learn.microsoft.com/rest/api/consumption/reservationrecommendations/list)

### 5. Resource Graph (the resource inventory)
- **What it does:** the main scan — lists every resource (VMs, disks, IPs, SQL, …) via a KQL query.
- **Azure service / family:** **Azure Resource Graph** (called through ARM)
- **Endpoint:** `POST https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** **Reader** — "at least `read` access to the resources you want to query"
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Permissions in Azure Resource Graph](https://learn.microsoft.com/azure/governance/resource-graph/overview#permissions-in-azure-resource-graph)

### 6. Azure Monitor metrics (utilisation)
- **What it does:** reads CPU/memory/etc. to find idle or oversized resources.
- **Azure service / family:** **Azure Monitor** — Metrics (called through ARM)
- **Endpoint:** `GET https://management.azure.com/{resourceId}/providers/microsoft.insights/metrics?api-version=2023-10-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** **Reader** (or **Monitoring Reader**) — `Microsoft.Insights/metrics/read`
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Metrics need `Microsoft.Insights/metrics/*/read`, granted by Reader/Contributor](https://learn.microsoft.com/azure/azure-monitor/metrics/azure-monitor-workspace-manage-access#azure-rbac) ·
  [Monitoring Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/monitor#monitoring-reader)

### 7. Cost Management query (the actual bill)
- **What it does:** reads real billed cost so savings are grounded in the actual bill, not list price.
- **Azure service / family:** **Cost Management** (called through ARM)
- **Endpoint:** `POST https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version=2023-11-01`
- **Auth method:** delegated user ARM Bearer token
- **RBAC role:** **Cost Management Reader** — Microsoft's documented minimum ("access to a subscription requires at least the Cost Management Reader")
- **Entra permission:** `https://management.azure.com/user_impersonation`
- **Delegated supported:** Yes  |  **Application supported:** Yes
- **Admin consent required:** Yes — shared ARM scope
- **Works with delegated-only token?** ✅ Yes
- **Docs:** [Assign access to Cost Management data](https://learn.microsoft.com/azure/cost-management-billing/costs/assign-access-acm-data#assign-subscription-scope-access) ·
  [Cost Management Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#cost-management-reader)

### 8. Azure Retail Prices (public price list)
- **What it does:** live per-SKU prices, used to turn usage into money and price RI/AHB savings.
- **Azure service / family:** **Azure Retail Prices API** — a standalone public API (NOT ARM)
- **Endpoint:** `GET https://prices.azure.com/api/retail/prices?...`
- **Auth method:** **none — anonymous** ("This API gives you an unauthenticated experience")
- **RBAC role:** none  |  **Entra permission:** none
- **Delegated / Application / Admin consent:** not applicable — no login at all
- **Works with delegated-only token?** ✅ Yes (needs no token)
- **Docs:** [Azure Retail Prices overview](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)

### 9. Token verification (Microsoft public keys) — inbound only
- **What it does:** checks that the user's token is genuine (RS256 signature). This is *inbound* — the
  backend isn't calling Azure on the user's behalf here; it's validating who's calling *it*.
- **Azure service / family:** Microsoft Entra OIDC metadata + **JWKS** (public keys)
- **Endpoint:** `GET https://login.microsoftonline.com/common/.well-known/openid-configuration` → its `jwks_uri`
- **Auth method:** **none — public metadata**
- **RBAC role / Entra permission / Admin consent:** none
- **Works with delegated-only token?** ✅ Yes (public)

---

## Quick matrix

| # | Endpoint | Azure family | RBAC role needed | Delegated | App-only | Admin consent | Delegated-only OK |
|---|----------|--------------|------------------|:---:|:---:|:---:|:---:|
| 1 | /subscriptions | ARM | Reader (any role) | ✅ | ✅ | Yes* | ✅ |
| 2 | /tenants | ARM | none (member) | ✅ | ✅ | Yes* | ✅ |
| 3 | Advisor/recommendations | Advisor | Reader | ✅ | ✅ | Yes* | ✅ |
| 4 | Consumption/reservationRecommendations | Consumption | Cost Management Reader / Billing Reader | ✅ | ✅ | Yes* | ✅ |
| 5 | ResourceGraph/resources | Resource Graph | Reader | ✅ | ✅ | Yes* | ✅ |
| 6 | insights/metrics | Azure Monitor | Reader / Monitoring Reader | ✅ | ✅ | Yes* | ✅ |
| 7 | CostManagement/query | Cost Management | Cost Management Reader | ✅ | ✅ | Yes* | ✅ |
| 8 | prices.azure.com/api/retail/prices | Retail Prices | — | anonymous | anonymous | No | ✅ |
| 9 | login.microsoftonline.com (JWKS) | OIDC verify | — | anonymous | anonymous | No | ✅ |

**\*** The "Yes" is **not per-endpoint.** All seven ARM endpoints share the **same one permission**
(`user_impersonation`). It's that single permission — not any individual API — that triggers the prompt.

**Two roles cover everything:** **Reader** (endpoints 1, 2, 3, 5, 6) + **Cost Management Reader**
(endpoints 4, 7). Without Cost Management Reader the tool still runs but falls back to list-price
estimates instead of the real bill.

---

## The consent question (why the "needs admin approval" screen appears)

**One permission gates all seven ARM endpoints:** `https://management.azure.com/user_impersonation`.
The app requests it once at sign-in; every ARM call rides on it. So the consent prompt is about that
**single scope**, never about a specific API.

**Why it needs admin consent — precisely:** ARM's `user_impersonation` is not permanently hard-flagged
"admin only." The requirement comes from the **target tenant's user-consent policy**:

- **Microsoft's current default** (`microsoft-user-default-low`): *"Allow user consent for apps from
  verified publishers, for selected (low-impact) permissions."* ARM `user_impersonation` is **not**
  low-impact (low-impact means the `openid / profile / email / offline_access / User.Read` class), so a
  regular user **cannot self-consent → a tenant admin must approve it once.** After that, no user is
  prompted again.
- If a tenant uses the older *"Allow user consent for all apps"* policy → users **can** self-consent, no
  admin needed.
- If a tenant *"disables user consent"* → admin consent is **always** required.

So the prompt is a **tenant policy decision**, not something the tool can turn off in code.

Docs: [Configure user consent settings / built-in policies](https://learn.microsoft.com/entra/identity/enterprise-apps/configure-user-consent#configure-user-consent-settings) ·
[Overview of user and admin consent](https://learn.microsoft.com/entra/identity/enterprise-apps/user-admin-consent-overview) ·
[API designers decide admin-consent; tenant admins have final say](https://learn.microsoft.com/security/zero-trust/develop/developer-strategy-delegated-permission#user-and-tenant-administrator-roles-in-permission-and-consent)

**Which endpoints force admin consent:** #1–7 (all ARM), collectively, via the one shared scope.
**#8 (Retail Prices) and #9 (JWKS) force nothing** — no token, no consent, ever.

---

## "Which APIs could be converted to delegated authentication?"

**They are already 100% delegated — there is nothing to convert.** Every call already runs on the
signed-in user's token and already works end-to-end given **Reader + Cost Management Reader**.

The real goal — **removing the admin-consent prompt** — is an *identity-architecture* choice, not a
per-endpoint change. There are exactly three documented paths:

1. **Admin consents once** (or the tenant classifies the ARM permission / relaxes its user-consent
   policy). Zero code. Afterwards every user signs in prompt-free on their own Azure role.
2. **Use an application identity instead of the user's token.** The client grants a **service principal**
   the **Reader + Cost Management Reader** RBAC roles, and the backend authenticates as *that* SP
   (`https://management.azure.com/.default`, client-credentials). ARM uses **RBAC**, not Microsoft-Graph
   app-role consent, so this needs **no directory admin consent at all** — only an RBAC role assignment.
   Bonus: it removes the "the backend is holding a write-capable user token" concern entirely.
3. **Agentless collection.** A **read-only CLI / Cloud Shell script** the client runs in their own Azure
   (using Microsoft's *own* built-in identity + their Reader role) exports a JSON file this tool ingests.
   It creates nothing, needs no app registration, and triggers no consent.

**Bottom line:** the whole admin-consent surface is **one delegated ARM scope** shared by seven read-only
endpoints. The endpoints are already delegated and already sufficient on the user's own token; only the
*consent friction* is left, and that's solved by identity design (options 2–3) or a one-time admin
approval (option 1) — never by changing the APIs themselves.

---

## Appendix — where this lives in the code

| File | What it holds |
|------|---------------|
| `frontend/src/auth/msalConfig.ts` | The one requested scope: `https://management.azure.com/user_impersonation` (+ openid/profile/email) |
| `backend/app/services/azure_client.py` | All 7 ARM calls (subscriptions, tenants, Advisor, Consumption, Resource Graph, Monitor, Cost Management) + API versions |
| `backend/app/services/pricing.py` | The anonymous Retail Prices API call |
| `backend/app/services/cost_management.py` | Cost Management query bodies (last-month / daily run-rate) |
| `backend/app/security/token.py` | Inbound token verification via Microsoft JWKS (RS256) |
| `backend/app/config.py` | Accepted token audiences (`https://management.azure.com/`, `…core.windows.net/`) |

**API versions in use:** Subscriptions/Tenants `2022-12-01` · Advisor `2023-01-01` · Resource Graph
`2021-03-01` · Monitor metrics `2023-10-01` · Cost Management `2023-11-01` · Consumption `2023-05-01`.

**Verified:** no Microsoft Graph calls anywhere in the backend; no application (app-only) permissions;
all Azure data access is delegated ARM; only reads (GET/query), never writes.
