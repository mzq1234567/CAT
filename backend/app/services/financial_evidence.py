"""
Financial evidence model — the single source of truth for "can this number be shown as money?".

The tool's overriding rule: it must NEVER invent, assume, or fabricate a financial number. Every
finding therefore carries an explicit EVIDENCE STATE and every financial value carries an explicit
SOURCE TYPE, and the two are never silently interchangeable (a retail/list price is NOT actual billed
cost; a potential saving is NOT an identified saving; an estimate is NOT a validated saving).

Only QUANTIFIED, non-conditional savings may:
  * display a savings amount,
  * contribute to Total Identified Savings,
  * reduce Projected Spend.

REVIEW findings surface a legitimate optimisation signal we cannot price for THIS customer; they show
"Financial impact: Not quantified" (optionally a clearly-labelled reference list price) and are kept
out of every savings total. SUPPRESSED findings are dropped entirely.
"""
from __future__ import annotations

# ── Evidence states ────────────────────────────────────────────────────────────────
QUANTIFIED = "quantified"   # defensible, reproducible financial impact → may count toward totals
REVIEW = "review"           # real signal, but customer financial impact cannot be reliably calculated
SUPPRESSED = "suppressed"   # not enough evidence to make a defensible recommendation → not shown

EVIDENCE_STATES = frozenset({QUANTIFIED, REVIEW, SUPPRESSED})


# ── Value source types (never silently interchangeable) ─────────────────────────────
# What a financial number actually IS. Used to tag every value we display so a reference/list price is
# never mistaken for actual spend, and a potential/estimated figure is never mistaken for a validated one.
ACTUAL_BILLED_COST = "ACTUAL_BILLED_COST"                       # from Cost Management (a real bill)
AUTHORITATIVE_RETAIL_PRICE = "AUTHORITATIVE_RETAIL_PRICE"       # live Retail Prices API list rate (reference)
AUTHORITATIVE_RESERVATION_PRICE = "AUTHORITATIVE_RESERVATION_PRICE"  # Azure reservation engine (real usage)
AZURE_ADVISOR_ESTIMATE = "AZURE_ADVISOR_ESTIMATE"              # Azure Advisor's own savings estimate
ESTIMATED_ANNUALISED_COST = "ESTIMATED_ANNUALISED_COST"        # run-rate projection of partial billing
QUANTIFIED_SAVINGS = "QUANTIFIED_SAVINGS"                       # a defensible, countable saving
POTENTIAL_SAVINGS = "POTENTIAL_SAVINGS"                         # conditional (e.g. AHB — needs eligibility)
UNQUANTIFIED = "UNQUANTIFIED"                                   # no defensible number available


def counts_toward_total(evidence_state: str, conditional: bool) -> bool:
    """A finding contributes to Total Identified Savings / Projected Spend ONLY when its impact is
    QUANTIFIED and it is NOT conditional (conditional = AHB-style, realised only if eligibility holds).
    This is the one rule the persistence layer, the API roll-up and the dashboard all defer to."""
    return evidence_state == QUANTIFIED and not conditional
