# Authentication Architecture Audit — Azure Cost Assessment Tool

_Audit only. No code, app registrations, Azure permissions, or UI were changed. Every claim below is
traced to the actual implementation (file:line), not inferred from names._

---

## 0. One-line summary

The tool uses **pure delegated user auth**: the browser signs the user in with MSAL against the Entra
**`common`** (multi-tenant) endpoint and obtains an **Azure Resource Manager access token** for that user;
the backend **verifies that token and re-uses it verbatim** as the Azure credential. There is **no service
principal with Azure roles, no client secret, no managed identity, and no app-only access**. The tool can
only see and assess what the **signed-in user already has RBAC on**.

---

## 1. Current authentication flow (traced)

| Step | Where | What actually happens |
|---|---|---|
| Browser SPA | `frontend/src/main.tsx`, `App.tsx`, `@azure/msal-react` | React SPA; MSAL provider wraps the app. |
| Login | `frontend/src/auth/msalConfig.ts:3-19`, `pages/Login.tsx` | MSAL popup/redirect to `https://login.microsoftonline.com/common` (multi-tenant). `clientId = VITE_AZURE_CLIENT_ID` (the TPT app registration — a **public SPA client**). Login scopes: `openid profile email https://management.azure.com/user_impersonation`. |
| Token acquisition | `frontend/src/services/api.ts:12-25` | `instance.acquireTokenSilent(armTokenRequest(account))` (falls back to `acquireTokenPopup` on `InteractionRequiredAuthError`) for scope `https://management.azure.com/user_impersonation` → an **ARM-audience delegated access token** for the user's own tenant. |
| Token attached | `api.ts:27-29` | Every API call sends `Authorization: Bearer <ARM access token>`. |
| Backend auth | `backend/app/api/dependencies.py:38-61` → `security/token.py:81-115` | `get_current_user` verifies the token's **RS256 signature** against Microsoft's `common` JWKS (expiry checked; audience optional), then extracts `oid`/`tid`/`upn`. Returns `{token, user_id, tenant_id, email}` — **the raw token is carried forward**. |
| Azure credential creation | `api/routes/assessments.py:58`, `subscriptions.py:13`, `security/rbac.py:18-31` | `AzureClient(user["token"])` — the **user's token becomes the Azure credential**. RBAC check: `verify_subscription_access` lists the caller's subscriptions and 403s any requested sub the user can't see. |
| Azure API calls | `services/azure_client.py` (`_send` → `httpx` with `Authorization: Bearer <token>`) | All calls to `https://management.azure.com` (Resource Graph, Cost Management, Monitor metrics, Advisor, Consumption) use the **same delegated token**. Pricing goes to `https://prices.azure.com` (anonymous). |
| Async run | `assessments.py:77` → `services/assessment.py run_assessment(id, subs, token)` | The token is passed to the background task **in memory** and used for the whole run. **Not persisted, not logged** (audit stores only `oid/tid/email/subscription_ids`; `resilience.py` never logs headers). |

**Reads of stored data** (`get_assessment`, findings, report) authorize on the token's `oid`+`tid` vs the
stored `user_id`+`tenant_id` (`assessments.py:35-44 _owned_assessment`) — they never call Azure.

---

## 2. Current app-registration model

**Neither Option A nor Option B.** It is a **single multi-tenant, public-client (SPA) app registration
with delegated permissions**. Evidence:
- `msalConfig.ts:7` `authority: .../common` → any Entra tenant can sign in.
- `msalConfig.ts:5` `clientId` only; **no secret anywhere** in frontend or backend (public client).
- `msalConfig.ts:18` delegated scope `management.azure.com/user_impersonation` (delegated, not app role).
- Backend never creates or authenticates as a service principal.

The "service principal in the customer tenant" that exists is only the **enterprise application (SP object)
that Entra auto-creates when a user/admin first consents** to the multi-tenant app. It holds **no Azure
RBAC** and performs no access — it is purely the OAuth consent/identity record. All Azure access is the
**user's**, via the delegated token.

---

## 3. Azure credential type

- **OAuth 2.0 Authorization Code + PKCE** (MSAL.js public client) → a **delegated user access token** for
  the `https://management.azure.com` resource (`user_impersonation`).
- The backend does **token pass-through / forwarding**, not a true On-Behalf-Of exchange: it reuses the
  exact ARM token the browser already obtained (`AzureClient(user["token"])`). There is **no confidential
  client, no client-credentials grant, no managed identity, no `DefaultAzureCredential`, no Azure CLI
  credential**.
- **SDK/classes:** frontend `@azure/msal-browser` + `@azure/msal-react`. Backend uses **raw `httpx`** with
  a `Bearer` header (`services/azure_client.py`) — no Azure SDK credential class. Token verification uses
  `PyJWT` + JWKS (`security/token.py`).

In plain terms: *the user logs into Azure in their browser, the browser gets an Azure key that only opens
the doors that user is already allowed through, and the backend borrows that exact key for the assessment.*

---

## 4. Token / tenant context

- **Login token issuer:** the **user's own tenant** (`common` endpoint; the token's `tid` = that tenant).
- **Azure API calls target:** `management.azure.com` with the user's token → they operate in the **user's
  tenant/subscriptions**. There is **no TPT credential in the ARM path**, so the tool **cannot accidentally
  use the TPT tenant** — the only TPT identity involved is the app *client_id* used to request the token;
  the token itself belongs to the user.
- **Customer tenant ID:** server-derived from the **verified token's `tid` claim** (`dependencies.py:28`),
  stored on the assessment (`assessments.py:64`). **Not taken from the frontend body.**
- **Customer subscription IDs:** supplied in the request body (`AssessmentCreate.subscription_ids`) but
  **verified server-side** against the user's accessible subscriptions (`rbac.py:23-31`) — a guessed/foreign
  sub is 403'd.

---

## 5. Required permissions (from the actual code paths)

**Identity / login (delegated, Entra):**
- `openid`, `profile`, `email` — sign-in + basic profile (`msalConfig.ts:18`).
- `https://management.azure.com/user_impersonation` — the single delegated scope that lets the user's token
  call ARM. Everything below rides on this one scope + the user's Azure RBAC.

**Azure RBAC actually exercised (per subscription):**

| Azure service | Code | Permission / action | R/W | Why | Covered by **Reader**? |
|---|---|---|---|---|---|
| Subscriptions list | `get_subscriptions` | `Microsoft.Resources/subscriptions/read` | read | RBAC gate + enumerate | ✅ |
| Resource Graph | `query_resource_graph` (inventory.py, kql.py) | `Microsoft.ResourceGraph/resources` (read) | read | resource inventory | ✅ |
| ARM resource reads | inventory buckets | `*/read` on resource types | read | disks/IPs/SQL/etc. state | ✅ |
| Monitor metrics | `get_metric` (metrics.py) | `Microsoft.Insights/metrics/read` | read | CPU/mem/IOPS utilisation | ✅ (also Monitoring Reader) |
| Advisor | `get_advisor_cost_recommendations` | `Microsoft.Advisor/recommendations/read` | read | Azure's own cost recs | ✅ |
| Consumption reservations | `get_reservation_recommendations` | `Microsoft.Consumption/reservationRecommendations/read` | read | authoritative RI recs | ✅ (read) |
| **Cost Management query** | `query_cost_management` (cost_management.py) | **`Microsoft.CostManagement/query/action`** | **action** | per-resource + per-service billed cost | ❌ **needs Cost Management Reader** |
| Pricing | `PricingEngine.query` (pricing.py) | none — **anonymous** `prices.azure.com` | read | live retail list prices | n/a |

**Key finding:** the built-in **Reader** role (`*/read`) covers inventory, metrics, Advisor and the
Consumption *read*, but **does NOT cover Cost Management `query/action`** — that requires **Cost Management
Reader** (or Billing Reader/Contributor). So the true minimum is **Reader + Cost Management Reader**. With
Reader only, the billing layer 403s and the tool degrades safely (Batch 1/2: REVIEW / PARTIAL, never a
fabricated number) — see §6.

**No write permissions are required anywhere** — the tool only reads.

---

## 6. Customer admin-consent behaviour (current)

- **Delegated consent:** `user_impersonation` for ARM is **user-consentable by default**, so in tenants
  that allow user consent, a **normal user consents for themselves** at first login — no admin needed. In
  tenants that **restrict user consent** (common in enterprises), **admin consent is required** for the
  whole app before anyone can sign in successfully.
- **Customer IS an admin:** can consent for the org (creating the enterprise app/SP) and proceed.
- **Customer is NOT an admin, user consent allowed:** consents for themselves, proceeds.
- **Customer is NOT an admin, user consent blocked:** MSAL returns a "consent required / admin approval
  needed" error in the browser; the backend never receives a token → no assessment. **There is no in-tool
  admin-consent request flow** today.
- **Consent denied:** MSAL error in the SPA; no token issued; no assessment. (Handled by MSAL, not the tool.)
- **Required RBAC role missing:** the token is valid but `get_subscriptions()` returns fewer/no subs →
  `verify_subscription_access` returns **403 with the list of inaccessible subscriptions** (`rbac.py:28-31`)
  — a **useful, specific error**. For **Cost Management** specifically, the sub is visible (Reader) but the
  cost query 403s → the run **partially completes** and is flagged **PARTIAL / degraded-billing** with a
  clean banner (Batch 2), never a wrong number.
- **Partial run:** yes — the whole Batch 1/2 machinery means missing cost/metrics/inventory degrades to
  REVIEW/PARTIAL rather than failing or fabricating.

---

## 7. Service principal behaviour

- **Is an SP created in the customer tenant?** Only the **enterprise application (SP object)** that Entra
  auto-creates on **first consent** to the multi-tenant app. The **tool never creates one**.
- **Automatically?** Yes — by Entra's consent process, not by the tool.
- **Who creates it / owns it?** Entra, as part of user/admin consent; it represents the TPT app in the
  customer tenant.
- **What Azure RBAC does it receive?** **None.** It is a consent/identity artifact only.
- **Any automatic subscription permissions?** **No.**
- **Where is RBAC granted?** It isn't granted to any SP — **all Azure access is the signed-in user's own
  RBAC** (Reader / Cost Management Reader), carried by the delegated token. There is no app-only path.

---

## 8. Multi-tenant isolation

**Supported.** TPT tenant + Customer A/B/C can use the tool without mixing identities or subscriptions:
- **Per-assessment tenant/user context is stored:** `Assessment.tenant_id` (from token `tid`) and
  `user_id` (`oid`) are persisted (`assessments.py:61-69`; `models/db.py`). All reads filter by BOTH
  (`list_assessments:88-91`, `_owned_assessment:38-42`).
- **No global/static tenant variable** in the request path. Module-level globals are all **identity-free**:
  the JWKS verifier cache (public keys), the pricing cache (public prices), the rate-limiter (keyed by
  `tenant:user`), and the Batch-2 concurrency semaphore (a rate limiter). No tenant is hard-coded.
- **No shared/cached tokens or credentials on the backend:** `AzureClient` is constructed **per request /
  per run** with that caller's token; the token is not cached, pooled, or persisted.
- **Findings / InventoryItem** rows don't carry `tenant_id` directly, but are reachable only via their
  parent `Assessment` (FK), and every access goes through `_owned_assessment` (tenant+user checked) — so
  there is no cross-tenant read path. `AuditLog` **does** carry `tenant_id`.
- **Subscription→tenant association:** subs are validated against the caller's accessible subs at creation
  (same tenant as the token), so they're implicitly tenant-scoped; the DB does not separately re-bind each
  stored sub id to the tenant after the fact (minor — noted in §9).

---

## 9. Security findings

| Check | Finding |
|---|---|
| Access tokens stored (backend)? | **No.** In-memory for the request/run only; never written to DB; never logged (verified: no token in logs/DB; `resilience.py` logs labels, not headers). |
| Access token in browser? | **Yes, by design** (delegated SPA): the user's ARM token lives in MSAL's **`localStorage`** (`msalConfig.ts:12`). Standard SPA tradeoff; **XSS-exposed** — a hardening consideration, not a defect. |
| Refresh tokens stored? | Backend: **none**. Browser: MSAL manages an SPA refresh token in `localStorage` (its normal behaviour). |
| Client secret in frontend? | **No** — public client, no secret exists anywhere. ✅ |
| Azure creds exposed to browser? | The **user's own** ARM token is in the browser (inherent to delegated SPA). No TPT/app secret is ever exposed. |
| Tenant ID trusted from frontend? | **No** — derived from the **verified token `tid`** server-side. ✅ |
| Subscription IDs verified server-side? | **Yes** — `verify_subscription_access` against the caller's real RBAC. ✅ |
| Cross-customer context leak? | **Low risk** — per-request tokens, tenant+user-filtered reads, no shared credential cache. |
| **Signature verification** | `verify_token_signature` defaults **True** (`config.py:22`). If disabled in prod, read endpoints trust `oid/tid` from an **unverified** token → **tenant-isolation bypass** (the `token.py` docstring flags this). Must stay ON in production. |
| **Audience enforcement** | `token_enforce_audience` defaults **False** (`config.py:23`). The backend accepts any RS256 Entra token, not only ARM-audience ones. ARM endpoints reject wrong-audience tokens anyway, but the **read-only endpoints** (which trust `oid/tid`) would accept a token minted for a *different* resource for the same user. **Recommend enabling audience enforcement.** |
| CORS | `allow_origins = settings.cors_origins` with `allow_credentials=True` (`main.py:53-56`) — must be a strict allowlist in prod (no `*`). |

---

## 10. Self-service scenarios (current behaviour)

- **A. Global Administrator** → login OK → can consent org-wide (or self) → sees all subs they have RBAC on
  → **full assessment** (if Cost Management Reader present; else billing degrades to PARTIAL).
- **B. Normal user, sufficient subscription RBAC, not tenant admin** → login OK **iff** the tenant allows
  user consent (else blocked pending admin) → sees their subs → assessment runs; **cost layer only if they
  also hold Cost Management Reader**, otherwise PARTIAL/degraded (clean banner, no wrong numbers).
- **C. No required Azure permissions** → login OK → `get_subscriptions()` returns nothing they can assess →
  **403 "no Reader access to subscription(s)…"** at create time. No assessment.
- **D. Refuses admin consent** → MSAL "consent required/admin approval" error in the SPA → no token → no
  assessment. **No in-tool admin-consent request flow.**
- **E. Multiple subscriptions** → `GET /subscriptions` lists all Enabled subs the user can see
  (`SelectSubscriptions.tsx`); the user selects one or more; one assessment spans all selected subs
  (verified + processed per-sub). ✅
- **F. Different Entra tenant than TPT** → fully supported: `common` endpoint issues a token in the
  **customer's** tenant; the assessment runs against the **customer's** subscriptions; the enterprise app/SP
  is created in the **customer's** tenant on consent. No TPT-tenant crossover.

**Current self-service limitations:** (1) no admin-consent onboarding flow when user consent is blocked;
(2) the **Cost Management Reader** requirement is neither requested nor surfaced/guided; (3) **token
lifetime (~1h)** vs long multi-sub runs — no backend refresh/OBO, so a long run could hit 401s mid-way;
(4) **no unattended/scheduled** assessments — the user's token is required, so nothing runs without the
user present and signed in; (5) **login-copy mismatch** — `pages/Login.tsx:17,81` promises _"uses your
existing Azure Reader access. No additional permissions required"_, but §5 shows **Cost Management Reader**
is needed for the billed-cost layer; with Reader-only the run silently degrades to PARTIAL. The copy
should be corrected (Reader for inventory/metrics; Cost Management Reader for cost). _(Copy fix only —
not changed here per the no-UI-change instruction.)_

---

## 11. Option 1 vs Option 2

> Reminder: the **current** tool is **neither** — it is delegated user-token pass-through (no SP access).
> Both options below are *changes* that would add standing, non-interactive access to customer environments.

### Option 1 — TPT multi-tenant app → **service principal granted RBAC in the customer tenant** (app-only)
- **Onboarding:** customer **admin consents** to the TPT app, then **assigns Azure RBAC** (Reader + Cost
  Management Reader) to the app's **enterprise SP** on their subscriptions (portal, or an ARM/Bicep
  template / Managed Application).
- **Where the SP lives:** one enterprise SP **per customer tenant** (the multi-tenant app's SP), holding
  the granted roles.
- **Where permissions are granted:** on the **customer's** subscriptions, to that SP.
- **Consent:** **admin consent required** (app permissions / role assignment are admin actions).
- **Auth mechanism:** **client credentials** (app-only) — TPT holds a **client secret or certificate** and
  calls ARM as the app in the customer tenant.
- **Security:** highest blast radius — a leaked TPT secret could reach **every onboarded customer** app-only
  (no user in the loop). Requires strong secret management (Key Vault, rotation), and app-only access is
  "always on."
- **Operational complexity:** medium/high — per-tenant consent + role assignment; credential lifecycle.
- **Self-service suitability:** moderate — enables unattended/scheduled runs, but onboarding needs an admin
  to assign roles; not zero-touch.
- **Limitations:** standing privileged access to customer data; secret is a single high-value target.

### Option 2 — TPT app/SP → **cross-tenant delegated access** (Azure Lighthouse / B2B guest)
- **Azure Lighthouse (the SaaS-grade form):** customer onboards **TPT's tenant** via a **delegation offer**
  (ARM template), granting **specific RBAC** (Reader + Cost Management Reader) on chosen subscriptions to a
  **TPT group/SP**, managed **from TPT's tenant**. TPT identities operate **cross-tenant** without becoming
  guests; the customer sees and can **revoke** the delegation any time.
- **Where the SP lives:** in **TPT's** tenant; it is *projected* into the customer's scope via Lighthouse.
- **Where permissions are granted:** on the customer's subscriptions, via the Lighthouse delegation.
- **Consent:** an **admin** deploys the delegation template (one-time); no per-app enterprise consent dance.
- **Security:** strongest isolation of the "standing access" options — least-privilege, per-customer,
  auditable, **customer-revocable**, no long-lived TPT secret shared across customers (managed identities /
  federated creds possible). Blast radius is bounded by the delegated scope.
- **Operational complexity:** medium — publish/maintain the delegation template; Lighthouse tooling.
- **Self-service suitability:** good for **unattended, multi-customer** assessment from one control plane.
- **Limitations:** Lighthouse covers ARM/RBAC-scoped services (which is exactly what this tool uses); needs
  an admin to accept the delegation; (B2B-guest variant is clunky for SaaS and not recommended).

---

## 12. Recommended architecture for the self-service requirement

The stated requirement — *"client opens tool → signs in → authorizes access → assessment runs against their
subscriptions"* — is an **interactive, per-user** flow, which is **exactly what the current delegated model
already delivers**, with the **least privilege and least onboarding** (no SP, no secret, no standing
access). **Recommendation: keep the delegated user-token model as the default self-service path and harden
it**, rather than adopt Option 1 or 2 for the interactive case.

Adopt Option 1/2 **only** if a second requirement appears — **unattended / scheduled / "run without the
user present"** assessments. For that, prefer **Option 2 (Azure Lighthouse)** over Option 1: it avoids a
single global TPT secret with cross-customer blast radius, is least-privilege and customer-revocable, and
matches the ARM/RBAC surface the tool already uses.

**Net recommendation:** *Default = hardened delegated (current); Opt-in = Azure Lighthouse for unattended
assessments. Avoid Option 1's app-only global-secret model unless specifically required.*

---

## 13. Exact changes that would implement the recommendation (NOT done here)

**A. Harden the current delegated model (small, high-value):**
1. **Enable audience enforcement** — set `token_enforce_audience=True` and keep `token_allowed_audiences`
   to the ARM audiences (`config.py:23-28`) so read endpoints accept only ARM-audience tokens.
2. **Keep `verify_token_signature=True` in every deployed environment** (guard against it being disabled).
3. **Surface the RBAC requirement**: document/show that a full assessment needs **Reader + Cost Management
   Reader**; when the cost layer 403s, add a concise "grant Cost Management Reader to include billed-cost
   savings" hint (reuse the existing PARTIAL banner — no redesign).
4. **Admin-consent onboarding**: add an "IT admin approval" path (the standard Entra **admin-consent URL**
   for the app) for tenants that block user consent, so Scenario D/B becomes self-service.
5. **Token lifetime for long runs**: either (a) keep runs short/chunked, or (b) move the backend to a
   **confidential client doing true On-Behalf-Of** (add a backend app secret/cert + OBO exchange) so it can
   refresh the ARM token mid-run — required only if long multi-sub runs outlive ~1h tokens.
6. **CORS allowlist** locked to the production origin(s).

**B. Add Azure Lighthouse (only if unattended assessments are required):**
1. Publish a **Lighthouse delegation ARM/Bicep template** granting **Reader + Cost Management Reader** on
   selected subscriptions to a **TPT SP/group**.
2. Add a backend **credential path for cross-tenant calls** (a TPT managed identity / app authenticating to
   the delegated scope) alongside the existing per-user `AzureClient` — selected per assessment by
   onboarding type.
3. Persist an **onboarding type** per customer/tenant (delegated vs Lighthouse) and the delegated scope,
   so a run picks the right credential; keep the tenant-isolation checks unchanged.
4. Add a **scheduler** for recurring assessments (only meaningful once non-interactive access exists).

_None of the above has been implemented — this document is for review before any change._
