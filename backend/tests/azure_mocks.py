"""
Reusable Azure API mocks for tests.

Built on httpx.MockTransport so we exercise the real request/response/pagination code paths
without any network or extra mocking dependency. Extended in later steps for Resource Graph,
Advisor, and Cost Management.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

import httpx


# ── Azure Retail Prices API ────────────────────────────────────────────────────

# Sample items shaped like real Retail Prices API responses.
VM_ITEMS: List[Dict] = [
    {
        "currencyCode": "USD", "retailPrice": 0.096, "unitPrice": 0.096,
        "armRegionName": "eastus", "location": "US East",
        "meterName": "D2s v3", "productName": "Virtual Machines Dv3 Series",
        "skuName": "D2s v3", "serviceName": "Virtual Machines", "serviceFamily": "Compute",
        "unitOfMeasure": "1 Hour", "type": "Consumption", "armSkuName": "Standard_D2s_v3",
        "savingsPlan": [
            {"term": "1 Year", "retailPrice": 0.06, "unitPrice": 0.06},
            {"term": "3 Years", "retailPrice": 0.05, "unitPrice": 0.05},
        ],
    },
    {   # Windows variant — must be excluded from Linux base price.
        "currencyCode": "USD", "retailPrice": 0.188, "unitPrice": 0.188,
        "armRegionName": "eastus", "location": "US East",
        "meterName": "D2s v3", "productName": "Virtual Machines Dv3 Series Windows",
        "skuName": "D2s v3", "serviceName": "Virtual Machines", "serviceFamily": "Compute",
        "unitOfMeasure": "1 Hour", "type": "Consumption", "armSkuName": "Standard_D2s_v3",
    },
    {   # Spot variant — must be excluded.
        "currencyCode": "USD", "retailPrice": 0.019, "unitPrice": 0.019,
        "armRegionName": "eastus", "location": "US East",
        "meterName": "D2s v3 Spot", "productName": "Virtual Machines Dv3 Series",
        "skuName": "D2s v3", "serviceName": "Virtual Machines", "serviceFamily": "Compute",
        "unitOfMeasure": "1 Hour", "type": "Consumption", "armSkuName": "Standard_D2s_v3",
    },
]

VM_RESERVATION_ITEMS: List[Dict] = [
    {
        "currencyCode": "USD", "retailPrice": 840.0, "unitPrice": 840.0,
        "armRegionName": "eastus", "armSkuName": "Standard_D2s_v3",
        "serviceName": "Virtual Machines", "productName": "Virtual Machines Dv3 Series",
        "type": "Reservation", "reservationTerm": "1 Year", "unitOfMeasure": "1 Hour",
    },
    {
        "currencyCode": "USD", "retailPrice": 1800.0, "unitPrice": 1800.0,
        "armRegionName": "eastus", "armSkuName": "Standard_D2s_v3",
        "serviceName": "Virtual Machines", "productName": "Virtual Machines Dv3 Series",
        "type": "Reservation", "reservationTerm": "3 Years", "unitOfMeasure": "1 Hour",
    },
]

DISK_ITEMS: List[Dict] = [
    {
        "currencyCode": "USD", "retailPrice": 19.71, "unitPrice": 19.71,
        "armRegionName": "eastus", "skuName": "P10 LRS",
        "serviceName": "Storage", "productName": "Premium SSD Managed Disks",
        "meterName": "P10 LRS Disk", "unitOfMeasure": "1/Month", "type": "Consumption",
    },
]

PUBLIC_IP_ITEMS: List[Dict] = [
    {
        "currencyCode": "USD", "retailPrice": 0.005, "unitPrice": 0.005,
        "armRegionName": "eastus", "meterName": "Standard IP Address Hours",
        "serviceName": "Virtual Network", "productName": "IP Addresses",
        "unitOfMeasure": "1 Hour", "type": "Consumption",
    },
]


# ── Azure Resource Graph ────────────────────────────────────────────────────────

ARG_UNATTACHED_DISK = {
    "id": "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.compute/disks/disk-1",
    "name": "disk-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a", "location": "eastus",
    "diskState": "Unattached", "skuName": "Premium_LRS", "diskSizeGB": 128,
    "timeCreated": "2025-01-01T00:00:00Z", "tags": {},
}
ARG_ORPHANED_IP = {
    "id": "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.network/publicipaddresses/ip-1",
    "name": "ip-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a", "location": "eastus",
    "ipAddress": "20.1.2.3", "allocationMethod": "Static", "skuName": "Standard", "tags": {},
}
ARG_RUNNING_VM = {
    "id": "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.compute/virtualmachines/vm-1",
    "name": "vm-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a", "location": "eastus",
    "vmSize": "Standard_D2s_v3", "osType": "Linux", "powerState": "VM running", "tags": {},
}


def resource_graph_router_handler(
    rows_by_marker: Dict[str, List[Dict]],
    fail_markers: Optional[List[str]] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route an ARG POST to rows based on a substring present in the KQL query.

    `rows_by_marker` is checked in insertion order (first match wins). `fail_markers` makes any
    query containing that substring raise a ConnectError (to test per-bucket failure isolation).
    """
    fail_markers = fail_markers or []

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content.decode())["query"]
        for marker in fail_markers:
            if marker in query:
                raise httpx.ConnectError("simulated ARG failure", request=request)
        for marker, rows in rows_by_marker.items():
            if marker in query:
                return httpx.Response(200, json={"data": rows})
        return httpx.Response(200, json={"data": []})

    return handler


def resource_graph_paginated_handler(
    pages: List[List[Dict]],
    request_counter: Optional[List[int]] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return successive pages of ARG rows, emitting `$skipToken` until the final page."""
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request_counter is not None:
            request_counter[0] += 1
        i = state["i"]
        state["i"] += 1
        data = pages[i] if i < len(pages) else []
        resp: Dict = {"data": data}
        if i < len(pages) - 1:
            resp["$skipToken"] = f"tok{i + 1}"
        return httpx.Response(200, json=resp)

    return handler


# ── Azure Monitor metrics ────────────────────────────────────────────────────────

def metrics_handler(
    values: List[float], max_values: Optional[List[float]] = None, status: int = 200,
    memory_available_values: Optional[List[float]] = None, memory_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock Azure Monitor metrics for CPU ("Percentage CPU") and memory ("Available Memory
    Percentage"), routed by `metricnames` + `aggregation` query params.

    CPU: `values` for Average, `max_values` (defaults to `values`) for Maximum.
    Memory: `memory_available_values` for Minimum — omit/None to simulate "no memory data".
    """
    max_values = max_values if max_values is not None else values

    def handler(request: httpx.Request) -> httpx.Response:
        metric_name = request.url.params.get("metricnames", "")
        aggregation = request.url.params.get("aggregation", "Average")
        key = aggregation.lower()

        if metric_name == "Available Memory Percentage":
            if memory_status != 200:
                return httpx.Response(memory_status, json={"error": {"message": "no memory metrics"}})
            if memory_available_values is None:
                return httpx.Response(200, json={"value": [{"timeseries": [{"data": []}]}]})
            data = [{key: v} for v in memory_available_values]
            return httpx.Response(200, json={"value": [{"timeseries": [{"data": data}]}]})

        if status != 200:
            return httpx.Response(status, json={"error": {"message": "no metrics"}})
        series = max_values if aggregation == "Maximum" else values
        data = [{"timeStamp": f"2025-01-{i + 1:02d}T00:00:00Z", key: v} for i, v in enumerate(series)]
        return httpx.Response(200, json={"value": [{"timeseries": [{"data": data}]}]})

    return handler


# ── Azure Cost Management ────────────────────────────────────────────────────────

COST_COLUMNS = [
    {"name": "Cost", "type": "Number"},
    {"name": "ResourceId", "type": "String"},
    {"name": "Currency", "type": "String"},
]


def cost_management_handler(
    rows: List[List],
    columns: Optional[List[Dict]] = None,
    status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock the Cost Management query endpoint with a single page of rows."""
    cols = columns if columns is not None else COST_COLUMNS

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "no access"}})
        return httpx.Response(200, json={"properties": {"columns": cols, "rows": rows}})

    return handler


def _filter_for(request: httpx.Request) -> str:
    return request.url.params.get("$filter", "")


def _filter_value(flt: str, field: str) -> Optional[str]:
    """Extract the value of `<field> eq '<value>'` from an OData filter string."""
    needle = f"{field} eq '"
    start = flt.find(needle)
    if start == -1:
        return None
    start += len(needle)
    end = flt.find("'", start)
    return flt[start:end] if end != -1 else None


def retail_prices_handler(
    request_counter: Optional[List[int]] = None,
    fail: Optional[Callable[[], bool]] = None,
    paginate_vms: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler for the Retail Prices API.

    - `request_counter`: a one-element list incremented per HTTP call (assert caching).
    - `fail`: callable returning True to simulate the API being down (raises ConnectError).
    - `paginate_vms`: split VM consumption items across two pages via NextPageLink.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request_counter is not None:
            request_counter[0] += 1
        if fail is not None and fail():
            raise httpx.ConnectError("simulated API outage", request=request)

        # Second page of a paginated VM response.
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"Items": VM_ITEMS[1:], "NextPageLink": None})

        flt = _filter_for(request)
        # Real Azure scopes by armSkuName server-side; mirror that so a nonexistent SKU
        # yields no items (the engine trusts the server-side filter).
        sku = _filter_value(flt, "armSkuName")
        if "Virtual Machines" in flt and sku not in (None, "Standard_D2s_v3"):
            return httpx.Response(200, json={"Items": [], "NextPageLink": None})
        if "Virtual Machines" in flt and "Reservation" in flt:
            term = "3 Years" if "3 Years" in flt else "1 Year"
            items = [i for i in VM_RESERVATION_ITEMS if i["reservationTerm"] == term]
            return httpx.Response(200, json={"Items": items, "NextPageLink": None})
        if "Virtual Machines" in flt:
            if paginate_vms:
                next_link = str(request.url.copy_set_param("page", "2"))
                return httpx.Response(200, json={"Items": VM_ITEMS[:1], "NextPageLink": next_link})
            return httpx.Response(200, json={"Items": VM_ITEMS, "NextPageLink": None})
        if "Storage" in flt:
            return httpx.Response(200, json={"Items": DISK_ITEMS, "NextPageLink": None})
        if "Virtual Network" in flt:
            return httpx.Response(200, json={"Items": PUBLIC_IP_ITEMS, "NextPageLink": None})
        return httpx.Response(200, json={"Items": [], "NextPageLink": None})

    return handler
