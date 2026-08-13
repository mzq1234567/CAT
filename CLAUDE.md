# CLAUDE.md — auto-loaded project context

**What this is:** "Azure CAT" (TPT Azure Cost Assessment) — a multi-tenant SaaS that assesses a client's
Azure subscriptions for cost savings and produces a client-presentable dashboard + PDF report.
Frontend: React + Vite + Material UI (light theme). Backend: FastAPI + SQLAlchemy + SQLite, async `httpx`
to Azure. Delegated, read-only Azure auth.

## Read these first (repo-committed, always current)
1. **`PROJECT_HANDOFF.md`** — full cold-start brief: what the tool is, status, how the user works, verify commands.
2. **`FINANCIAL_INTEGRITY_AUDIT.md`** — Batch 1 (evidence model / no-fabrication rules).
3. **`AZURE_DATA_COLLECTION_AUDIT.md`** — Batch 2 (throttling/retry/concurrency/partial-data).
4. **`AUTHENTICATION_ARCHITECTURE_AUDIT.md`** — current auth + the not-yet-built recommendation.

_Other root `*.md` files (ACCURACY_AUDIT, AHB_*, FABRICATION_AUDIT, FIX_PLAN_*, CODEBASE_BRIEF_*,
BACKEND_AUTH_ARCHITECTURE, TOOL_STATUS_FOR_GPT, OPTION2_*, memory.md) are **historical/superseded** — use
the four docs above for current truth._

## Overriding product rule (never violate)
**Never invent, assume, or fabricate a financial number.** Missing data ≠ zero; failed pricing ≠ a price;
failed billing ≠ zero spend; failed metrics ≠ zero utilisation; partial collection ≠ a complete assessment.
Every saving is grounded in the resource's actual billed cost or shown as REVIEW / "Not quantified".

## Status (2026-08-13)
Batch 1 financial integrity ✅ · Batch 2 Azure reliability ✅ · Authentication 🔍 audited, NOT implemented.
**Next (only on the user's go-ahead):** implement the reviewed auth hardening (see the auth audit).

## How the user works (apply every session)
- Directs work in explicit **numbered batches**; often wants an **audit first**, then implementation.
- **Scope discipline:** don't touch UI redesign, auth, or unrelated code unless the batch says so; don't
  weaken earlier batches' rules/tests. Trace real code; don't infer from names.
- **Always verify + report:** _what changed · tests passed · build status · remaining risks · answers to
  their questions._ Never claim success unless tests pass.

## Verify (from repo root)
```bash
cd backend && python -m pytest -q            # ~304 tests; MUST cd into backend (cwd resets to repo root)
cd frontend && npx tsc --noEmit && npx vite build
```
Backend must be restarted (uvicorn) to pick up code changes; a fresh assessment re-run is needed for
finding-level detail changes. DB schema: `create_all` + `database.ensure_runtime_columns()` (no live Alembic).
