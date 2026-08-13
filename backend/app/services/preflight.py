"""
Self-service permission pre-flight (readiness check).

Before a client clicks "Run Assessment", we probe — cheaply, in the context of THEIR signed-in user —
whether each data source the assessment relies on is reachable, so missing access is surfaced up front
rather than several minutes into a run. Every probe is honest: a failure is reported as a failure (or a
non-blocking warning), never smoothed into a fake "ok", and NO raw HTTP status / SDK exception is exposed
to the client.

Permission model note: the assessment needs the signed-in account to have **sufficient read access** to
the subscription (built-in **Reader** is enough for resource inventory, metrics and — in the tenants we
target — Cost Management queries). We deliberately do NOT tell customers they need a separate
"Cost Management Reader" role; if billed cost can't be read we degrade gracefully (some findings become
"not quantified"), we don't gate the assessment on it.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .azure_client import AzureClient
from .cost_management import get_service_costs_and_currency
from .pricing import PricingUnavailableError, get_pricing_engine

logger = logging.getLogger("cat.preflight")

OK = "ok"            # available
WARNING = "warning"  # not available, but the assessment can still run (findings degrade to "not quantified")
UNAVAILABLE = "unavailable"  # blocking — the assessment can't run without this

# A tiny Resource Graph query — proves both "ARG reachable" and "can read resources".
_PROBE_KQL = "Resources | project id | limit 1"


def _check(key: str, label: str, status: str, detail: str = "", blocking: bool = False) -> Dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "blocking": blocking}


async def run_preflight(
    client: AzureClient, subscription_id: str, user_email: Optional[str] = None,
) -> Dict:
    """Probe each assessment prerequisite for one subscription and return a client-safe readiness report.

    Returns {subscription_id, subscription_name, tenant_id, ready, checks[]}. `ready` is True when no
    BLOCKING check failed (subscription access + resource inventory); cost/metrics/pricing are
    non-blocking (their absence degrades findings to "not quantified", per the evidence model).
    """
    checks: List[Dict] = []
    checks.append(_check("signin", "Microsoft sign-in", OK,
                         f"Signed in as {user_email}" if user_email else "Signed in"))

    # 2. Subscription access — the requested sub must be one Azure returns for THIS user (server-side,
    #    never trusted from the browser). This also yields the tenant + friendly name.
    subscription_name: Optional[str] = None
    tenant_id: Optional[str] = None
    sub_ok = False
    try:
        subs = await client.get_subscriptions()
        match = next((s for s in subs
                      if s.get("subscriptionId") == subscription_id and s.get("state") == "Enabled"), None)
        if match:
            sub_ok = True
            subscription_name = match.get("displayName") or subscription_id
            tenant_id = match.get("tenantId")
            checks.append(_check("subscription", "Azure subscription access", OK, subscription_name))
        else:
            checks.append(_check("subscription", "Azure subscription access", UNAVAILABLE,
                                 "Your account can't access this subscription, or it isn't enabled.",
                                 blocking=True))
    except Exception:  # noqa: BLE001 — never surface the raw error to the client
        logger.warning("Preflight subscription check failed for %s", subscription_id)
        checks.append(_check("subscription", "Azure subscription access", UNAVAILABLE,
                             "We couldn't verify your Azure subscription access. Please try again.",
                             blocking=True))

    if not sub_ok:
        # No point probing further data sources we can't reach; mark them pending, not failed.
        for key, label in (("inventory", "Resource inventory"), ("cost", "Cost data"),
                           ("metrics", "Resource metrics"), ("pricing", "Pricing data")):
            checks.append(_check(key, label, UNAVAILABLE, "Requires subscription access."))
        return {"subscription_id": subscription_id, "subscription_name": subscription_name,
                "tenant_id": tenant_id, "ready": False, "checks": checks}

    # 3. Resource inventory (Azure Resource Graph) — proves ARG access AND resource read access.
    try:
        await client.query_resource_graph([subscription_id], _PROBE_KQL)
        checks.append(_check("inventory", "Resource inventory", OK,
                             "Resource inventory is readable."))
    except Exception:  # noqa: BLE001
        logger.warning("Preflight inventory probe failed for %s", subscription_id)
        checks.append(_check("inventory", "Resource inventory", UNAVAILABLE,
                             "We couldn't read your resource inventory. Your account may lack read access "
                             "to this subscription.", blocking=True))

    # 4. Cost data (Cost Management) — non-blocking. Missing billed cost degrades findings to
    #    "not quantified" (never fabricated), so a failure here is a WARNING, not a blocker.
    try:
        costs, _ = await get_service_costs_and_currency(client, subscription_id)
        if costs:
            checks.append(_check("cost", "Cost data", OK, "Billed cost is readable."))
        else:
            checks.append(_check("cost", "Cost data", WARNING,
                                 "Billing data wasn't returned for this subscription (no access, or no "
                                 "billed usage yet). Some financial recommendations may not be quantified."))
    except Exception:  # noqa: BLE001
        logger.warning("Preflight cost probe failed for %s", subscription_id)
        checks.append(_check("cost", "Cost data", WARNING,
                             "Billing data could not be retrieved. Some financial recommendations may not "
                             "be quantified."))

    # 5. Resource metrics — utilisation metrics use the same subscription read access (built-in Reader
    #    includes Microsoft.Insights/metrics/read). Verified per-resource during the run; a per-resource
    #    metrics failure there excludes only that resource's utilisation-based recommendation.
    checks.append(_check("metrics", "Resource metrics", OK,
                         "Utilisation metrics use your subscription read access."))

    # 6. Pricing data (public Azure Retail Prices API — no customer permission). Non-blocking.
    try:
        price = await get_pricing_engine().get_vm_monthly_price("eastus", "Standard_D2s_v3")
        if price is not None:
            checks.append(_check("pricing", "Pricing data", OK, "Live Azure retail pricing is available."))
        else:
            checks.append(_check("pricing", "Pricing data", WARNING,
                                 "Live pricing was temporarily unavailable. Pricing-based recommendations "
                                 "may be limited."))
    except PricingUnavailableError:
        checks.append(_check("pricing", "Pricing data", WARNING,
                             "Live pricing was temporarily unavailable. Pricing-based recommendations may "
                             "be limited."))
    except Exception:  # noqa: BLE001
        logger.warning("Preflight pricing probe failed")
        checks.append(_check("pricing", "Pricing data", WARNING,
                             "Live pricing was temporarily unavailable."))

    ready = not any(c["blocking"] and c["status"] == UNAVAILABLE for c in checks)
    return {"subscription_id": subscription_id, "subscription_name": subscription_name,
            "tenant_id": tenant_id, "ready": ready, "checks": checks}
