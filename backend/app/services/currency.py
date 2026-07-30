"""
Currency conversion for the few figures that are authored in USD.

Most numbers in the tool are already in the subscription's billing currency — the pricing engine
fetches Azure retail prices with `currencyCode`, and Cost Management returns the billing currency.
But a handful of values are hardcoded in USD:
  * the severity thresholds (critical/high/medium/low bands), and
  * nominal fallback estimates for resources that aren't cleanly retail-priced (empty load balancer,
    idle NAT gateway, Bastion, snapshot $/GB, GRS vault, App Service Plan tiers, IP/disk fallbacks).

Applying a USD threshold to an INR amount would mark almost everything "critical" (₹300 ≈ $3.6), and a
"$32" NAT-gateway estimate shown as "₹32" is 80× too low. These helpers convert so both read correctly.

The rates are a static reference table (approximate, refreshed occasionally). They only drive rough
severity banding and small nominal estimates — never the grounded savings from real prices/bills — so
minor FX drift is immaterial. Unknown currencies fall back to 1.0 (treated as USD-comparable).
"""
from __future__ import annotations

# USD value of one unit of each currency (e.g. 1 INR ≈ 0.012 USD).
_USD_PER_UNIT = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "INR": 0.012, "JPY": 0.0064, "SGD": 0.74, "AED": 0.27, "CHF": 1.12,
    "NZD": 0.61, "ZAR": 0.055, "BRL": 0.18, "MXN": 0.058, "SEK": 0.095,
}


# Display symbols (browser renders these fine; the PDF uses its own Helvetica-safe set in report.py).
_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "INR": "₹",
           "JPY": "¥", "SGD": "S$", "AED": "AED ", "CHF": "CHF ", "NZD": "NZ$", "ZAR": "R",
           "BRL": "R$", "MXN": "MX$", "SEK": "kr "}


def symbol(currency: str) -> str:
    """Currency symbol for finding text (falls back to the code + space)."""
    code = (currency or "USD").upper()
    return _SYMBOL.get(code, code + " ")


def to_usd(amount: float, currency: str) -> float:
    """Convert an amount in `currency` to a USD-equivalent (for severity banding)."""
    return amount * _USD_PER_UNIT.get((currency or "USD").upper(), 1.0)


def from_usd(usd_amount: float, currency: str) -> float:
    """Convert a USD-authored figure into `currency` (for nominal fallback estimates)."""
    rate = _USD_PER_UNIT.get((currency or "USD").upper(), 1.0)
    return round(usd_amount / rate, 2) if rate else usd_amount
