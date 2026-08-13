# Azure CAT — Project Handoff (read me first)

_Portable cold-start brief for any Claude/dev picking this up on any machine (no chat history needed).
Last updated 2026-08-13._

> The user works across two laptops via this git repo. This file + the four `*_AUDIT.md` docs are the
> source of truth. (A machine-local `~/.claude/.../memory/` may add context but does **not** sync across
> devices — this repo does.)

## What the tool is

**Azure CAT** ("TPT Azure Cost Assessment") is a **multi-tenant SaaS** that analyses a client's Azure
subscriptions for cost-optimisation opportunities and produces a **client-presentable report** (dashboard
+ downloadable PDF). Branding: **TPT (Tech Plus Talent)**. Users sign in with their **own** Azure identity
(delegated, read-only); the tool never creates or deletes anything in their environment.

**Stack:** React 18 + Vite + Material UI (light theme) frontend; FastAPI + SQLAlchemy + SQLite backend;
async `httpx` to Azure; ReportLab PDF. `backend/app/services/` holds the engine (`assessment.py`
orchestrator, `findings.py`, `pricing.py`, `cost_management.py`, `azure_client.py`, `report.py`).

## The overriding rule (never violate)

**The tool must NEVER invent, assume, or fabricate a financial number.** Missing data ≠ zero; failed
pricing ≠ a price; failed billing ≠ zero spend; failed metrics ≠ zero utilisation; partial collection ≠ a
complete assessment. Every saving is grounded in the resource's **actual billed cost**, or it's shown as
**REVIEW / "Not quantified"** (never a guess). This is the product's whole reason for existing.

## How the user directs the work

- In explicit **numbered batches** with detailed specs — follow the active spec literally.
- Often wants an **audit/document first**, reviews it, then authorises implementation.
- **Scope discipline:** don't touch UI redesign, authentication, or unrelated code unless the batch says
  so; don't weaken earlier batches' rules or tests.
- **Always verify** (see commands) and report: _what changed · tests passed · build status · remaining
  risks · answers to their explicit questions._ Never claim success unless tests actually pass.
- Trace real code; don't infer from names.

## Status (done vs pending)

| Area | State | Reference |
|---|---|---|
| **Batch 1 — Financial integrity** | ✅ Done | `FINANCIAL_INTEGRITY_AUDIT.md` |
| **Batch 2 — Azure API reliability** | ✅ Done (+hardening) | `AZURE_DATA_COLLECTION_AUDIT.md` |
| **Authentication** | 🔍 Audited, NOT implemented | `AUTHENTICATION_ARCHITECTURE_AUDIT.md` |
| UI redesign (light theme, drawer, cards) | ✅ Done — don't re-open unless asked | — |

**Batch 1 highlights:** evidence model QUANTIFIED / REVIEW / SUPPRESSED; AHB is conditional and excluded
from headline totals (shown as "potential"); Reserved Instances come only from Azure's engine; RI vs
right-sizing overlap resolved via a non-overlapping `counted_savings`; sponsored-subscription + insufficient
-billing-history guards; PDF matches the web's evidence semantics.

**Batch 2 highlights:** two-level concurrency (per-run `azure_max_concurrency=8` + process-wide
`azure_global_max_concurrency=24`); centralized retry (429/5xx, `Retry-After`, exp backoff+jitter, bounded)
with shared stats; complete pagination; a **COMPLETE / PARTIAL / FAILED** data-quality state (missing data
is never treated as zero) with a concise client banner; a failed-metrics VM becomes REVIEW, never "idle".

**Auth (current):** pure **delegated user-token pass-through** — the browser (MSAL, multi-tenant `common`)
gets an Azure Resource Manager token for the signed-in user; the backend verifies it and reuses it as the
Azure credential. **No service principal with RBAC, no client secret, no app-only access.** Full assessment
needs **Reader + Cost Management Reader** on the subscription. See the auth audit for findings + the
recommended (not-yet-built) hardening.

## Next step (do NOT start without the user's go-ahead)

Implement the reviewed **authentication** recommendation: harden the delegated model (enable audience
enforcement, keep signature verification on, surface the RBAC requirement, add an admin-consent path,
OBO/refresh for long runs); add **Azure Lighthouse** only if unattended/scheduled assessments are needed.

## Verify (from the repo root)

```bash
cd backend && python -m pytest -q          # ~304 tests; MUST cd into backend (cwd resets to repo root)
cd frontend && npx tsc --noEmit && npx vite build
```

- Backend must be **restarted** (uvicorn) to pick up code changes.
- A finding-level change needs a **fresh assessment re-run** (details are stored per finding); client-side
  dashboard aggregates update on browser refresh.
- DB schema uses `create_all` + `database.ensure_runtime_columns()` (idempotent column top-up; no live
  Alembic in dev).

## Deeper detail

`FINANCIAL_INTEGRITY_AUDIT.md`, `AZURE_DATA_COLLECTION_AUDIT.md`, `AUTHENTICATION_ARCHITECTURE_AUDIT.md`
(each has a per-topic matrix, tests, and remaining risks).
