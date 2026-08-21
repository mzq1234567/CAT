"""
Cost Management REQUEST BUDGET regression tests.

These tests exist because the Cost Management retry envelope is multiplicative: the pipeline issues up
to 6 sequential CM queries per subscription under total throttling (3 in the first pass, 3 more in the
whole-billing retry), so every extra retry costs 6x. Before the budget was bounded, 12 retries meant
**78 CM requests per subscription** and up to ~3.9h of backoff, against a healthy baseline of just 2
requests. See PROJECT_HANDOFF.md §9.

What is pinned here is the BUDGET ITSELF, measured by counting real outbound HTTP requests through
httpx.MockTransport — not the retry constants in isolation. If someone raises the retry count, adds a
CM query, or adds another whole-billing pass, these tests fail with the actual number, which is the
signal that matters.

Financial-integrity semantics are asserted alongside the counts, so a future budget change can never
silently trade honesty for fewer requests: a recovered throttle must still be COMPLETE, an exhausted
one must still be PARTIAL with NO fabricated spend, and resource-based findings must still surface.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.db import Assessment, Finding
from app.services import assessment as pipeline
from app.services import resilience
from app.services.azure_client import (
    COST_MANAGEMENT_MAX_RETRIES,
    COST_MANAGEMENT_MAX_RETRY_AFTER,
    AzureClient,
)
from app.services.pricing import PricingEngine
from app.services.reservations import reconcile_vm_recommendations
from tests.test_assessment_pipeline import _composite_handler, _instant_sleep

# The pipeline's CM query plan under TOTAL throttling, per subscription:
#   pass 1 -> monthly-history, daily run-rate, service-costs fallback          (3 queries)
#   whole-billing retry (assessment.py) repeats all three                      (3 queries)
CM_QUERIES_PER_SUB_WHEN_THROTTLED = 6
ATTEMPTS_PER_QUERY = COST_MANAGEMENT_MAX_RETRIES + 1          # 1 initial + N retries
MAX_CM_REQUESTS_PER_SUB = CM_QUERIES_PER_SUB_WHEN_THROTTLED * ATTEMPTS_PER_QUERY

CM_PATH = "/providers/Microsoft.CostManagement/query"


def _counting(handler, counter, *, force_429=False, retry_after=None):
    """Wrap a composite Azure handler, counting Cost Management requests.

    `force_429` makes every CM request return 429 (optionally with a `Retry-After`), so the retry
    budget is exercised end-to-end; everything else is delegated to the wrapped handler.
    """
    def wrapped(request: httpx.Request) -> httpx.Response:
        if CM_PATH in request.url.path:
            counter[0] += 1
            if force_429:
                headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
                return httpx.Response(429, headers=headers,
                                      json={"error": {"code": "TooManyRequests"}})
        return handler(request)
    return wrapped


@pytest.fixture
def budget_env(monkeypatch):
    """In-memory DB + mocked Azure, returning (session_factory, install, seed, cm_counter).

    `install` takes an already-built composite handler and wraps it in the CM counter.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)
    # Never actually wait: these tests measure REQUEST COUNT, not wall clock.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)
    monkeypatch.setattr(pipeline, "COST_MAP_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(pipeline, "BILLING_RETRY_DELAY_SECONDS", 0)

    counter = [0]

    def install(handler, *, force_429=False, retry_after=None):
        transport = httpx.MockTransport(
            _counting(handler, counter, force_429=force_429, retry_after=retry_after))
        monkeypatch.setattr(pipeline, "AzureClient",
                            lambda token, **kw: AzureClient(token, transport=transport, **kw))
        monkeypatch.setattr(pipeline, "get_pricing_engine",
                            lambda currency=None: PricingEngine(transport=transport))

    def seed():
        s = TestSession()
        a = Assessment(user_id="u1", user_email="u@x.com", tenant_id="t1",
                       subscription_ids=["sub-1"], status="queued")
        s.add(a)
        s.commit()
        aid = a.id
        s.close()
        return aid

    return TestSession, install, seed, counter


def _row(TestSession, aid):
    s = TestSession()
    try:
        return s.get(Assessment, aid)
    finally:
        s.close()


# ── A. a healthy run must not retry at all ────────────────────────────────────────────────────────

async def test_A_successful_billing_run_performs_no_retries(budget_env):
    """A run where Cost Management answers 200 first time must cost the MINIMUM number of requests and
    perform ZERO retries. This is the baseline the budget protects: the healthy path is 2 CM requests
    (monthly history + service costs), not 6 — the 6-query plan only exists on failure paths."""
    TestSession, install, seed, cm = budget_env
    install(_composite_handler())
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = _row(TestSession, aid)
    assert cm[0] == 2, f"healthy run should cost exactly 2 CM requests, got {cm[0]}"
    retry = a.collection_diagnostics["retry"]
    assert retry["retries"] == 0                 # no retry attempts
    assert retry["throttled_responses"] == 0     # nothing was throttled
    assert retry["exhausted"] == 0               # nothing gave up
    assert a.data_quality == "complete"
    assert a.cost_data_available == 1


# ── B. continuous 429 must not exceed the bounded budget ──────────────────────────────────────────

async def test_B_continuous_429_cannot_exceed_cost_management_budget(budget_env):
    """THE budget test. Under unrelenting 429 the run must stop at the bounded number of Cost
    Management requests — 6 queries x (1 + COST_MANAGEMENT_MAX_RETRIES) — and never grind on."""
    TestSession, install, seed, cm = budget_env
    install(_composite_handler(), force_429=True)
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    assert cm[0] == MAX_CM_REQUESTS_PER_SUB, (
        f"CM request budget breached: {cm[0]} requests for one subscription "
        f"(budget is {CM_QUERIES_PER_SUB_WHEN_THROTTLED} queries x {ATTEMPTS_PER_QUERY} attempts "
        f"= {MAX_CM_REQUESTS_PER_SUB})")
    # Pin the absolute number too, so a change to the query plan OR the retry count is visible.
    assert cm[0] == 30
    assert _row(TestSession, aid).data_quality == "partial"


async def test_B2_budget_scales_linearly_and_is_bounded_per_subscription(budget_env, monkeypatch):
    """Two subscriptions cost exactly twice one subscription — no cross-subscription amplification."""
    TestSession, install, seed, cm = budget_env
    install(_composite_handler(), force_429=True)
    s = TestSession()
    a = Assessment(user_id="u1", user_email="u@x.com", tenant_id="t1",
                   subscription_ids=["sub-1", "sub-2"], status="queued")
    s.add(a)
    s.commit()
    aid = a.id
    s.close()

    await pipeline.run_assessment(aid, ["sub-1", "sub-2"], "token")

    assert cm[0] == 2 * MAX_CM_REQUESTS_PER_SUB == 60


# ── C. Retry-After must not be able to unbound the budget ─────────────────────────────────────────

async def test_C_retry_after_cannot_unbound_the_budget(budget_env, monkeypatch):
    """A hostile / very large `Retry-After` must change neither the request count nor produce an
    unbounded wait: every honoured wait is clamped to COST_MANAGEMENT_MAX_RETRY_AFTER."""
    waits: list[float] = []

    async def _record(seconds):
        waits.append(float(seconds))

    monkeypatch.setattr(resilience, "_sleep", _record)
    TestSession, install, seed, cm = budget_env
    # An hour-long Retry-After: without the cap this would be honoured verbatim.
    install(_composite_handler(), force_429=True, retry_after=3600)
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    assert cm[0] == MAX_CM_REQUESTS_PER_SUB          # count unchanged by Retry-After
    assert waits, "expected the retry layer to have waited at least once"
    assert max(waits) <= COST_MANAGEMENT_MAX_RETRY_AFTER, (
        f"Retry-After was honoured beyond the cap: {max(waits)}s > {COST_MANAGEMENT_MAX_RETRY_AFTER}s")
    # Total CM backoff is bounded by (queries x retries x cap), so the run can never hang for hours.
    cm_waits = [w for w in waits if w == COST_MANAGEMENT_MAX_RETRY_AFTER]
    assert len(cm_waits) <= CM_QUERIES_PER_SUB_WHEN_THROTTLED * COST_MANAGEMENT_MAX_RETRIES


# ── D. a 429 that recovers within the budget must still yield COMPLETE billing ────────────────────

async def test_D_429_then_recovery_still_produces_complete_billing(budget_env):
    """A transient throttle inside the budget must still recover fully: real spend, COMPLETE quality,
    no billing failure recorded. Reducing the budget must not turn transient throttles into failures."""
    TestSession, install, seed, cm = budget_env
    # Throttle fewer times than one query's budget, so the very first query recovers on retry.
    install(_composite_handler(cost_throttle_first_n=COST_MANAGEMENT_MAX_RETRIES - 1))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = _row(TestSession, aid)
    assert a.data_quality == "complete"                  # a recovered throttle is NOT a partial run
    assert a.cost_data_available == 1
    assert a.current_monthly_spend is not None           # real spend from the recovered response
    assert a.collection_diagnostics["billing_failed_subs"] == 0
    assert a.collection_diagnostics["retry"]["throttled_responses"] >= 1   # throttling really happened


# ── E. exhausting the budget must be honest: PARTIAL, and nothing fabricated ──────────────────────

async def test_E_budget_exhaustion_is_partial_and_fabricates_nothing(budget_env):
    """When the (now smaller) budget is exhausted the run must report the failure honestly and invent
    no financial data — while resource-based findings, which never needed billing, still surface."""
    TestSession, install, seed, cm = budget_env
    install(_composite_handler(), force_429=True)
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"                  # the assessment itself still completes
    assert a.data_quality == "partial"              # honest failure state
    assert a.collection_diagnostics["billing_failed_subs"] >= 1
    assert a.cost_data_available == 0
    assert a.current_monthly_spend is None          # NO fabricated current spend
    assert a.current_annual_spend is None           # NO fabricated projected spend
    assert a.total_savings_annual == 0.0            # billing-dependent savings stay unquantified
    # Resource-based findings do not depend on billing and must be unaffected by the budget.
    findings = s.query(Finding).filter(Finding.assessment_id == aid).all()
    cats = {f.category for f in findings}
    assert "unattached_managed_disks" in cats and "orphaned_public_ips" in cats
    assert all(f.evidence_state == "review" for f in findings
               if f.category in ("unattached_managed_disks", "orphaned_public_ips"))
    s.close()


# ── F. the single whole-billing retry must still be able to rescue a run ──────────────────────────

async def test_F_whole_billing_retry_still_recovers_after_first_pass_exhausts(budget_env):
    """The whole-billing retry is preserved by this change. Throttle the ENTIRE first pass to
    exhaustion (3 queries x 5 attempts = 15 requests), then let Azure recover: the patient second
    attempt must still rescue the run to COMPLETE, clear the stale billing-failed flag, and forgive the
    transient exhaustion (the #127 semantics)."""
    TestSession, install, seed, cm = budget_env
    first_pass_requests = 3 * ATTEMPTS_PER_QUERY          # 15
    install(_composite_handler(cost_throttle_first_n=first_pass_requests))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = _row(TestSession, aid)
    diag = a.collection_diagnostics
    assert diag["retry"]["throttled_responses"] >= 1   # throttling genuinely occurred (telemetry kept)
    assert a.cost_data_available == 1                  # billing recovered on the whole-billing retry
    assert a.current_monthly_spend is not None         # real spend populated
    assert diag["billing_failed_subs"] == 0            # stale failure flag cleared
    assert diag["retry"]["exhausted"] == 0             # recovered billing exhaustion forgiven
    assert a.data_quality == "complete"
    # Cost: the exhausted first pass plus a recovered second pass — still far under the 429 ceiling.
    assert cm[0] == first_pass_requests + 2 == 17
    assert cm[0] < MAX_CM_REQUESTS_PER_SUB


# ── H. isolation + resource-based behaviour must be untouched by the budget change ────────────────

def test_H_subscription_isolation_unchanged_by_budget_change():
    """The budget change must not touch subscription-migration isolation: a stale recommendation scoped
    to the OLD subscription still must not attach to a same-SKU VM now in the NEW subscription."""
    from tests.test_findings import _current_vm, _vm_ri_group

    stale_old = _vm_ri_group(sku="Standard_D2s_v3", subscription_id="old-sub")
    current_new = [_current_vm(sku="Standard_D2s_v3", subscription_id="new-sub", name="moved-vm")]
    assert reconcile_vm_recommendations([stale_old], current_new, assessment_id=1) == []
    # And a subscription with no current VMs still yields nothing.
    assert reconcile_vm_recommendations([stale_old], [], assessment_id=1) == []


async def test_H2_resource_based_findings_unaffected_by_cost_management_budget(budget_env):
    """Resource-based detection runs off Resource Graph, not Cost Management. Total CM failure must not
    reduce the resources discovered or the findings raised from inventory alone."""
    TestSession, install, seed, cm = budget_env
    install(_composite_handler(), force_429=True)
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = _row(TestSession, aid)
    assert a.total_resources == 50          # inventory summary unaffected by the CM budget
    assert a.resource_type_count == 3
    assert a.findings_count >= 2            # disk + IP still detected from inventory
