# Option 2 — Service Principal + RBAC authentication (deep dive)

> **What this document is:** a detailed, source-backed investigation of the "service principal + Azure
> RBAC" identity architecture for the Azure Cost Assessment Tool (CAT) — the alternative to the tool's
> current per-user delegated login. It answers 10 specific questions, compares the two approaches, and
> shows what Datadog, CloudHealth, Turbo360 and Wiz actually do. Every claim is backed by a Microsoft
> doc or the named vendor's own docs — nothing is guessed. Written to be read by a human and complete
> enough for an AI (e.g. ChatGPT) to reason about on its own.

---

## Background: what the tool does today (context)

CAT is a read-only Azure cost-assessment web app. Today it uses **delegated authentication**: the user
signs in (MSAL) and the browser hands the backend the **user's own** Azure Resource Manager (ARM) token
(scope `https://management.azure.com/user_impersonation`). The backend calls Azure **as that user** —
7 ARM endpoints (Subscriptions, Tenants, Advisor, Consumption reservations, Resource Graph, Monitor
metrics, Cost Management) plus the anonymous Retail Prices API. It only ever **reads** (GET/query).

**Two problems with that model:**
1. The ARM `user_impersonation` scope isn't classified "low impact," so under Microsoft's default tenant
   policy a normal user can't self-consent → a **tenant admin must approve it once** ("needs admin approval").
2. The token the backend receives **inherits the user's Azure role** — usually **Owner**, i.e. it is
   *write-capable*, even though the tool only reads. A security team will flag a server holding that.

**Option 2 fixes both** by authenticating as a dedicated, read-only **service principal** instead.

---

## The one-paragraph version

The 7 ARM endpoints are authorized by **Azure RBAC**, not by Entra API permissions. So a **service
principal** assigned **Reader + Cost Management Reader** can call all of them with its **own** token
(`https://management.azure.com/.default`, client-credentials) — **no `user_impersonation`, no per-user
token, and no API-permission admin-consent prompt** (the app declares no API permissions; ARM checks
RBAC). The trade-off versus today is a one-time "create SP + assign roles" onboarding step, in exchange
for a **read-only** identity that runs **unattended**.

---

## Two documented variants of Option 2

Both are "SP + RBAC"; they differ in **who owns the app registration**:

- **Model A — single-tenant SP per customer.** The **client** registers an app in **their own** tenant,
  assigns it read roles, and hands you Tenant ID + Client ID + secret. → used by **Datadog, CloudHealth, Turbo360.**
- **Model B — one multi-tenant app + RBAC.** **You** own one app registration; an Enterprise App (SP) is
  instantiated in the **client's** tenant on admin-consent, then RBAC is assigned to it. → used by **Wiz.**

---

## The 10 questions, answered

### 1. Who creates the service principal?
The SP always lives in the **client's** tenant and is the identity that reads their Azure.
- **Model A:** the **client** creates it (registering an app auto-creates the SP; e.g. `az ad sp create-for-rbac`).
- **Model B:** **you** own the app registration; the SP is instantiated in the client tenant via admin
  consent or `az ad sp create --id <appId>`.
Docs: [App & service principal objects](https://learn.microsoft.com/entra/identity-platform/app-objects-and-service-principals) ·
[Create SP from a multitenant app](https://learn.microsoft.com/entra/identity/enterprise-apps/create-service-principal-cross-tenant) ·
[az ad sp create-for-rbac](https://learn.microsoft.com/cli/azure/azure-cli-sp-tutorial-1)

### 2. Does an Enterprise Application get created?
**Yes — in both models.** A service principal *is* an Enterprise Application: *"Microsoft Entra ID
automatically creates an Enterprise Application in each tenant… they're called Enterprise Applications,
but the objects are service principals."* The client always ends up with one Enterprise App (the SP)
representing your tool.
Docs: [Establish applications](https://learn.microsoft.com/entra/architecture/establish-applications#register-applications)

### 3. Does the client need an App Registration?
- **Model A: Yes** — the client creates an App Registration in their tenant (it produces the SP).
- **Model B: No** — the App Registration lives in **your** tenant; the client only gets the Enterprise
  App/SP. *"For multitenant apps, the customer doesn't have access to app registrations that stay in the
  ISV's tenant."*
Docs: [Establish applications](https://learn.microsoft.com/entra/architecture/establish-applications#register-applications)

### 4. Does the client need Global Admin?
**No — never strictly required.** By step:
- Create app registration / SP: **any user by default**; if the tenant restricted it → **Application
  Developer** (or Application / Cloud Application Administrator).
- Instantiate the multi-tenant SP (Model B): **Cloud Application Administrator** or **Application Administrator**.
- Assign the RBAC roles: **Owner** or **User Access Administrator** (an *Azure* role, not a directory role).
Docs: [Restrict who can register apps / App Developer](https://learn.microsoft.com/entra/identity/role-based-access-control/delegate-app-roles#restrict-who-can-create-applications) ·
[How apps are added (default: all users)](https://learn.microsoft.com/entra/identity-platform/how-applications-are-added#who-has-permission-to-add-applications-to-my-microsoft-entra-instance) ·
[Create SP cross-tenant](https://learn.microsoft.com/entra/identity/enterprise-apps/create-service-principal-cross-tenant)

### 5. Does only Subscription Owner suffice?
**For the RBAC assignment — yes.** Subscription **Owner** has `Microsoft.Authorization/roleAssignments/write`,
so it can grant **Reader + Cost Management Reader** to the SP.
**But Owner is an *Azure* role, not a *directory* role.** It doesn't by itself create an app registration
in a locked-down tenant or consent/instantiate a multi-tenant SP (those are Entra directory actions). So:
- **Model A on a default tenant:** Subscription Owner is usually enough (register app + assign role).
- **Model B (or a restricted tenant):** Owner covers the RBAC step; a **directory role** is also needed
  for the SP/consent step.
Docs: [Role-assignment prerequisites](https://learn.microsoft.com/azure/role-based-access-control/role-assignments-steps#step-4-check-your-prerequisites) ·
[Owner as subscription admin](https://learn.microsoft.com/azure/role-based-access-control/role-assignments-portal-subscription-admin)

### 6. Can RBAC alone replace the current delegated flow?
**Yes — completely, for the data access.** All 7 ARM endpoints authorize via Azure RBAC, not Entra API
permissions. An SP with **Reader + Cost Management Reader** calls every one via client-credentials — no
`user_impersonation`, no delegated token, no API-permission consent (the app declares no API permissions;
ARM checks RBAC). `az ad sp create-for-rbac --role reader` grants ARM access *purely* through RBAC. The
Retail Prices API and JWKS are anonymous and unchanged.
Docs: [az ad sp create-for-rbac](https://learn.microsoft.com/cli/azure/ad/sp) ·
[Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/general#reader) ·
[Cost Management Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#cost-management-reader)

### 7. Can this still remain a self-service SaaS?
**Yes — this *is* the industry-standard self-service model.** It adds a one-time setup step versus "just
log in," but it's fully self-serve:
- **Model A:** you give the client a script/wizard; they run `az ad sp create-for-rbac --role "Reader" …`
  (Datadog ships a Cloud Shell **"Quickstart" + Terraform**) and paste Tenant/Client/Secret into your app.
- **Model B:** the client clicks your **admin-consent URL**, then applies a one-click **ARM/Bicep template**
  assigning the roles — often at **management-group** scope (all subscriptions at once).

### 8. Comparison with the current delegated architecture

| | **Current — delegated `user_impersonation`** | **Option 2 — service principal + RBAC** |
|---|---|---|
| Identity | the **user's own** ARM token | a **dedicated SP** for the tool |
| Privilege | inherits the user's role — usually **Owner (write-capable)** | **read-only by construction** (Reader + Cost Management Reader) |
| Consent prompt | ARM scope isn't low-impact → **admin consent** under default policy | **no API-permission consent** (RBAC only); Model B still instantiates an SP |
| Backend holds | a **write-capable** user token (security red flag) | a read-only SP credential (or federated cred / cert) |
| Onboarding | "just sign in" (after one admin consent) | one-time **create SP + assign roles** |
| Secret to manage | none (delegated token, in memory) | Model A: a secret/cert to store & rotate; Model B/federated: can avoid a stored secret |
| Unattended runs | tied to a human session | runs independently of any user |

### 9. Onboarding experience for the client
- **Model A:** admin/Owner runs the script → app + SP created → **Reader + Cost Management Reader** assigned
  at subscription (or management group) → client secret created → paste **Tenant ID + Client ID + Secret**
  into the app. Minutes, scriptable, no per-user login, no consent screen.
- **Model B:** admin clicks **"Grant admin consent"** (instantiates the SP) → applies a provided **RBAC
  template at management-group scope** → connector live across all subscriptions. One admin action + one template.

### 10. Which architecture do Turbo360, Datadog, Wiz, CloudHealth use — and why?
**All four use Option 2 (service principal + Azure RBAC). None uses the delegated per-user ARM token this
tool currently uses.** They split across the two flavors:

- **Datadog → Model A (single-tenant SP).** Customer registers an app in their own tenant, assigns
  **Monitoring Reader**, and provides **Tenant ID + Client ID + Client Secret** (customer must be Owner,
  or Contributor + User Access Admin). — [Datadog: Getting Started with Azure](https://docs.datadoghq.com/getting_started/integrations/azure/)
- **CloudHealth (VMware/Tanzu/Broadcom) → Model A.** Customer creates an **App Registration**, assigns the
  **Reader** role (read-only), hands over credentials. — [CloudHealth: Setting up Azure Account](https://techdocs.broadcom.com/us/en/vmware-tanzu/cloudhealth/tanzu-cloudhealth/saas/tnz-cloudhealth/getting-started-with-tanzu-cloudhealth-azure-quick-start.html)
- **Turbo360 → Model A.** Customer registers an app, creates a **client secret**, and assigns **Reader** at
  subscription level (explicitly required to read cost). — [Turbo360: Permissions for Service Principal](https://docs.turbo360.com/docs/permissions-for-service-principal)
- **Wiz → Model B (multi-tenant app + admin consent + RBAC).** Customer sets **connector scope**
  (Management Group / Subscription), grants **admin consent** to Wiz's app, and RBAC (Reader / Security
  Reader) is assigned — management-group scope covers all subscriptions. — [Connecting Wiz to your Azure tenant](https://www.cordant.au/what-we-believe/connecting-wiz-to-your-microsoft-azure-tenant) ·
  pattern per [MS multi-tenant guidance](https://learn.microsoft.com/entra/architecture/establish-applications)

**Why SP + RBAC over delegated `user_impersonation` (documented properties, not opinion):** an SP is
**read-only by construction**, runs **unattended/continuously** (not tied to a human's session or their
Owner-level token), and keeps a **write-capable user token off the vendor's server**. Cost/monitoring
tools (Datadog, CloudHealth, Turbo360) favor **Model A** (the customer fully owns the identity + secret;
nothing of the vendor sits in their directory). A posture scanner (Wiz) favors **Model B** (one
vendor-managed app + management-group-scoped RBAC covers the whole estate in one admin step). Microsoft
itself recommends the **multi-tenant** flavor for ISVs over single-tenant-per-customer, though both are valid.

---

## Verdict for this tool

An SP with **Reader + Cost Management Reader** replaces `user_impersonation` **entirely** for all 7 ARM
endpoints, removes the admin-consent prompt on the API-permission axis, and removes the
write-capable-token concern — at the cost of a one-time SP+RBAC onboarding step.
- **Closest fit to the current multi-tenant SaaS shape:** **Model B** (multi-tenant app + management-group
  RBAC, à la Wiz).
- **Client owns everything, no vendor app in their directory:** **Model A** (à la Datadog).

The Retail Prices API and JWKS verification are unaffected — they never used a token in the first place.

---

## Sources

**Microsoft:**
[App vs service principal](https://learn.microsoft.com/entra/identity-platform/app-objects-and-service-principals) ·
[Establish applications (ISV guidance)](https://learn.microsoft.com/entra/architecture/establish-applications) ·
[Create SP from a multitenant app](https://learn.microsoft.com/entra/identity/enterprise-apps/create-service-principal-cross-tenant) ·
[az ad sp create-for-rbac](https://learn.microsoft.com/cli/azure/azure-cli-sp-tutorial-1) ·
[az ad sp reference](https://learn.microsoft.com/cli/azure/ad/sp) ·
[Restrict who can register apps](https://learn.microsoft.com/entra/identity/role-based-access-control/delegate-app-roles) ·
[How applications are added](https://learn.microsoft.com/entra/identity-platform/how-applications-are-added) ·
[Role-assignment prerequisites](https://learn.microsoft.com/azure/role-based-access-control/role-assignments-steps) ·
[Owner as subscription admin](https://learn.microsoft.com/azure/role-based-access-control/role-assignments-portal-subscription-admin) ·
[Reader role](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/general#reader) ·
[Cost Management Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#cost-management-reader)

**Vendors:**
[Datadog Azure](https://docs.datadoghq.com/getting_started/integrations/azure/) ·
[CloudHealth Azure](https://techdocs.broadcom.com/us/en/vmware-tanzu/cloudhealth/tanzu-cloudhealth/saas/tnz-cloudhealth/getting-started-with-tanzu-cloudhealth-azure-quick-start.html) ·
[Turbo360 SP permissions](https://docs.turbo360.com/docs/permissions-for-service-principal) ·
[Wiz onboarding writeup](https://www.cordant.au/what-we-believe/connecting-wiz-to-your-microsoft-azure-tenant)
