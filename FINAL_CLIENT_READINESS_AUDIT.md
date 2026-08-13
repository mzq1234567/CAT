# Final Client-Readiness Audit

**System:** TPT Azure Cost Assessment (Azure CAT). **Date:** 2026-08. **Purpose:** a single, honest
verdict on whether the tool is ready to put in front of a paying client, backed by adversarial testing of
the scenarios most likely to expose a cost-assessment tool — and a P0/P1/P2 classification of anything left.

**Verdict: READY for supervised client demos and pilot engagements**, subject to the three P0 deployment
gates in §3 being confirmed in the target environment. There are **no open P0 code defects**. The
financial-integrity and reliability safeguards built in earlier work are intact and, where this program
touched them, strengthened. Full backend suite: **350 passing**; frontend typecheck + production build clean.

This audit is the capstone of an 8-phase program:

| Phase | Area | Key artefacts |
|---|---|---|
| A | Auth security hardening | audience/issuer/appid/tenant/delegated enforcement; `sessionStorage`; isolation tests |
| B | Self-service permission preflight | `GET /preflight`; readiness UI; Reader-sufficient messaging |
| C | Assessment state + evidence transparency | `finding_basis`; quantified-vs-review split; COMPLETE/PARTIAL/FAILED status |
| D | Recommendation conflict/overlap | `flag_reservation_rightsizing_overlaps`; `RECOMMENDATION_CONFLICTS_AUDIT.md` |
| E | Environment matrix + performance | `ENVIRONMENT_VALIDATION_MATRIX.md`; `test_performance` (linear to 3,000 resources) |
| F | PDF/report quality | completeness + review + calc-basis disclosures in the client report |
| G | Security readiness | `SECURITY_READINESS_AUDIT.md` |
| H | Client error handling + this audit | `errorMessage()` mapper; `ErrorBoundary`; this document |

---

## 1. Adversarial scenario testing

Each row: what an adversary/edge environment does → what the tool does → verdict. "Guarded by" is the
mechanism/test that makes the behaviour non-accidental. **The through-line: no fabricated number, ever.**

### Financial integrity (the cardinal rule)

| # | Scenario | Tool behaviour | Verdict | Guarded by |
|---|---|---|---|---|
| 1 | **Empty subscription** | No findings; "well-optimized" only when data was complete | ✅ PASS | dashboard empty-branch guard |
| 2 | **No billing access** | Spend shown as "Awaiting billing data" (never 0); no fabricated spend | ✅ PASS | `cost_data_available`; ExecutiveSummary |
| 3 | **Sponsored/credited sub (≈0 cost)** | Flat-rate → REVIEW (reference price only); VMs → anomaly, saving withheld | ✅ PASS | `SPONSORED_COST_FRACTION`; `test_financial_integrity` |
| 4 | **New sub, partial billing month** | Spend labelled "estimated run rate" + days billed; <14-day annualisation → REVIEW | ✅ PASS | `spend_estimated`; `MIN_BILLING_DAYS_FOR_ANNUAL` |
| 5 | **No metrics available** | No fabricated utilisation; `vm_metrics_unavailable` REVIEW finding | ✅ PASS | `_metrics_unavailable_review` |
| 6 | **Bastion / orphan, no billed cost** | "Not quantified" + clearly-labelled reference list price; never a saving | ✅ PASS | `_grounded_or_review`; `test_financial_integrity` |
| 7 | **Savings exceed measured spend** | Projected spend withheld + honest caveat; PDF suppresses projection charts | ✅ PASS | ExecutiveSummary reconcile gate; `test_report` |
| 8 | **Non-USD billing currency** | Currency-normalised severity bands; figures in billing currency | ✅ PASS | `severity_from_savings`; `test_findings` |

### Recommendation conflicts

| # | Scenario | Tool behaviour | Verdict | Guarded by |
|---|---|---|---|---|
| 9 | **RI + right-sizing on same VM** | Counted once (larger wins); both displayed; disclosure note | ✅ PASS | `resolve_overlaps`; `test_financial_integrity` |
| 10 | **Reserved capacity + right-sizing (SQL/disk)** | Disclosed as "upper bound, not a sum"; never silent double-count, never fabricated de-overlap | ✅ PASS | `flag_reservation_rightsizing_overlaps` |
| 11 | **AHB applicable** | Shown as "potential"; excluded from headline total | ✅ PASS | `CONDITIONAL_CATEGORIES` |
| 12 | **Two findings on one resource** | Deduped to the larger | ✅ PASS | `_dedupe` |

### Security & isolation

| # | Scenario | Tool behaviour | Verdict | Guarded by |
|---|---|---|---|---|
| 13 | **Same subscription-id in two tenants** | Isolated; one tenant can't see the other's assessment | ✅ PASS | `_owned_assessment`; `test_security_pentest` |
| 14 | **Token minted for another application** | 401 | ✅ PASS | app-id check; `test_security_pentest` |
| 15 | **App-only (non-delegated) token** | 401 | ✅ PASS | `require_delegated`; `test_token_auth` |
| 16 | **Guessed subscription id** | 403 (no Reader access) | ✅ PASS | `verify_subscription_access` |
| 17 | **Guessed assessment id (other tenant/user)** | 404 | ✅ PASS | `_owned_assessment` |
| 18 | **Wrong-audience token (not ARM)** | 401 | ✅ PASS | audience enforcement; `test_security_pentest` |
| 19 | **Assessment-creation spam** | 429 + `Retry-After` | ✅ PASS | `enforce_assessment_rate_limit` |

### Reliability & error handling

| # | Scenario | Tool behaviour | Verdict | Guarded by |
|---|---|---|---|---|
| 20 | **Azure throttling (429)** | Retry + backoff + `Retry-After`; partial-data banner if exhausted | ✅ PASS | `resilience`; `test_azure_stress` |
| 21 | **1000s of resources** | Near-linear (~28 ms for 3,000); no blow-up | ✅ PASS | `test_performance` |
| 22 | **Backend 500 / circuit open** | Friendly message + request-id; no traceback to client | ✅ PASS | `errors.py`; frontend `errorMessage()` |
| 23 | **PARTIAL/FAILED run** | Explicit status chip + banner (web) and scope note (PDF); never presents as complete | ✅ PASS | `data_quality`; PDF `_completeness_note` |
| 24 | **Frontend render crash** | Calm branded fallback + reload; no white screen / React overlay | ✅ PASS | `ErrorBoundary` |

**Result: 24/24 scenarios behave correctly.** No scenario produced a fabricated financial figure, a silent
zero, a raw internal error, or a cross-tenant leak.

---

## 2. Issue classification

### P0 — open code defects
**None.** (The P0 items below are deployment configuration, not code.)

### P0 — deployment gates (must confirm before client exposure)
Carried from `SECURITY_READINESS_AUDIT.md` §3:
1. `VERIFY_TOKEN_SIGNATURE=true`
2. `AZURE_CLIENT_ID` set (enables the app-id check)
3. `CORS_ORIGINS` locked to the exact production origin

### P1 — before broad production
1. Per-process rate-limiter + circuit-breaker → shared store (Redis) for multi-instance.
2. Add HTTP security headers (HSTS/CSP/X-Content-Type-Options).
3. Postgres + Alembic migrations for concurrent multi-instance production.

### P2 — hardening / disclosed residuals
1. Non-VM reserve-vs-right-size overlap is a **disclosed upper bound**, not a netted figure (by design —
   Azure exposes no resource-level reservation attribution; see `RECOMMENDATION_CONFLICTS_AUDIT.md` §4).
2. `debug_findings_reasoning` off in prod; remove/gate before GA.
3. Durable, append-only audit-log sink for engagements that require it.
4. Frontend bundle is a single ~1.3 MB chunk — code-split for faster first paint (cosmetic).

---

## 3. Go-live checklist

- [ ] P0 deployment gates confirmed (§2): signature-verify on, `azure_client_id` set, CORS locked
- [ ] `DEBUG_FINDINGS_REASONING=false`
- [ ] Backend suite green (`cd backend && python -m pytest -q` → 350 passed)
- [ ] Frontend build green (`cd frontend && npx tsc --noEmit && npx vite build`)
- [ ] A live smoke test against a real tenant: preflight → run → dashboard → PDF, on a subscription
      WITH billing and one WITHOUT, confirming the honest degradation in both
- [ ] TLS + security headers at the edge; production DB provisioned if multi-instance

---

## 4. What makes this tool trustworthy to a client

1. **It never invents a number.** Missing data is disclosed, not zeroed; a list price is never a saving;
   a potential (AHB) saving is never counted as realised; an un-priceable signal is shown as "Not
   quantified", never estimated. This is enforced by the evidence model and verified by the
   financial-integrity suite.
2. **It tells the client when it couldn't see everything.** COMPLETE/PARTIAL/FAILED is stated explicitly
   on the dashboard and in the PDF — a partial run can never masquerade as a finished assessment.
3. **It shows its work.** Every figure carries a plain-English basis ("How this is calculated"), shared
   verbatim between the dashboard and the report.
4. **It respects tenant boundaries and least privilege.** Delegated auth, ARM as the authorization oracle,
   no stored secrets, strict tenant/user isolation, Reader-sufficient permissions.
5. **It fails gracefully.** Throttling is retried; crashes show a calm fallback; clients never see a
   traceback, a 500 body, or a raw Azure/SDK error.

---

## 5. Residual risks (accept, with eyes open)

- The non-VM reserve/right-size overlap (P2-1) is a disclosed upper bound; a client who literally sums a
  reservation AND a right-sizing recommendation for the same resource family would over-count. The UI/PDF
  say so explicitly, and the projected-spend backstop prevents an impossible headline.
- Single-instance assumptions (rate-limit, breaker, SQLite) hold for demos/pilots; multi-instance needs the
  P1 changes.
- Wall-clock at very large scale is dominated by Azure API latency (bounded by concurrency/retry), not the
  engine; a live large-tenant run is the only way to measure the true end-to-end time.
