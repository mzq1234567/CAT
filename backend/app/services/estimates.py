"""
RETIRED — this module no longer holds any client-facing price or savings constant.

Every optimisation dollar figure now comes from an authoritative source, in this strict order:
  1. Cost Management actual billed cost (the resource's real bill), then
  2. the live Azure Retail Prices API (current Microsoft list price), then
  3. an authoritative Microsoft recommendation API (Reservation Recommendations, Advisor), then
  4. "Recommendation unavailable" — the finding is dropped rather than quantified from a guess.

The tool NEVER falls back to a hardcoded price, discount, or savings percentage. The dated
estimate tables that used to live here (snapshot per-GB, GRS vault, SQL AHB per-vCore, Load
Balancer / NAT Gateway / Bastion / App Service Plan fallbacks) were removed in the accuracy
audit because a hardcoded number can silently go stale or misrepresent a resource's real cost —
and a wrong number on a client deliverable is worse than showing nothing.

Only the disk *tier* breakpoints remain hardcoded (in pricing.py), because those are a stable
Azure capacity spec, not a price.
"""
from __future__ import annotations

# Kept only so existing imports of this symbol don't break; it no longer gates any figure.
ESTIMATES_VERIFIED = "retired"
