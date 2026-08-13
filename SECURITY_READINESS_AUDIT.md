# Security Readiness Audit

**System:** TPT Azure Cost Assessment — FastAPI + SQLAlchemy backend, React + MSAL frontend, delegated
Azure auth (user-token pass-through to ARM). **Assessment date:** 2026-08. **Method:** control-by-control
review of authentication, authorization, tenant isolation, input handling, secrets, transport, error
handling, abuse controls, and deployment configuration, cross-referenced to the enforcing code + tests.

**Overall posture:** **Strong for a delegated-auth, single-tenant-per-run design.** No stored Azure
secrets, no service principal, no long-lived tokens persisted, no fabricated auth. There are **no open P0
code defects**; the P0 items below are **deployment gates** that must be confirmed before any client-facing
exposure. See §3 for the go-live checklist.

---

## 1. Controls assessed

| # | Control | State | Enforced by |
|---|---|---|---|
| 1 | **Token signature verification** (RS256 vs Entra JWKS) | ✅ On by default; loud startup warning if disabled | `security/token.py`; `dependencies.get_current_user`; `main.py` warning |
| 2 | **Audience enforcement** (token minted for ARM) | ✅ On by default (URL forms + ARM App-ID GUID) | `config.token_allowed_audiences`; `test_security_pentest.test_wrong_audience_token_is_401` |
| 3 | **Issuer + issuer↔tenant match** | ✅ Recognised Microsoft issuers only; `iss` must contain `tid` | `token._verify_claims`; `test_token_auth` (untrusted issuer / tenant mismatch) |
| 4 | **App-id check** (`appid`/`azp` == our client) | ⚠️ On, but **skipped when `azure_client_id` unset** (dev) with warning | `dependencies` warning; `test_security_pentest.test_token_for_other_application_is_401` |
| 5 | **Delegated-only** (reject app-only tokens) | ✅ App-only (roles, no scp) rejected | `token._verify_claims`; `test_token_auth.test_app_only_token_is_rejected` |
| 6 | **Tenant isolation** (assessments scoped to owner tenant+user) | ✅ 404 on cross-tenant/user access | `_owned_assessment`; `test_security_pentest` (same-sub-different-tenant, simultaneous cross-tenant) |
| 7 | **RBAC** (Reader on every requested subscription) | ✅ 403 if not accessible; delegated ARM only returns owned subs | `security/rbac.verify_subscription_access` |
| 8 | **Input validation** (subscription GUIDs, bounded fan-out) | ✅ GUID regex on route + schema; max-subscriptions cap; typed int ids | `schemas.AssessmentCreate`; `routes.preflight` `_GUID_RE`; `max_subscriptions_per_assessment` |
| 9 | **Rate limiting** (assessment creation) | ✅ Sliding window per tenant+user; `Retry-After` | `security/rate_limit`; `enforce_assessment_rate_limit` |
| 10 | **Error handling** (no internal leakage) | ✅ Generic message + request-id; full detail logged only | `errors.py`; `test_security_pentest` / `test_errors` |
| 11 | **Token handling** (never persisted/logged) | ✅ Pass-through only; not stored in DB; not logged; frontend `sessionStorage` | `dependencies._claims_to_user`; `msalConfig` (sessionStorage) |
| 12 | **Secrets** | ✅ None — no client secret / service principal; `azure_client_id` is public | `config.py` (no secret fields) |
| 13 | **Abuse / resilience** | ✅ Bounded per-run + process-wide Azure concurrency; retry caps; circuit breaker | `azure_client.py`; `resilience.py`; `test_azure_stress` |
| 14 | **CORS** | ⚠️ Configurable allow-list, `allow_credentials=True`; **default is localhost only** | `main.py`; `config.cors_origins` |
| 15 | **Preflight** (no permission over-ask, no raw errors) | ✅ Reader-sufficient messaging; client-safe | `services/preflight`; `test_preflight` |

---

## 2. Findings

Severity: **P0** = must fix / confirm before client exposure · **P1** = fix before broad production ·
**P2** = hardening / hygiene.

### P0 — deployment gates (config, not code defects)

| ID | Finding | Required action |
|---|---|---|
| P0-1 | **`verify_token_signature` can be disabled** — if off, tokens aren't signature-checked (tenant-isolation bypass). Secure default is on; a startup warning fires if off. | Confirm `VERIFY_TOKEN_SIGNATURE=true` in every non-local environment. |
| P0-2 | **`azure_client_id` must be set** — unset disables the app-id check (finding #4), so a token minted for another multi-tenant app (with a valid ARM audience) would pass. | Set `AZURE_CLIENT_ID` to the app's client id in production; verify the startup warning is absent. |
| P0-3 | **CORS must be locked to the real origin** — `allow_credentials=True` with a broad/misconfigured `cors_origins` would let other origins call with credentials. | Set `CORS_ORIGINS` to the exact production web origin(s); never `*` (invalid with credentials anyway). |

### P1 — before broad production

| ID | Finding | Recommendation |
|---|---|---|
| P1-1 | **In-memory rate limiter + circuit breaker are per-process.** On a multi-instance deployment, limits are per-instance (N× effective), and breaker state isn't shared. | Move to a shared store (Redis) for multi-instance; the limiter is already written to swap. Acceptable as-is for single-instance demo. |
| P1-2 | **No HTTP security headers** (HSTS, `X-Content-Type-Options`, `X-Frame-Options`/CSP, `Referrer-Policy`). | Add via middleware or the fronting App Service/Front Door. Low effort, meaningful defence-in-depth for the served SPA. |
| P1-3 | **SQLite default DB.** Fine for single-instance demo; not for concurrent multi-instance production (locking, no shared state). | Use Postgres in production; run Alembic migrations (present) rather than `create_all` + `ensure_runtime_columns`. |

### P2 — hardening / hygiene

| ID | Finding | Recommendation |
|---|---|---|
| P2-1 | **`debug_findings_reasoning` / `debug_reason`** dev scaffolding (default off; a TODO to remove/gate exists). | Confirm `DEBUG_FINDINGS_REASONING=false` in prod; remove or gate behind an admin role before GA. |
| P2-2 | **Static SPA mounted at `/`.** Starlette `StaticFiles` is traversal-safe, but it serves whatever is in `frontend/dist`. | Ensure the deployed `dist` contains no source maps / secrets; consider serving the SPA from a CDN/Front Door. |
| P2-3 | **Audit logging** exists for assessment run / finding dismiss / report download; no tamper-evident store. | Ship audit records to a durable, append-only sink for client engagements that require it. |

---

## 3. Go-live security checklist (P0 gates)

- [ ] `VERIFY_TOKEN_SIGNATURE=true` (no signature-verify warning in logs at startup)
- [ ] `AZURE_CLIENT_ID=<app client id>` (no app-id-check-disabled warning at startup)
- [ ] `CORS_ORIGINS=<exact prod web origin>` — not `*`, not localhost
- [ ] `DEBUG_FINDINGS_REASONING=false`
- [ ] Production DB is Postgres with migrations applied (if multi-instance)
- [ ] TLS terminated in front of the app; security headers added at the edge or in middleware
- [ ] Frontend `VITE_AZURE_CLIENT_ID` points at the same app registration

---

## 4. What is explicitly NOT a vulnerability (by design)

- **Delegated user-token pass-through.** The backend forwards the caller's own ARM token; it holds no
  standing credential, so there is no service-principal secret to leak or rotate. This is intentional and
  should **not** be "hardened" into app-only auth (that would broaden blast radius and break self-service).
- **The tool never trusts browser-supplied `tenant_id` / `subscription_id` / `assessment_id`.** Tenant and
  user come from the verified token; subscription access is re-checked against ARM; assessment access is
  scoped by owner. Guessed ids yield 403/404, verified by isolation tests.
- **ARM as the authorization oracle.** Because auth is delegated, ARM only returns resources the caller can
  actually see — used deliberately as the access check (RBAC #7).

---

## 5. Test coverage anchoring this audit

`test_security_pentest.py` (wrong audience, wrong application, cross-tenant isolation, simultaneous
cross-tenant runs), `test_token_auth.py` (issuer, tenant match, missing tid, app-only rejection),
`test_preflight.py` (no permission over-ask, no raw errors), `test_errors` (no internal leakage),
`test_azure_stress.py` (throttling / concurrency), plus the financial-integrity + reliability suites.
Full backend suite: **350 passing**.
