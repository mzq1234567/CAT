"""
Canonical, client-safe explanation of HOW a finding's financial figure was derived.

This is the single source of truth for the "Why this number?" line, reused by the dashboard detail
drawer AND the PDF report so the two can never disagree. The sentence is deliberately QUALITATIVE
(value source + method + derivation caveat) and embeds no currency amounts — the caller formats money
in the customer's own currency. It never overstates certainty: a list/reference price is described as a
reference, an estimate as an estimate, a conditional saving as conditional.

Data-quality WARNINGS (a suspected sponsored-subscription anomaly, throttled billing) are NOT part of
this basis — those are surfaced separately as their own notes/banners in both the UI and the PDF, so
the basis stays a clean statement of provenance.
"""
from __future__ import annotations

from typing import Any, Optional

from .financial_evidence import ACTUAL_BILLED_COST, AZURE_ADVISOR_ESTIMATE, QUANTIFIED, REVIEW

# AHB is conditional (realised only with eligible licences) — kept in step with the rest of the engine.
_CONDITIONAL_CATEGORIES = frozenset({"windows_ahb", "sql_ahb"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict (in-run finding) or an ORM/pydantic object (persisted finding)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def describe_finding_basis(finding: Any) -> Optional[str]:
    """Return one plain-English sentence explaining how this finding's figure was calculated.

    Works on the in-run finding dict, the persisted `Finding` ORM row, and the `FindingResponse` schema
    (all expose evidence_state / category / validation_status / details). Returns None only when there
    is genuinely nothing to say.
    """
    details = _get(finding, "details", None) or {}
    state = _get(finding, "evidence_state", None) or QUANTIFIED
    category = _get(finding, "category", "") or ""
    conditional = category in _CONDITIONAL_CATEGORIES

    # ── REVIEW — a real signal we cannot price for THIS customer (never a saving) ────────────────
    if state == REVIEW:
        if details.get("sponsored_or_credited"):
            return (
                "This resource is billed at effectively zero — typically an Azure Sponsorship or credited "
                "subscription (or a billing-currency mismatch) — so a realistic saving can't be calculated "
                "from its bill. Any amount shown is Azure's published list price, for reference only."
            )
        if details.get("insufficient_billing_history"):
            days = details.get("billing_span_days")
            span = f"{int(days)} days" if isinstance(days, (int, float)) else "only a few days"
            return (
                f"Based on {span} of billing — too short a history to project a reliable annual figure, so "
                "this is shown for review rather than counted as a saving."
            )
        return (
            "We couldn't establish this resource's actual billed cost for your subscription, so no "
            "defensible saving can be calculated. Any amount shown is Azure's published list price, for "
            "reference only."
        )

    # ── QUANTIFIED — a defensible, countable figure. Describe its value source. ──────────────────
    savings_source = details.get("savings_source")
    if conditional:
        licence = "SQL Server" if category == "sql_ahb" else "Windows Server"
        base = (
            f"The licence share of each eligible resource's actual billed cost from Azure Cost Management — "
            f"realised only on resources covered by {licence} licences you already own"
        )
    elif details.get("source") == "azure_reservation_recommendations":
        base = (
            "Computed by Azure's own reservation engine from your actual usage at your real prices, "
            "excluding reservations you already hold"
        )
    elif savings_source == ACTUAL_BILLED_COST:
        base = (
            "This resource's actual billed cost from Azure Cost Management, removed entirely by "
            "deleting or stopping the idle resource"
        )
    elif details.get("savings_capped_at_actual_cost"):
        base = (
            "Based on live Azure retail pricing, then capped to this resource's actual billed cost from "
            "Azure Cost Management so the saving never exceeds what you actually pay"
        )
    elif _get(finding, "validation_status", None) == "validated":
        base = "Based on live Azure retail pricing and cross-checked against your actual billed cost"
    elif savings_source == AZURE_ADVISOR_ESTIMATE or category == "advisor_cost":
        base = "Azure Advisor's own savings estimate for this recommendation"
    else:
        base = "Based on live Azure retail pricing for the affected resources"

    # Derivation qualifiers (not warnings) — a floor on the total, and a partial-period estimate flag.
    if details.get("savings_capped_at_measured_spend"):
        base += ", and capped to your total measured spend for this scope"
    if not conditional and (details.get("cost_basis") == "run_rate" or details.get("cost_is_estimate")):
        base += " (estimated from a partial billing period — re-run after a full billing month to settle it)"

    return base.rstrip(".") + "."
