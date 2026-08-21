# Azure CAT — Project Handoff (read me first)

_Portable cold-start brief for any Claude/dev picking this up on any machine (no chat history needed).
Last updated **2026-08-21**._

> The user works across two laptops via this git repo. **This file is the current source of truth.**
> The four `*_AUDIT.md` docs hold per-topic depth. `memory.md` is an append-only historical
> engineering log from the original upgrade and is now **substantially out of date** — do not trust its
> "Assumptions" / "Pending" sections (they still describe unverified tokens, a static ASP price table
> and a 50%-of-PAYG VM heuristic, none of which exist any more). A machine-local
> `~/.claude/.../memory/` may add context but does **not** sync across devices — this repo does.

---

## 1. What the tool is

**Azure CAT** ("TPT Azure Cost Assessment") is a **multi-tenant SaaS** that analyses a client's Azure
subscriptions for cost-optimisation opportunities and produces a **client-presentable report**
(dashboard + downloadable PDF). Branding: **TPT (Tech Plus Talent)**. Users sign in with their **own**
Azure identity (delegated, read-only); the tool never creates or deletes anything in their environment.

**Stack:** React 18 + Vite + Material UI (light **and dark** themes) frontend; FastAPI + SQLAlchemy +
SQLite backend; async `httpx` to Azure; ReportLab PDF. `backend/app/services/` holds the engine
(`assessment.py` orchestrator, `findings.py`, `pricing.py`, `cost_management.py`, `azure_client.py`,
`reservations.py`, `collection.py`, `report.py`).

## 2. The overriding rule (never violate)

**The tool must NEVER invent, assume, or fabricate a financial number.** Missing data ≠ zero; failed
pricing ≠ a price; failed billing ≠ zero spend; failed metrics ≠ zero utilisation; partial collection ≠
a complete assessment. Every saving is grounded in the resource's **actual billed cost**, or it's shown
as **REVIEW / "Not quantified"** (never a guess). This is the product's whole reason for existing.

## 3. How the user directs the work

- In explicit **numbered batches** with detailed specs — follow the active spec literally.
- Often wants an **audit/document first**, reviews it, then authorises implementation.
- **Scope discipline:** don't touch UI redesign, authentication, or unrelated code unless the batch says
  so; don't weaken earlier batches' rules or tests.
- **Always verify** (see §12) and report: _what changed · tests passed · build status · remaining
  risks · answers to their explicit questions._ Never claim success unless tests actually pass.
- Trace real code; don't infer from names.

---

## 4. Status

| Area | State | Reference |
|---|---|---|
| **Batch 1 — Financial integrity** | ✅ Done | `FINANCIAL_INTEGRITY_AUDIT.md` |
| **Batch 2 — Azure API reliability** | ✅ Done (+hardening) | `AZURE_DATA_COLLECTION_AUDIT.md` |
| **Authentication hardening** | ✅ **IMPLEMENTED** (was "audited only") | `AUTHENTICATION_ARCHITECTURE_AUDIT.md` |
| **Production-readiness program A–H** | ✅ Done | `FINAL_CLIENT_READINESS_AUDIT.md`, `SECURITY_READINESS_AUDIT.md` |
| **Dark mode** | ✅ Done (`0ae9c9d`) | §7 |
| **RI reconciliation / migration isolation** | ✅ Done (`0ae9c9d`) | §6 |
| **Cost Management 429 throttling** | ✅ Bounded budget (Option A) implemented | §9 |
| UI redesign (drawer, cards, themes) | ✅ Done — don't re-open unless asked | — |

**Auth (current, implemented):** pure **delegated user-token pass-through** — the browser (MSAL,
multi-tenant `common`) gets an Azure Resource Manager token for the signed-in user; the backend
verifies it and reuses it as the Azure credential. **No service principal, no client secret, no
app-only access.** `security/token.py` enforces RS256 signature (JWKS), expiry, ARM audience,
Microsoft issuer, issuer↔`tid` match, `appid`/`azp` == our client id, and delegated-only (app-only
tokens rejected). Frontend MSAL cache is **sessionStorage**, not localStorage. Full assessment needs
**Reader**; Cost Management access degrades gracefully rather than blocking (see §5).

**Repo state:** the dark-mode + RI-reconciliation + billing-resilience work was committed on
**2026-08-21** as `0ae9c9d` ("did many things", 40 files, +2,488/−431), which also brought
`test_mixed_pipeline.py`, `test_new_subscription_scenario.py` and `components/themeMode.ts` under
version control. Working tree is clean apart from documentation.

---

## 5. Core domain rules a new session must know

These are load-bearing behaviours that are easy to break without realising.

### 5.1 Current inventory is the source of truth
Azure's **reservation recommendations are historical and usage-based**. They carry no resource IDs and
they **lag real inventory changes by days**. A recommendation therefore proves *past usage*, never that
the resource still exists in the assessed subscription.

Before any reservation recommendation becomes a client-facing finding it is reconciled against the
**current Resource Graph inventory** (`services/reservations.py`):

- `reconcile_vm_recommendations()` — matches within the **same subscription**, by SKU *family* (so Azure
  instance-size flexibility, `D2s_v5` vs `D4s_v5`, isn't mistaken for absence). No current match →
  the recommendation is **dropped**, not shown.
- `reconcile_sql_recommendations()` — same principle plus **vCore-vs-DTU eligibility**: DTU databases
  (Basic/Standard/Premium) cannot be reserved, so only current vCore DBs (GeneralPurpose/
  BusinessCritical/Hyperscale) make a SQL reservation actionable. Region is a **soft** signal: if no DB
  matches the recommendation's region but the subscription does have eligible vCore DBs, the finding is
  kept and the relaxation is logged (`MATCHED_CURRENT_SQL_DB_REGION_RELAXED`) — Azure's region string
  and ARG's `location` legitimately diverge.

Affected-resource lists shown to the client are resolved **only** from current inventory
(`current_resources` → `affected_vms`), never synthesised from the historical recommendation.

### 5.2 Subscription-migration isolation
A VM moved from `old-sub` to `new-sub` can still generate a stale recommendation scoped to `old-sub`.
That recommendation **must never attach** to the same-SKU VM now living in `new-sub`. Reconciliation
matches within the recommendation's own subscription, so it can't. If a subscription has zero current
VMs, **no VM RI finding can be produced at all**. Covered by
`tests/test_new_subscription_scenario.py::test_migrated_vm_stale_old_sub_rec_does_not_attach_in_new_subscription`.

There is a second, historically-live leak path: **Azure Advisor** also returns reservation *purchase*
recommendations, which are subscription-scoped. `findings.advisor_rec_is_subscription_scoped()` skips
them, so reservations exist **only** via the reconciled Consumption path and a finding never uses a
subscription id as its affected resource.

### 5.3 Billing-dependent vs resource-based detectors
This distinction drives everything a new/migrated subscription sees.

| Detector | Needs billed cost? | Behaviour with **no** billing data |
|---|---|---|
| Unattached disks, orphaned IPs, snapshots, empty LBs, NAT gateways, Bastion, idle ASPs | No — ARG state is authoritative | **Still surfaces**, as REVIEW / "Not quantified", with the live retail rate shown as a clearly-labelled *reference price* only |
| Idle / oversized VMs | **Yes** | Skipped entirely (a list-price delta can dwarf real spend) |
| Windows AHB | **Yes** | VM excluded and counted in `excluded_no_billing`; never priced at list |
| SQL DB / MI right-sizing | **Yes** (grounded-only) | Nothing emitted |
| Deallocated VMs | **Yes** (the *disks'* cost) | **Nothing emitted at all** — a VM whose residual disk cost can't be quantified is skipped, and if no VM qualifies the aggregate finding isn't produced |
| Disk SKU right-sizing | Partly | Priced from retail delta, capped at actual cost when known |
| Reserved Instances | No (Azure's own figures) | Only exist if Azure's engine returned a recommendation |
| Advisor cost recs | No (Advisor's own estimate) | Unaffected |

The key asymmetry: **resource-based findings degrade to REVIEW; billing-dependent findings disappear.**
`₹0` on the dashboard therefore means "not yet quantifiable", never "no potential" — the UI says so
explicitly.

### 5.4 New / recently-migrated subscriptions
No complete billing month → `_gather_spend_baseline` estimates a **monthly run rate** from average daily
spend over the *observed billing period* (first billed day → last billed day, from daily-granularity
data, so a subscription that started billing mid-month is measured from that day, not the 1st).
`spend_estimated=1` and `spend_period_days` flag it; the UI shows "Projected · ₹X billed in Nd" rather
than presenting a projection as money already spent. A run-rate saving over fewer than
`MIN_BILLING_DAYS_FOR_ANNUAL = 14` days is downgraded to REVIEW (`insufficient_billing_history`).

**Why an old subscription can show high trailing spend with almost no current inventory:** billing is
**historical**, inventory is **current**. Cost Management reports what the resources cost *while they
were still there*. After a migration out, the last complete calendar month (or the trailing run-rate
window, up to 62 days back) still contains that spend, while Resource Graph correctly shows an almost
empty subscription. This is **not a bug** — the two data sources describe different time frames. It
resolves on its own once a full billing month has elapsed post-migration.

### 5.5 "Awaiting billing data" vs "Billing data temporarily unavailable"
Two very different causes, deliberately distinguished in
`components/dashboard/AssessmentDashboard.tsx` and `ExecutiveSummary.tsx`:

- **Awaiting billing data** — `cost_data_available=0` **and** `data_quality="complete"`. Billing was
  queried successfully; the subscription simply has no history yet (new/migrated). Blue hourglass chip.
  Not a failure, not "fully optimized".
- **Billing data temporarily unavailable** — `cost_data_available=0` **and** `data_quality="partial"`.
  Cost Management was **throttled/errored** this run. Amber chip. Resource-based findings still shown;
  re-run shortly.
- **Billing detail unavailable** — subscription total came back but per-resource detail didn't
  (`billing_detail_unavailable=1`). Ungrounded list-price findings are **withheld** and a re-run banner
  is shown.

Getting these mixed up would either alarm a healthy new-subscription client or silently present a
throttled run as clean.

### 5.6 Data-quality state
`services/collection.py` computes **COMPLETE / PARTIAL / FAILED**. Every collection stage records what
it actually collected vs what failed; missing data is never zero. `failed_sources()` names the exact
missing source in the client banner; `stage_summary()` writes a per-stage ok/partial/failed line to the
backend log on **every** run (not just failures).

`billing_failed_sub_ids` is a **set**, not a counter — `mark_billing_failed()` / `mark_billing_recovered()`
are the only writers, so a subscription whose billing 429s and then succeeds on a later retry is
**removed** from the failed set. The flag reflects the *final* billing outcome, not "a 429 happened at
some point".

**Stale-PARTIAL bug and fix (assessment #127):** a 429 that exhausted its inline retries but was
recovered by the outer cost-map / whole-billing retry used to leave `retry.exhausted > 0` forever,
which pinned the run at PARTIAL and showed a client-facing warning on an otherwise healthy assessment.
Fixed in `assessment.py` by baselining `exhausted_before_billing` and **forgiving only the
billing-phase exhaustions**, and only when billing genuinely recovered (`billing_failed_subs == 0` and
not detail-degraded). Exhaustions from other phases (e.g. throttled reservations) are preserved, and a
real billing failure keeps its exhaustion. Throttle telemetry (`throttled_responses`) is never cleared.

---

## 6. Batch highlights

**Batch 1 (financial integrity):** evidence model QUANTIFIED / REVIEW / SUPPRESSED; AHB is conditional
and excluded from headline totals (shown as "potential"); Reserved Instances come only from Azure's
engine; RI vs right-sizing overlap resolved via a non-overlapping `counted_savings`; sponsored-
subscription + insufficient-billing-history guards; PDF matches the web's evidence semantics.

**Batch 2 (reliability):** two-level concurrency (per-run `azure_max_concurrency=8` + process-wide
`azure_global_max_concurrency=24`); centralized retry with shared stats; complete pagination; the
COMPLETE/PARTIAL/FAILED state; a failed-metrics VM becomes REVIEW, never "idle".

**Program A–H:** A auth hardening · B self-service preflight (`GET /api/assessments/preflight`) ·
C `services/finding_basis.py` transparency (see §10.2) · D reservation↔right-sizing conflict
disclosure · E `ENVIRONMENT_VALIDATION_MATRIX.md` + perf tests · F PDF completeness/scope notes ·
G security readiness audit · H client-safe `services/errors.ts` + `ErrorBoundary.tsx`.

**Landed in `0ae9c9d` (2026-08-21):**
- RI reconciliation for VMs and SQL (§5.1) + the Advisor RI leak fix (§5.2).
- **Per-term RI purity** — 1-year and 3-year totals are summed only over the items Azure actually priced
  for that term. A missing term is **never** back-filled from the other; a single-term recommendation
  renders one row labelled "Recommended reservation", not a fabricated comparison.
- **Deallocated VMs aggregated** into ONE finding (like RI/AHB) instead of one card per VM.
- Billing resilience changes (§9) — these are the ones now under review.
- Verbose diagnostic logging on the reservation path (raw item → category → drop reason).
- A large cosmetic sweep replacing em-dashes with commas across backend + frontend prose (inflates the
  diff; no behaviour change).
- New tests: `test_mixed_pipeline.py`, `test_new_subscription_scenario.py`.

---

## 7. Frontend state

**Dark mode** ships alongside light. Two implementation constraints are load-bearing — do not
"simplify" them:

1. **Theme mode state lives in `App.tsx`**, not in a nested provider. A nested provider cannot re-render
   referentially-stable `children`, which left every `colors.*`-in-`sx` value baked in the previous mode
   after a toggle. `components/themeMode.ts` holds only the context object.
2. **`applyColorScheme()` writes inline styles on `<html>`/`<body>`** and `<CssBaseline key={mode}>` is
   remounted. MUI's emotion global block caches the first (light) insertion and the later dark block
   keeps winning by source order, which left inherited body text light-on-light on dark→light — the
   "washed out" symptom. An inline style outranks the global block in both directions.

`colors` is a **mutable module object** (real hex, so MUI `alpha()` works). Mode persists to
`localStorage` under `cat-theme-mode`; the toggle is in the `Layout` sidebar footer. The dark-ink TPT
logo is inverted via a CSS filter in dark mode.

Other client-facing behaviour already implemented: reversible "exclude from savings" with Undo
snackbar; `ErrorBoundary` fallback instead of a white screen; `errorMessage()` maps any error to one
client-safe sentence (never a traceback); assessment progress screen uses customer language only (never
names Advisor / Resource Graph / Cost Management).

---

## 8. Azure API surfaces used

| Surface | Endpoint | Retry budget | Circuit breaker |
|---|---|---|---|
| Resource Graph | `Microsoft.ResourceGraph/resources` | `azure_max_retries` (6) | Yes |
| Monitor metrics | `microsoft.insights/metrics` | 6 | Yes |
| Advisor | `Microsoft.Advisor/recommendations` | 6 | Yes |
| ARM subscriptions/tenants | `/subscriptions`, `/tenants` | 6 | Yes |
| **Cost Management** | `Microsoft.CostManagement/query` | **12** (`COST_MANAGEMENT_MAX_RETRIES`) | **No** (`use_breaker=False`) |
| **Consumption (reservations)** | `Microsoft.Consumption/reservationRecommendations` | **8** | **No** |
| Retail Prices (public, no auth) | `prices.azure.com` | n/a (cached 24h + last-known-good) | n/a |

Resource Graph issues **21 queries per assessment** (20 inventory buckets + 1 count-by-type summary),
independent of subscription count — an ARG query takes a subscription *list*. The code's own note puts
ARG's tenant throttle at ~15 queries / 5s, so 21 concurrent queries (bounded to 8 in flight) is already
tight. **2 of those 21 buckets are collected for nothing** — see §10.3.

---

## 9. Cost Management throttling — investigated 2026-08-21; **Option A implemented 2026-08-21**

Measured by driving the real `run_assessment` against a mock transport returning HTTP 429 for every
billing request and counting actual outbound HTTP calls. **These are measured numbers, not estimates.**

> **Current state:** the bounded-budget fix (Option A, §9.6) is **implemented and test-pinned**. The
> "before" figures below are retained deliberately — they are the rationale for the budget and the
> regression the tests exist to prevent. Every §9.x number labelled *before* describes the pre-fix
> behaviour; *after* figures are called out inline and in §9.2.

### 9.1 Request flow under continuous 429

```
Assessment (per subscription)
  PASS 1
    A. _gather_cost_and_consistency  -> get_cost_map_and_consistency
         CM query #1  grouping=ResourceId  granularity=Monthly (6 months)   -> 1 + 12 retries = 13 HTTP
         (the month-to-date follow-up is never reached: query #1 raises)
    B. _gather_spend_baseline (complete_months=0 -> run-rate branch)
         CM query #2  grouping=ServiceName granularity=Daily (62 days)      -> 13 HTTP
       ...run-rate returns nothing, so `if not totals:` falls back to
         CM query #3  grouping=ServiceName granularity=None  (last month)   -> 13 HTTP
    (cost-map-only retry is SKIPPED here: it requires service_costs to have succeeded)
  -> billing_failed_subs > 0 and no cost_map and no service_costs
  -> sleep BILLING_RETRY_DELAY_SECONDS (45s)
  PASS 2  (whole-billing retry — repeats the identical queries)
    A. CM query #1 again                                                    -> 13 HTTP
    B. CM query #2 again                                                    -> 13 HTTP
       CM query #3 again                                                    -> 13 HTTP

Separately, in the Advisor phase (Consumption billing plane):
    get_reservation_recommendations
      scope='Single'  + Last30Days   -> 1 + 8 retries = 9 HTTP
      (throttled result is indistinguishable from "no recommendations", so the
       any-scope fallback fires) -> 9 HTTP
```

### 9.1a Healthy-path baseline (what a budget must preserve)

Measured on the **non-throttled** paths — this is the cost the system should normally pay:

| Scenario | CM requests / subscription | Outcome |
|---|---|---|
| Established subscription, stable months | **2** | COMPLETE, spend populated |
| Established subscription, erratic costs | **3** | COMPLETE (the extra query is the month-to-date run-rate) |
| Brand-new subscription (all queries 200-empty) | **5** | COMPLETE, `cost_data_available=0` |
| Subscription total OK, per-resource detail empty | **6** | PARTIAL (`billing_detail_unavailable`), cost-map-only retry fires |

**The healthy cost is 2, not 6.** The "6 logical queries" figure only occurs on failure paths, and the
×13 retry multiplier — not the query count — is the dominant term. 78 vs 2 is a **39× amplification**.

### 9.2 Verified worst-case counts — before vs after Option A

| Metric (per subscription, continuous 429) | Before | **After (current)** |
|---|---|---|
| CM attempts per logical query | 13 (`1 + 12`) | **5** (`1 + COST_MANAGEMENT_MAX_RETRIES=4`) |
| CM logical queries (3 pass-1 + 3 whole-billing retry) | 6 | 6 (unchanged) |
| **Cost Management HTTP requests** | **78** | **30** |
| **Preflight CM HTTP requests** | **13** | **2** |
| **Absolute worst case (preflight + assessment)** | **91** | **32** |
| Consumption HTTP requests (out of scope for Option A) | 18 | 18 |
| Max single honoured `Retry-After` wait | 180s | **90s** |
| CM critical-path backoff | ~232 min | **~36 min** |

At the 50-subscription cap: CM **3,900 → 1,500**; billing-plane incl. preflight **5,450 → 2,500**.
Scales linearly with subscription count; peak in-flight remains 8/assessment, 24/process.

Confirmed by request-body fingerprinting: each of the 3 distinct query shapes is now hit exactly
**10 times = 2 passes × 5** (was 26 = 2 × 13).

**Healthy paths are unchanged** — re-measured after the fix: still 2 / 3 / 5 / 6 CM requests
(§9.1a). The budget only ever engages on a failure path.

**Whole-billing recovery cost:** when the entire first pass exhausts (15 requests) and Azure then
recovers, the run costs **17 CM requests** total and still reports COMPLETE.

### 9.3 Verified worst-case duration

The 6 CM queries are **sequential within a subscription's chain**, so their backoff waits sum on the
critical path (subscriptions run concurrently, so N subs don't multiply the wall clock).

| Azure's `Retry-After` | Retry sleeps | Critical-path wait |
|---|---|---|
| absent (exp backoff, full jitter, capped 60s) | 88 | ~19.6 min |
| `60` | 88 | ~72 min |
| `≥180` (clamped to `COST_MANAGEMENT_MAX_RETRY_AFTER`) | 88 | **~232 min (3.9 h)** |

Plus the 45s orchestrator sleep, plus up to 90s httpx timeout per attempt if Azure hangs rather than
429s. `run_assessment` is a FastAPI **BackgroundTask with no overall timeout**, so a fully-throttled
assessment can occupy a worker for hours.

### 9.4 Amplification points (all verified)

1. **The whole-billing retry duplicates every query.** Pass 2 re-issues all three CM queries with the
   same 12-retry budget. Confirmed: 26 hits per query shape.
2. **No circuit breaker on the billing plane.** Both CM and Consumption pass `use_breaker=False`. There
   is no fail-fast: query #6 burns its full 13 attempts even though queries #1–#5 just proved the
   service is throttling. This was a deliberate isolation choice (so a busy metrics run can't fail-fast
   the billing query) but it removed the only give-up mechanism.
3. **Throttled-empty is indistinguishable from genuinely-empty**, in two places:
   `_gather_spend_baseline`'s `if not totals:` fallback to service costs, and
   `get_reservation_recommendations`'s `Single` → any-scope retry. Both fire *because* of throttling
   and add a whole extra logical query.
4. **`Retry-After` is honoured with no jitter** — `delay = min(retry_after, ra_cap)`. Concurrent
   subscriptions receiving the same `Retry-After` wake at the same instant. Only the
   exponential-backoff fallback path has jitter. (`_retry_after_seconds` parses **numeric seconds
   only**; an HTTP-date header returns None and falls back to jittered backoff capped at 60s — so the
   180s cap only ever applies to numeric headers.)
5. **Concurrency slots are released during backoff** (documented and intentional for throughput), so
   waiting retries hold no semaphore. **Measured correction:** the resulting burst is still *bounded* —
   peak in-flight CM requests is **8 per assessment** and **24 process-wide**, confirmed with 6
   concurrent assessments. What is unbounded is total request **volume** and total **duration**, not
   the instantaneous burst. Note also that the semaphores bound *concurrency*, not *request rate* —
   there is no token bucket anywhere.
6. **Preflight primes the throttle bucket.** `GET /api/assessments/preflight` calls
   `get_service_costs_and_currency`, which inherits the CM retry envelope: **13 CM requests and up to
   36 minutes** for one subscription, on a *foreground* request. The frontend fires it **concurrently
   per selected subscription** (`useQueries`) with react-query `retry: 1`, and axios has **no client
   timeout configured** — so if an upstream proxy/gateway times the request out, react-query can start
   a *second* 36-minute server-side envelope while the first is still running. (The proxy timeout is
   deployment-specific and not verifiable from this repo; the mechanism is.)
7. **Retries are per-page, not per-query.** `query_cost_management` paginates with `while url:` and each
   page gets a fresh 12-retry budget, so a query that throttles mid-pagination costs
   `pages × 13` requests rather than 13.

### 9.5 Assessment of the current strategy

The direction of the recent changes (more patience, honour `Retry-After` beyond the 60s global cap) is
right for a hard-throttling billing API — but it was applied **per-call** with no run-level budget. The
result is that patience multiplies: 6 sequential queries × 12 retries × up to 180s each. The
configuration is internally consistent and every wait is individually bounded; what is missing is a
**global give-up** for the billing plane. Nothing here is a correctness bug — no number is fabricated,
and the run correctly reports PARTIAL — but the cost of failing is far higher than it needs to be.

Two further facts sharpen this:

- **`retry_request` has no total-elapsed-time cap** — only per-wait (`MAX_BACKOFF_SECONDS` /
  `retry_after_cap`) and per-count (`max_retries`) bounds. Nothing anywhere bounds the billing phase
  as a whole.
- **No test asserts the whole-billing retry ever recovers a run.** Both tests that reference
  `BILLING_RETRY_DELAY_SECONDS` set it to 0 and assert the *failure* path; the #127 regression test
  exercises the **cost-map-only** retry (`COST_MAP_RETRY_DELAY_SECONDS`), which is a different branch.
  Its recovery value is therefore unproven, while its cost (2× every CM query) is measured.
  Reasoning about when it could help: the inner envelope already spans up to ~36 min per query, so a
  further 45s wait adds nothing when Azure supplies `Retry-After`. It could only help on the
  no-`Retry-After` path, where the inner envelope collapses to roughly 3 minutes.

**Explicitly out of scope for this work (user's instruction):** Redis, per-customer concurrency locking,
queueing, or limiting the site to one customer at a time. Those are future hosting/scaling
considerations and must **not** be recorded or treated as the implemented solution.

### 9.6 Fixes — Option A IMPLEMENTED; Option B still open

Both options preserve the four required behaviours: successful billing → COMPLETE; genuine failure →
PARTIAL; transient 429 that recovers → COMPLETE; no fabricated spend or savings; resource-based
findings still surface without billing.

**Option A — minimal, config-level. ✅ IMPLEMENTED 2026-08-21.** `COST_MANAGEMENT_MAX_RETRIES` 12 → 4;
`COST_MANAGEMENT_MAX_RETRY_AFTER` 180.0 → 90.0; new `COST_MANAGEMENT_PROBE_MAX_RETRIES = 1`; a
keyword-only `max_retries` parameter on `AzureClient.query_cost_management` and
`cost_management.get_service_costs_and_currency`, used by `preflight.run_preflight` so the readiness
probe no longer inherits the collection budget. **No control-flow change** — the whole-billing retry,
cost-map retry, `Retry-After` handling, exponential backoff, jitter, breaker isolation, pagination,
subscription isolation, inventory collection and every detector are untouched.

Measured result: **91 → 32 CM requests/subscription** worst case; **232 → ~36 min** CM backoff;
preflight **13 → 2** requests. Healthy paths unchanged at 2–6.

Pinned by `tests/test_cost_management_budget.py` (9 tests) and two new tests in
`tests/test_preflight.py`. These assert the **measured request count**, not the constants, so adding a
CM query or another whole-billing pass fails the build with the real number.

**Option B — bounded budget + per-subscription latch. NOT implemented, still open.** Deliberately
deferred: Option A was shipped first as an isolated, easily-reverted change. Revisit only if real
throttled-tenant telemetry shows 30 requests/subscription is still too many.

**Option B — bounded budget + per-subscription latch.** Option A, plus: (1) a per-assessment billing
request budget and wall-clock deadline checked inside `query_cost_management`, which on exhaustion
raises a distinct error the gathers treat exactly like a billing failure (`mark_billing_failed`);
(2) a **per-subscription in-pass latch** — once a subscription's CM query exhausts on 429, skip that
subscription's remaining CM queries for the current pass, since queries 2–6 are near-certain to
throttle too; (3) keep exactly **one** whole-billing retry, which clears the latch (preserving the
recovery path); (4) jitter the `Retry-After` wait.
→ **12 CM requests/subscription worst case**, ~13 min bounded by an absolute deadline.
Files: `azure_client.py`, `cost_management.py`, `assessment.py`, `collection.py`, plus tests.
Risk **medium** — the latch changes which branch recovers a throttled-then-recovered run, so
`test_cost_management_exhausts_then_recovers_clears_stale_partial` would recover via the whole-billing
retry rather than the cost-map retry and would need `BILLING_RETRY_DELAY_SECONDS` monkeypatched to 0
(otherwise it sleeps a real 45s). Outcome assertions themselves still hold.

Adjacent (same billing plane, not Cost Management): `get_reservation_recommendations` costs 18
requests/subscription because its `Single` → any-scope fallback cannot distinguish a throttled-empty
result from a genuinely-empty one. Suppressing the fallback when the first attempt was throttled, and
lowering its retry budget, would cut this to ~5.

---

## 10. Known gaps (identified 2026-08-21, none fixed)

### 10.1 Priority
1. ~~Cost Management throttling (§9)~~ — **Option A done 2026-08-21.** Option B remains available but
   is not scheduled; re-evaluate against real throttled-tenant telemetry.
2. Unused inventory buckets (§10.3) — small, and directly reduces §9's remaining blast radius.
3. `basis` not rendered (§10.2) — client-facing value already built and paid for.
4. Dead frontend modules (§10.4) — pure hygiene, zero runtime impact.

Still open on the billing plane but **out of Option A's scope**: `get_reservation_recommendations`
(Consumption) remains 18 requests/subscription because its `Single` → any-scope fallback cannot
distinguish a throttled-empty result from a genuinely-empty one.

### 10.2 `basis` is computed, served, and never displayed
`services/finding_basis.describe_finding_basis()` produces a canonical, client-safe "how this number was
calculated" sentence. It is exposed as a **computed field on `FindingResponse`**, so it is serialised
into every findings API response and reaches the browser. It has a dedicated test file
(`test_finding_basis.py`). But:

- `RecommendationDetails.tsx` (the detail drawer) never reads `finding.basis`.
- `report.py` has **never** imported `finding_basis` — the PDF only carries a single static aggregate
  "How these figures are calculated" paragraph in `report_template.yml`.
- Git history confirms no commit has ever rendered it. It was added in `060252b` and the presentation
  half was never written. **This is unfinished, not intentional hiding.**

Compounding it: the drawer has its **own parallel implementation**, `savingsInfo()`, which derives a
short chiplet ("Cost-validated" / "Azure-calculated" / "Estimated" / "Live pricing" / "Eligibility
required" / "Verify billing") from overlapping `details` fields. So there are two divergent sources of
truth for provenance — the canonical backend sentence (unused) and the frontend chiplet (used). The
client currently loses the specific, per-finding derivation explanation.

### 10.3 Two inventory buckets are collected and never used
Both are registered in `kql.filtered_inventory_queries()` (20 buckets total) and fetched from Resource
Graph on **every** assessment:

- **`sql_ahb_eligible`** — was collected for `detect_sql_ahb()`, which is now **retired** (it returns
  `[]` because the SQL licence component can't be derived from any authoritative Microsoft pricing API).
  `_detect_all` never calls it. Referenced nowhere outside `kql.py`.
- **`geo_redundant_vaults`** — was collected for a `backup_redundancy` (GRS→LRS) finding that was
  **removed in the accuracy audit** (the geo-redundancy premium can't be isolated from the vault's
  total bill without an assumption). `backup_redundancy` survives only in display maps
  (`CATEGORY_DISPLAY`, report `_PILLAR`); no detector emits it. `ORPHAN_RULES` does not include it.

Impact: 2 wasted ARG queries per assessment on the surface with the tightest documented throttle
(~15 queries / 5s per tenant), and — more importantly — **a throttle or error on either bucket flips the
whole run to PARTIAL** and shows the client a "some resource types could not be collected" banner for
data nothing consumes (`_any_partial()` triggers on any non-empty `inventory_failed_buckets`).

Removing them changes no finding, no saving, and no total. The only reason to keep them is if SQL AHB or
backup-redundancy detection is planned for a near-term batch — the KQL builders can stay in `kql.py`
either way; it's only the **registry entry** that causes the fetch. Adjacent observation: **all**
inventory rows are written to `inventory_items` and that table is never read back by any code path.

### 10.4 Dead frontend modules (not "dead imports")
Four modules with **zero importers** anywhere in `src/`:

| File | Lines | Note |
|---|---|---|
| `components/FindingsTable.tsx` | 448 | superseded by the OpportunityGrid/drawer experience |
| `components/SummaryCards.tsx` | 298 | superseded by `ExecutiveSummary`; only referenced in a `theme.ts` comment |
| `components/dashboard/charts/SavingsProjection.tsx` | 178 | |
| `components/dashboard/charts/HBars.tsx` | 49 | |

Verified: none has module-level side effects (pure declarations), and **none is in the production
bundle** — rollup never adds an unimported module to the graph, confirmed by grepping unique string
literals against `dist/assets/index-*.js`. So removing them **cannot** change runtime behaviour or
bundle size. The real cost is that `tsc --noEmit` still type-checks them, so a change to a shared type
(`Finding`, `Severity`, `colors`) can break the build from a file nothing uses. `theme.ts` also retains
a `chartTheme` export solely for the dead `SummaryCards`. Purely cleanup, safe, low value.

---

## 11. Deployment gates (config, not code — confirm before any client exposure)

`VERIFY_TOKEN_SIGNATURE=true` · set `AZURE_CLIENT_ID` (else the `appid` check is skipped with a startup
warning) · lock `CORS_ORIGINS` to the production origin · `DEBUG_FINDINGS_REASONING=false`.

P1 for multi-instance production: shared-state backends for rate limiting / circuit breaker, security
headers, Postgres + Alembic. Azure Lighthouse only if unattended/scheduled runs are ever needed.

---

## 12. Verify (from the repo root)

```bash
cd backend && python -m pytest -q          # 426 passed, ~61s (MUST cd into backend; cwd resets to root)
cd frontend && npx tsc --noEmit && npx vite build   # both clean
```

Last verified 2026-08-21: **426 backend tests pass**, `tsc --noEmit` clean, `vite build` clean
(1.31 MB bundle; the >500 kB chunk warning is pre-existing and expected).

- Backend must be **restarted** (uvicorn) to pick up code changes.
- A finding-level change needs a **fresh assessment re-run** (details are stored per finding);
  client-side dashboard aggregates update on browser refresh.
- DB schema uses `create_all` + `database.ensure_runtime_columns()` (idempotent column top-up; no live
  Alembic in dev).

## 13. Deeper detail

`FINANCIAL_INTEGRITY_AUDIT.md`, `AZURE_DATA_COLLECTION_AUDIT.md`,
`AUTHENTICATION_ARCHITECTURE_AUDIT.md`, `RECOMMENDATION_CONFLICTS_AUDIT.md`,
`SECURITY_READINESS_AUDIT.md`, `FINAL_CLIENT_READINESS_AUDIT.md`,
`ENVIRONMENT_VALIDATION_MATRIX.md`.
