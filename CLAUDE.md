# CLAUDE.md — auto-loaded project context

**What this is:** "Azure CAT" (TPT Azure Cost Assessment) — a multi-tenant SaaS that assesses a client's
Azure subscriptions for cost savings and produces a client-presentable dashboard + PDF report.
Frontend: React + Vite + Material UI (light **and dark** themes). Backend: FastAPI + SQLAlchemy + SQLite,
async `httpx` to Azure. Delegated, read-only Azure auth.

## Read these first (repo-committed, always current)
1. **`PROJECT_HANDOFF.md`** — full cold-start brief: what the tool is, status, how the user works, verify commands.
2. **`FINANCIAL_INTEGRITY_AUDIT.md`** — Batch 1 (evidence model / no-fabrication rules).
3. **`AZURE_DATA_COLLECTION_AUDIT.md`** — Batch 2 (throttling/retry/concurrency/partial-data).
4. **`AUTHENTICATION_ARCHITECTURE_AUDIT.md`** — auth architecture (the hardening it recommends is now IMPLEMENTED).

_Other root `*.md` files (ACCURACY_AUDIT, AHB_*, FABRICATION_AUDIT, FIX_PLAN_*, CODEBASE_BRIEF_*,
BACKEND_AUTH_ARCHITECTURE, TOOL_STATUS_FOR_GPT, OPTION2_*, memory.md) are **historical/superseded** — use
the four docs above for current truth. `memory.md` in particular is an append-only log from the original
upgrade; its "Assumptions"/"Pending" sections are now factually wrong._

## Overriding product rule (never violate)
**Never invent, assume, or fabricate a financial number.** Missing data ≠ zero; failed pricing ≠ a price;
failed billing ≠ zero spend; failed metrics ≠ zero utilisation; partial collection ≠ a complete assessment.
Every saving is grounded in the resource's actual billed cost or shown as REVIEW / "Not quantified".

## Status (2026-08-21)
Batch 1 financial integrity ✅ · Batch 2 Azure reliability ✅ · Auth hardening ✅ **implemented** ·
Program A–H ✅ · Dark mode ✅ · RI reconciliation / migration isolation ✅.
Last commit `0ae9c9d` (2026-08-21) landed dark mode + RI reconciliation + billing resilience.

**Open, investigated but NOT fixed** — see `PROJECT_HANDOFF.md` §9–§10:
1. **Cost Management 429 throttling** — one assessment issues **78 CM HTTP requests per subscription**
   under continuous 429 (6 sequential queries × 13 attempts; the whole-billing retry duplicates all of
   them), and can sit in backoff for up to ~3.9 h. No circuit breaker on the billing plane.
   _Redis / queueing / per-customer locking are explicitly OUT of scope and are NOT the agreed solution._
2. `basis` (per-finding "how this was calculated") is computed + served but rendered nowhere.
3. `sql_ahb_eligible` + `geo_redundant_vaults` inventory buckets are fetched every run, consumed by
   nothing, and can flip a run to PARTIAL.
4. Four dead frontend modules (~970 lines, zero importers, absent from the bundle).

## How the user works (apply every session)
- Directs work in explicit **numbered batches**; often wants an **audit first**, then implementation.
- **Scope discipline:** don't touch UI redesign, auth, or unrelated code unless the batch says so; don't
  weaken earlier batches' rules/tests. Trace real code; don't infer from names.
- **Always verify + report:** _what changed · tests passed · build status · remaining risks · answers to
  their questions._ Never claim success unless tests pass.

## Verify (from repo root)
```bash
cd backend && python -m pytest -q            # 426 tests, ~61s; MUST cd into backend (cwd resets to repo root)
cd frontend && npx tsc --noEmit && npx vite build
```
Backend must be restarted (uvicorn) to pick up code changes; a fresh assessment re-run is needed for
finding-level detail changes. DB schema: `create_all` + `database.ensure_runtime_columns()` (no live Alembic).
