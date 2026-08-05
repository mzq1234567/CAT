"""Tests for report generation with validation + confidence (Step 9)."""
from __future__ import annotations

from datetime import datetime

from app.models.db import Assessment, Finding
from app.services.report import generate_pdf


def _assessment():
    a = Assessment(
        user_id="u1", user_email="u@x.com", tenant_id="t1", subscription_ids=["sub-1"],
        status="completed", total_savings_monthly=120.0, total_savings_annual=1440.0,
        findings_count=2, needs_review_count=1,
    )
    a.id = 1
    a.created_at = datetime(2025, 1, 15, 10, 0)
    a.snapshot_at = datetime(2025, 1, 15, 10, 1)
    return a


def _findings():
    f1 = Finding(
        category="idle_vms", display_name="Idle Virtual Machines", resource_name="vm-1",
        resource_group="rg-a", subscription_id="sub-1", resource_type="microsoft.compute/virtualmachines",
        estimated_savings_monthly=100.0, estimated_savings_annual=1200.0, severity="high",
        confidence=0.95, description="idle", recommendation="deallocate",
        validation_status="validated", validation_variance_pct=4.3, actual_monthly_cost=96.0,
        advisor_recommendation_id="ADV-1", details={"validation_note": "within trend"},
    )
    f2 = Finding(
        category="unattached_managed_disks", display_name="Unattached Managed Disks",
        resource_name="disk-1", resource_group="rg-a", subscription_id="sub-1",
        resource_type="microsoft.compute/disks",
        estimated_savings_monthly=20.0, estimated_savings_annual=240.0, severity="medium",
        confidence=0.5, description="unattached", recommendation="delete",
        validation_status="needs_review", validation_variance_pct=140.0, actual_monthly_cost=5.0,
        details={"validation_note": "estimate exceeds actual cost"},
    )
    return [f1, f2]


def test_generate_pdf_is_valid():
    pdf = generate_pdf(_assessment(), _findings())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000


def test_pdf_handles_missing_snapshot():
    a = _assessment()
    a.snapshot_at = None
    pdf = generate_pdf(a, _findings())
    assert pdf[:4] == b"%PDF"


def _rich_assessment():
    a = _assessment()
    a.tenant_display_name = "CamaPlan"
    a.subscription_names = {"sub-1": "Azure subscription"}
    a.major_resource_types = [{"type": "Virtual Machines", "count": 30}, {"type": "Disks", "count": 40}]
    a.total_resources = 161
    a.resource_type_count = 12
    a.cost_data_available = 1
    a.current_monthly_spend = 5282.66
    a.current_annual_spend = 63391.92
    a.total_savings_monthly = 227.19
    a.total_savings_annual = 2726.32
    a.observed_annual_growth = 0.12  # drives the Linear (12%) + Conservative (6%) projections
    return a


def _rich_findings():
    ri = Finding(
        category="ri_vm", display_name="Reserved Instance (VM)", resource_name="RIs — 2 SKUs",
        subscription_id="sub-1", resource_type="microsoft.consumption/reservationrecommendations",
        estimated_savings_monthly=150.0, estimated_savings_annual=1800.0, severity="high",
        confidence=0.9, description="reserve", recommendation="buy RI",
        details={"aggregate": True, "source": "azure_reservation_recommendations",
                 "total_1yr_monthly": 150.0, "total_3yr_monthly": 240.0,
                 "reservation_items": [
                     {"name": "Standard_D8s_v3", "sku": "Standard_D8s_v3", "region": "westus2",
                      "quantity": 3, "monthly_savings": 100.0, "monthly_savings_3yr": 160.0,
                      "monthly_ondemand": 560.0, "monthly_reserved": 460.0},
                     {"name": "Standard_B2ms", "sku": "Standard_B2ms", "region": "westus2",
                      "quantity": 4, "monthly_savings": 50.0, "monthly_savings_3yr": 80.0,
                      "monthly_ondemand": 410.0, "monthly_reserved": 360.0}]},
    )
    ahb = Finding(
        category="windows_ahb", display_name="Windows Azure Hybrid Benefit",
        resource_name="3 Windows VMs eligible for AHB", subscription_id="sub-1",
        resource_type="microsoft.compute/virtualmachines",
        estimated_savings_monthly=250.0, estimated_savings_annual=3000.0, severity="critical",
        confidence=0.7, description="ahb", recommendation="apply ahb",
        details={"eligible_count": 3, "eligible_vms": [
            {"name": "vmwinserver", "sku": "Standard_D8s_v3", "monthly_savings": 250.0,
             "actual_cost_based": True}]},
    )
    disk = Finding(
        category="unattached_managed_disks", display_name="Unattached Managed Disks",
        resource_name="orphan-osdisk", subscription_id="sub-1",
        resource_type="microsoft.compute/disks",
        estimated_savings_monthly=4.8, estimated_savings_annual=57.6, severity="low",
        confidence=0.9, description="unattached", recommendation="delete",
        details={"skuName": "Standard_SSD_LRS", "diskSizeGB": 128},
    )
    ip = Finding(
        category="orphaned_public_ips", display_name="Orphaned Public IP Addresses",
        resource_name="vpnpublicip", subscription_id="sub-1",
        resource_type="microsoft.network/publicipaddresses",
        estimated_savings_monthly=3.72, estimated_savings_annual=44.64, severity="low",
        confidence=0.9, description="orphan ip", recommendation="delete",
        details={"skuName": "Basic"},
    )
    return [ri, ahb, disk, ip]


def test_pdf_full_template_renders():
    """The full TPT template (cover, TOC, exec + projections, pillar tables) builds without error."""
    pdf = generate_pdf(_rich_assessment(), _rich_findings())
    assert pdf[:4] == b"%PDF"


def test_projections_render_without_measured_growth():
    """With too little history (no measured growth), the trend-based charts are skipped but the
    report still builds — the Fixed and Civo scenarios remain."""
    a = _rich_assessment()
    a.observed_annual_growth = None
    pdf = generate_pdf(a, _rich_findings())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 5000  # multi-page branded report, not a stub


def test_projections_suppressed_when_savings_exceed_spend():
    """Partial/young billing window: run-rate savings exceed the measured (partial) spend, so the
    spend-based projection charts would go negative — the report must suppress them and still build."""
    a = _rich_assessment()
    a.current_monthly_spend = 2798.0
    a.current_annual_spend = 33576.0          # partial window (mid-cycle migration)
    a.total_savings_monthly = 132000.0
    a.total_savings_annual = 1590000.0        # run-rate savings >> measured spend
    pdf = generate_pdf(a, _rich_findings())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 5000                    # still a full report, just no broken projection bars


def test_report_currency_symbols():
    from app.services.report import _set_currency, _usd
    _set_currency("GBP"); assert _usd(1000) == "£1,000.00"
    _set_currency("INR"); assert _usd(1000) == "Rs 1,000.00"      # Helvetica-safe (no ₹ glyph)
    _set_currency("CAD"); assert _usd(1000) == "CA$1,000.00"      # disambiguated from USD
    _set_currency("USD")  # reset for other tests


def test_pdf_renders_in_non_usd_currency():
    a = _rich_assessment()
    a.currency = "GBP"
    pdf = generate_pdf(a, _rich_findings())
    assert pdf[:4] == b"%PDF"
