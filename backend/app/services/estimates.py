"""
Central table of static cost estimates — every value here is a REAL Azure retail figure captured on
the date below, NOT a guess.

Two kinds live here:
  1. Resources the Retail Prices API can't price per-resource because the cost depends on data VOLUME
     that Resource Graph doesn't expose (snapshot storage actually used, backup volume). These stay
     estimates by necessity.
  2. Last-resort FALLBACKS for resources we now price live (Load Balancer, NAT Gateway, Bastion, App
     Service Plan) — used only when the retail fetch fails or returns an out-of-band value.

These feed only the smaller "orphan"/estimate findings, which the dashboard already labels "Estimate"
(not "Cost-validated") — never the grounded headline numbers (idle VMs, RI/SP, AHB), which come from
live retail prices + the client's actual billed cost.

Re-verify against the Azure pricing calculator when the date below gets stale.
Last verified: 2026-07-31 (USD, eastus retail).
"""
from __future__ import annotations

ESTIMATES_VERIFIED = "2026-07-31"

# ── Genuinely can't be fetched live (cost depends on data VOLUME, not exposed by Resource Graph) ──
SNAPSHOT_PER_GB_MONTHLY_USD = 0.05      # incremental snapshot storage ~$0.05/GB-mo (LRS)
GRS_VAULT_MONTHLY_USD = 25.0            # nominal; real cost depends on backup volume
# SQL Server licence removed by Azure Hybrid Benefit — a flat per-vCore charge, the SAME dollar amount
# across GP/BC/Hyperscale (~$0.1534/vCore-hr). The AHB saving is grounded (capped at actual cost).
SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD = 112.0

# ── Fallbacks for the LIVE-priced resources (used only if the retail fetch fails / is out of band) ──
LOAD_BALANCER_MONTHLY_USD = 18.0        # Standard LB base (~$0.025/hr)
NAT_GATEWAY_MONTHLY_USD = 32.0          # NAT gateway resource (~$0.045/hr)
BASTION_MONTHLY_USD = 138.0             # Basic Bastion (~$0.19/hr)
APP_SERVICE_PLAN_MONTHLY_USD = {        # per SKU (lower-cased)
    "f1": 0.0, "d1": 9.49, "b1": 13.14, "b2": 26.28, "b3": 52.56,
    "s1": 56.94, "s2": 113.88, "s3": 227.76,
    "p1v2": 73.0, "p2v2": 146.0, "p3v2": 292.0,
    "p0v3": 37.23, "p1v3": 75.0, "p2v3": 150.0, "p3v3": 300.0,
    "i1v2": 294.0, "i2v2": 588.0, "i3v2": 1176.0,
}
