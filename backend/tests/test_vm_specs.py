"""Tests for the VM SKU spec table + same-series ladder walk."""
from __future__ import annotations

from app.services.vm_specs import get_spec, smaller_same_series


def test_get_spec_known_sku():
    spec = get_spec("Standard_D16s_v3")
    assert spec is not None
    assert spec.vcpu == 16
    assert spec.memory_gb == 64
    assert spec.family == "Dsv3"


def test_get_spec_case_insensitive():
    assert get_spec("standard_d16s_v3") is not None


def test_get_spec_unknown_sku_returns_none():
    assert get_spec("Standard_Nonexistent_v9") is None


def test_smaller_same_series_ordered_largest_first():
    ladder = smaller_same_series("Standard_D16s_v3")
    names = [s.sku for s in ladder]
    assert names == ["Standard_D8s_v3", "Standard_D4s_v3", "Standard_D2s_v3"]


def test_smaller_same_series_excludes_other_families():
    ladder = smaller_same_series("Standard_E8s_v3")
    assert all(s.family == "Esv3" for s in ladder)
    assert "Standard_D4s_v3" not in [s.sku for s in ladder]


def test_smaller_same_series_smallest_has_no_smaller():
    assert smaller_same_series("Standard_D2s_v3") == []


def test_smaller_same_series_unknown_sku_returns_empty():
    assert smaller_same_series("Standard_Nonexistent_v9") == []
