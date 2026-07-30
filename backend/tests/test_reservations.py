"""Unit tests for parsing Azure Consumption reservationRecommendations (authoritative RI source)."""
from __future__ import annotations

from app.services.reservations import parse_reservation_recommendations


def _legacy(sku="Standard_D2s_v3", rtype="virtualmachines", term="P1Y", look="Last30Days",
            net=100.0, no_ri=400.0, with_ri=300.0, qty=2, region="eastus", scope="Single"):
    """A legacy (subscription-scope) recommendation item — flat decimal figures."""
    return {
        "kind": "legacy", "location": region,
        "properties": {
            "resourceType": rtype, "normalizedSize": sku, "term": term,
            "lookBackPeriod": look, "scope": scope, "netSavings": net,
            "costWithNoReservedInstances": no_ri, "totalCostWithReservedInstances": with_ri,
            "recommendedQuantity": qty, "instanceFlexibilityGroup": "DSv3 Series",
            "subscriptionId": "sub-1",
        },
    }


def test_parse_groups_one_and_three_year_terms_together():
    items = [_legacy(term="P1Y", net=100.0), _legacy(term="P3Y", net=160.0)]
    groups = parse_reservation_recommendations(items, "sub-1")
    assert len(groups) == 1
    g = groups[0]
    assert g["category"] == "ri_vm" and g["sku"] == "Standard_D2s_v3"
    assert g["terms"]["P1Y"]["monthly_savings"] == 100.0   # Last30Days → factor 1
    assert g["terms"]["P3Y"]["monthly_savings"] == 160.0
    assert g["terms"]["P1Y"]["quantity"] == 2


def test_parse_normalises_shorter_lookback_to_monthly():
    # A 7-day window saving of $21 → ~$90/mo (×30/7).
    g = parse_reservation_recommendations([_legacy(look="Last7Days", net=21.0)], "sub-1")[0]
    assert g["terms"]["P1Y"]["monthly_savings"] == 90.0


def test_parse_prefers_30_day_window_over_7_day_for_same_term():
    items = [_legacy(look="Last7Days", net=21.0), _legacy(look="Last30Days", net=100.0)]
    g = parse_reservation_recommendations(items, "sub-1")[0]
    assert g["terms"]["P1Y"]["monthly_savings"] == 100.0  # 30-day wins regardless of order


def test_parse_handles_modern_money_objects():
    item = _legacy()
    p = item["properties"]
    p["netSavings"] = {"currency": "USD", "value": 120.0}
    p["costWithNoReservedInstances"] = {"currency": "USD", "value": 500.0}
    p["totalCostWithReservedInstances"] = {"currency": "USD", "value": 380.0}
    p["lookBackPeriod"] = 30  # modern returns an int
    g = parse_reservation_recommendations([item], "sub-1")[0]
    assert g["terms"]["P1Y"]["monthly_savings"] == 120.0
    assert g["terms"]["P1Y"]["monthly_ondemand"] == 500.0


def test_parse_maps_non_vm_resource_types():
    g = parse_reservation_recommendations(
        [_legacy(sku="SQLDB_BC_Gen5", rtype="sqldatabases", net=250.0)], "sub-1")[0]
    assert g["category"] == "sql_db_reserved_capacity" and g["product"] == "SQL Database"


def test_parse_skips_zero_and_negative_savings():
    assert parse_reservation_recommendations([_legacy(net=0.0)], "sub-1") == []
    assert parse_reservation_recommendations([_legacy(net=-5.0)], "sub-1") == []


def test_parse_skips_unknown_resource_type():
    assert parse_reservation_recommendations([_legacy(rtype="quantumwidgets")], "sub-1") == []
