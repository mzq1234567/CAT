"""
VM SKU specs (vCPU / RAM) + same-series downsize ladder.

Needed for two things the findings engine cannot do with pricing alone:
  1. Converting a peak-CPU/peak-memory *percentage* into absolute cores/GB used, so it can be
     checked against a *candidate* smaller SKU's actual capacity (not just re-applied as a %).
  2. Naming a specific, concrete downsize target ("D16s_v3 → D8s_v3") instead of vague advice.

Data source tradeoff (documented, see memory.md): this is a **curated static table** for the
common D/E/B/F v3/v4/v5 series, not the live `Microsoft.Compute/skus` ARM API. That API is
region-scoped, returns thousands of rows, and needs per-region filtering — a real upgrade path,
but out of scope for now. The static table covers the series seen in the vast majority of real
fleets. An unknown SKU simply yields no ladder (no downsize recommendation is made for it) rather
than a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

GB = 1024 ** 3


@dataclass(frozen=True)
class VmSpec:
    sku: str        # ARM SKU name, e.g. "Standard_D8s_v3"
    family: str      # series identity used for ladder grouping, e.g. "Dsv3"
    size: int        # the numeral in the SKU (8 for D8s_v3) — proxy for relative capacity
    vcpu: int
    memory_gb: float


# Curated specs for common general-purpose / memory-optimised series.
# Extend this table as real customer fleets surface series it doesn't yet cover.
_RAW_SPECS: List[VmSpec] = [
    # D-series v3 (general purpose)
    VmSpec("Standard_D2s_v3", "Dsv3", 2, 2, 8),
    VmSpec("Standard_D4s_v3", "Dsv3", 4, 4, 16),
    VmSpec("Standard_D8s_v3", "Dsv3", 8, 8, 32),
    VmSpec("Standard_D16s_v3", "Dsv3", 16, 16, 64),
    VmSpec("Standard_D32s_v3", "Dsv3", 32, 32, 128),
    VmSpec("Standard_D64s_v3", "Dsv3", 64, 64, 256),
    # D-series v5 (general purpose, current generation)
    VmSpec("Standard_D2s_v5", "Dsv5", 2, 2, 8),
    VmSpec("Standard_D4s_v5", "Dsv5", 4, 4, 16),
    VmSpec("Standard_D8s_v5", "Dsv5", 8, 8, 32),
    VmSpec("Standard_D16s_v5", "Dsv5", 16, 16, 64),
    VmSpec("Standard_D32s_v5", "Dsv5", 32, 32, 128),
    VmSpec("Standard_D64s_v5", "Dsv5", 64, 64, 256),
    # E-series v3 (memory optimised)
    VmSpec("Standard_E2s_v3", "Esv3", 2, 2, 16),
    VmSpec("Standard_E4s_v3", "Esv3", 4, 4, 32),
    VmSpec("Standard_E8s_v3", "Esv3", 8, 8, 64),
    VmSpec("Standard_E16s_v3", "Esv3", 16, 16, 128),
    VmSpec("Standard_E32s_v3", "Esv3", 32, 32, 256),
    # E-series v5 (memory optimised, current generation)
    VmSpec("Standard_E2s_v5", "Esv5", 2, 2, 16),
    VmSpec("Standard_E4s_v5", "Esv5", 4, 4, 32),
    VmSpec("Standard_E8s_v5", "Esv5", 8, 8, 64),
    VmSpec("Standard_E16s_v5", "Esv5", 16, 16, 128),
    VmSpec("Standard_E32s_v5", "Esv5", 32, 32, 256),
    # F-series v2 (compute optimised)
    VmSpec("Standard_F2s_v2", "Fsv2", 2, 2, 4),
    VmSpec("Standard_F4s_v2", "Fsv2", 4, 4, 8),
    VmSpec("Standard_F8s_v2", "Fsv2", 8, 8, 16),
    VmSpec("Standard_F16s_v2", "Fsv2", 16, 16, 32),
    VmSpec("Standard_F32s_v2", "Fsv2", 32, 32, 64),
    # B-series (burstable)
    VmSpec("Standard_B2s", "B", 2, 2, 4),
    VmSpec("Standard_B2ms", "Bms", 2, 2, 8),
    VmSpec("Standard_B4ms", "Bms", 4, 4, 16),
    VmSpec("Standard_B8ms", "Bms", 8, 8, 32),
]

_BY_SKU: Dict[str, VmSpec] = {s.sku.lower(): s for s in _RAW_SPECS}

# Group by family, sorted ascending by size — used to walk the ladder.
_BY_FAMILY: Dict[str, List[VmSpec]] = {}
for _spec in _RAW_SPECS:
    _BY_FAMILY.setdefault(_spec.family, []).append(_spec)
for _family in _BY_FAMILY:
    _BY_FAMILY[_family].sort(key=lambda s: s.size)


def get_spec(sku: str) -> Optional[VmSpec]:
    """Look up a VM SKU's vCPU/RAM. None if the SKU isn't in the curated table."""
    return _BY_SKU.get((sku or "").lower())


def smaller_same_series(sku: str) -> List[VmSpec]:
    """Same-series SKUs smaller than `sku`, ordered largest→smallest (nearest step first).

    Empty list if the SKU is unknown or is already the smallest in its family — callers should
    treat that as "no downsize recommendation available" rather than guessing.
    """
    spec = get_spec(sku)
    if spec is None:
        return []
    family_sizes = _BY_FAMILY.get(spec.family, [])
    smaller = [s for s in family_sizes if s.size < spec.size]
    return sorted(smaller, key=lambda s: s.size, reverse=True)
