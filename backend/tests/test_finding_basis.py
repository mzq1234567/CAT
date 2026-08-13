"""Tests for the canonical 'how this number was calculated' basis helper (Phase C)."""
from __future__ import annotations

from app.services.finding_basis import describe_finding_basis
from app.services.financial_evidence import ACTUAL_BILLED_COST, QUANTIFIED, REVIEW


def _f(**kw):
    base = {"evidence_state": QUANTIFIED, "category": "idle_vms", "validation_status": None, "details": {}}
    base.update(kw)
    return base


# ── REVIEW variants — never a saving, always flagged as reference-only ──────────────────────────

def test_review_sponsored_says_reference_only_and_no_saving():
    b = describe_finding_basis(_f(evidence_state=REVIEW, category="bastion_hosts",
                                  details={"sponsored_or_credited": True}))
    assert "reference only" in b
    assert "Sponsorship" in b or "credited" in b
    assert "saving" in b.lower()  # explicitly frames the absence of a defensible saving


def test_review_insufficient_history_states_the_span():
    b = describe_finding_basis(_f(evidence_state=REVIEW,
                                  details={"insufficient_billing_history": True, "billing_span_days": 9}))
    assert "9 days" in b and "review" in b.lower()


def test_review_no_billed_cost_is_reference_only():
    b = describe_finding_basis(_f(evidence_state=REVIEW, category="orphaned_public_ips", details={}))
    assert "couldn't establish" in b.lower() and "reference only" in b


# ── QUANTIFIED variants — describe the real value source ────────────────────────────────────────

def test_quantified_actual_billed_cost():
    b = describe_finding_basis(_f(details={"savings_source": ACTUAL_BILLED_COST}))
    assert "actual billed cost" in b.lower() and "Cost Management" in b


def test_quantified_reservation_engine():
    b = describe_finding_basis(_f(category="ri_vm",
                                  details={"source": "azure_reservation_recommendations"}))
    assert "reservation engine" in b.lower() and "already hold" in b.lower()


def test_quantified_capped_at_actual_cost():
    b = describe_finding_basis(_f(details={"savings_capped_at_actual_cost": True}))
    assert "capped" in b.lower() and "never exceeds" in b.lower()


def test_quantified_validated_crosschecked():
    b = describe_finding_basis(_f(validation_status="validated", details={}))
    assert "cross-checked" in b.lower() and "retail pricing" in b.lower()


def test_quantified_conditional_ahb_names_the_licence():
    b = describe_finding_basis(_f(category="windows_ahb", details={"conditional": True}))
    assert "Windows Server" in b and "already own" in b.lower()
    b_sql = describe_finding_basis(_f(category="sql_ahb", details={"conditional": True}))
    assert "SQL Server" in b_sql


def test_quantified_default_is_retail_pricing():
    b = describe_finding_basis(_f(details={}))
    assert "retail pricing" in b.lower()


def test_quantified_run_rate_flags_estimate():
    b = describe_finding_basis(_f(details={"cost_basis": "run_rate"}))
    assert "partial billing period" in b.lower() and "re-run" in b.lower()


def test_quantified_spend_cap_is_disclosed():
    b = describe_finding_basis(_f(details={"savings_capped_at_actual_cost": True,
                                           "savings_capped_at_measured_spend": True}))
    assert "measured spend" in b.lower()


def test_conditional_does_not_get_estimate_suffix():
    # AHB carries its own partial-billing note elsewhere; the basis must not double up the estimate flag.
    b = describe_finding_basis(_f(category="windows_ahb",
                                  details={"conditional": True, "cost_basis": "run_rate"}))
    assert "partial billing period" not in b.lower()


def test_all_basis_sentences_end_with_a_period():
    for f in (_f(details={"savings_source": ACTUAL_BILLED_COST}),
              _f(evidence_state=REVIEW, details={}),
              _f(category="windows_ahb", details={"conditional": True})):
        assert describe_finding_basis(f).endswith(".")


def test_works_on_object_not_just_dict():
    class _Obj:
        evidence_state = QUANTIFIED
        category = "idle_vms"
        validation_status = "validated"
        details = {}
    b = describe_finding_basis(_Obj())
    assert b and "cross-checked" in b.lower()
