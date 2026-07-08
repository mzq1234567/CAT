"""
Assessment finding evaluators.

Advisor-based findings: pull savings directly from Azure Advisor cost recommendations.
Inventory-based findings: detect orphaned resources using Resource Graph data.
"""
from typing import Dict, List, Optional, Tuple


CATEGORY_DISPLAY = {
    "ri_vm": "Reserved Instances (VM)",
    "savings_plan_vm": "Savings Plans (VM)",
    "orphaned_app_service_plans": "Orphaned App Service Plans",
    "orphaned_public_ips": "Orphaned Public IP Addresses",
    "orphaned_vms": "Orphaned Virtual Machines",
    "orphaned_managed_disks": "Orphaned Managed Disks",
    "backup_redundancy": "Backup Redundancy",
    "vm_rightsizing": "VM Rightsizing",
    "disk_rightsizing": "Disk Rightsizing",
    "managed_disk_reserved_capacity": "Managed Disk Reserved Capacity",
    "app_service_reserved_capacity": "App Service Reserved Capacity",
    "sql_db_reserved_capacity": "Azure SQL Database Reserved Capacity",
    "sql_mi_reserved_capacity": "Azure SQL Managed Instance Reserved Capacity",
    "azure_files_reserved_capacity": "Azure Files Reserved Capacity",
    "defender_dcu_prepurchase": "Microsoft Defender DCU Prepurchase",
    "backup_policy_review": "Backup Policy Review",
    "orphaned_sql_databases": "Orphaned Azure SQL Databases",
    "orphaned_sql_mi": "Orphaned SQL Managed Instances",
    "app_service_plan_rightsizing": "App Service Plan Rightsizing",
    "sql_db_rightsizing": "Azure SQL Database Rightsizing",
    "sql_mi_rightsizing": "SQL Managed Instance Rightsizing",
    "incremental_backup": "Differential/Incremental Backup Recommendation",
    "mysql_reserved_capacity": "MySQL Reserved Capacity",
    "cosmos_reserved_capacity": "Cosmos DB Reserved Capacity",
}

# Approximate monthly disk costs per GB (USD)
_DISK_COST_PER_GB = {
    "ultrassd_lrs": 0.125,
    "premium_v2_lrs": 0.17,
    "premiumv2_lrs": 0.17,
    "premium_lrs": 0.17,
    "standardssd_zrs": 0.12,
    "standardssd_lrs": 0.10,
    "standard_lrs": 0.045,
}

# Approximate monthly App Service Plan costs (USD) for lowest-capacity SKU
_ASP_COST = {
    "f1": 0, "d1": 9.49,
    "b1": 13.14, "b2": 26.28, "b3": 52.56,
    "s1": 56.94, "s2": 113.88, "s3": 227.76,
    "p1v2": 73.00, "p2v2": 146.00, "p3v2": 292.00,
    "p0v3": 37.23, "p1v3": 75.00, "p2v3": 150.00, "p3v3": 300.00,
    "i1": 240.00, "i2": 480.00, "i3": 960.00,
    "i1v2": 294.00, "i2v2": 588.00, "i3v2": 1176.00,
}


def _extract_savings(ext: Dict) -> Tuple[float, float]:
    monthly = float(ext.get("savingsAmount") or ext.get("monthlySavingsAmount") or 0)
    annual = float(ext.get("annualSavingsAmount") or 0)
    if annual == 0 and monthly > 0:
        annual = monthly * 12
    if monthly == 0 and annual > 0:
        monthly = annual / 12
    return monthly, annual


def _severity_from_savings(monthly: float, advisor_impact: str = "") -> str:
    if advisor_impact.lower() == "high" or monthly > 100:
        return "high"
    if advisor_impact.lower() == "medium" or monthly > 20:
        return "medium"
    return "low"


def _parse_ids(resource_id: str) -> Tuple[str, str]:
    """Return (subscription_id, resource_group) from an ARM resource ID."""
    parts = resource_id.split("/")
    sub = parts[2] if len(parts) > 2 and parts[1].lower() == "subscriptions" else ""
    rg = parts[4] if len(parts) > 4 and parts[3].lower() == "resourcegroups" else ""
    return sub, rg


def _classify_advisor(impacted_field: str, problem: str) -> Optional[str]:
    f = impacted_field.lower()
    p = problem.lower()

    if "virtualmachine" in f:
        if any(k in p for k in ["reserved virtual machine", "buy reserved", "reserved instance", "reserved capacity"]):
            return "ri_vm"
        if "savings plan" in p:
            return "savings_plan_vm"
        if any(k in p for k in ["right-size", "rightsize", "resize", "underutilized", "shut down", "shutdown"]):
            return "vm_rightsizing"

    if "savings plan" in p and "virtualmachine" not in f:
        return "savings_plan_vm"

    if "serverfarm" in f or "app service plan" in f:
        if any(k in p for k in ["reserved", "linux reserved"]):
            return "app_service_reserved_capacity"
        if any(k in p for k in ["right-size", "rightsize", "scale down", "downgrade", "underutilized"]):
            return "app_service_plan_rightsizing"

    if "managedinstance" in f:
        if any(k in p for k in ["reserved", "purchase"]):
            return "sql_mi_reserved_capacity"
        if any(k in p for k in ["right-size", "rightsize"]):
            return "sql_mi_rightsizing"

    if ("sql" in f and "database" in f) or "sql/servers/database" in f:
        if any(k in p for k in ["reserved", "purchase"]):
            return "sql_db_reserved_capacity"
        if any(k in p for k in ["right-size", "rightsize", "scale down"]):
            return "sql_db_rightsizing"

    if "disk" in f:
        if any(k in p for k in ["reserved capacity", "reserved instance", "purchase reserved"]):
            return "managed_disk_reserved_capacity"
        if any(k in p for k in ["right-size", "rightsize", "downsize", "premium to standard"]):
            return "disk_rightsizing"

    if "mysql" in f:
        if any(k in p for k in ["reserved", "purchase"]):
            return "mysql_reserved_capacity"

    if "documentdb" in f or "cosmos" in p:
        if any(k in p for k in ["reserved", "purchase"]):
            return "cosmos_reserved_capacity"

    if "storage" in f and "file" in p:
        if any(k in p for k in ["reserved", "purchase"]):
            return "azure_files_reserved_capacity"

    if "recoveryservices" in f or "backup" in p:
        if any(k in p for k in ["incremental", "differential"]):
            return "incremental_backup"
        if any(k in p for k in ["redundan", "lrs", "grs", "zrs"]):
            return "backup_redundancy"
        if any(k in p for k in ["policy", "retention"]):
            return "backup_policy_review"

    if "defender" in p or "microsoft.security" in f:
        if any(k in p for k in ["prepurchase", "pre-purchase", "commit", "dcu"]):
            return "defender_dcu_prepurchase"

    return None


def evaluate_advisor_findings(recommendations: List[Dict]) -> List[Dict]:
    findings = []
    seen_resource_ids: set = set()

    for rec in recommendations:
        props = rec.get("properties", {})
        ext = props.get("extendedProperties", {})
        short = props.get("shortDescription", {})
        meta = props.get("resourceMetadata", {})

        impacted_field = props.get("impactedField", "")
        problem = short.get("problem", "")
        category = _classify_advisor(impacted_field, problem)
        if not category:
            continue

        resource_id = meta.get("resourceId", "")
        # Deduplicate: same resource_id + category
        key = f"{category}:{resource_id}"
        if key in seen_resource_ids:
            continue
        seen_resource_ids.add(key)

        monthly, annual = _extract_savings(ext)
        impact = props.get("impact", "Medium")
        sub_id, rg = _parse_ids(resource_id) if resource_id else ("", "")

        findings.append({
            "category": category,
            "display_name": CATEGORY_DISPLAY.get(category, category),
            "resource_id": resource_id,
            "resource_name": props.get("impactedValue", resource_id.split("/")[-1] if resource_id else ""),
            "subscription_id": sub_id or props.get("subscriptionId", ""),
            "resource_group": rg,
            "resource_type": impacted_field,
            "estimated_savings_monthly": monthly,
            "estimated_savings_annual": annual,
            "severity": _severity_from_savings(monthly, impact),
            "description": problem,
            "recommendation": short.get("solution", "Follow Azure Advisor recommendation."),
            "details": {
                "advisor_recommendation_id": rec.get("id"),
                "impact": impact,
                "extended_properties": ext,
            },
        })

    return findings


# ── Inventory-based evaluators ──────────────────────────────────────────────

def evaluate_orphaned_managed_disks(disks: List[Dict]) -> List[Dict]:
    findings = []
    for disk in disks:
        if disk.get("diskState", "").lower() != "unattached":
            continue
        sku = (disk.get("skuName") or "standard_lrs").lower()
        size_gb = int(disk.get("diskSizeGB") or 0)
        price_per_gb = _DISK_COST_PER_GB.get(sku, 0.045)
        monthly = round(size_gb * price_per_gb, 2)
        findings.append({
            "category": "orphaned_managed_disks",
            "display_name": CATEGORY_DISPLAY["orphaned_managed_disks"],
            "resource_id": disk.get("id"),
            "resource_name": disk.get("name"),
            "subscription_id": disk.get("subscriptionId"),
            "resource_group": disk.get("resourceGroup"),
            "resource_type": "microsoft.compute/disks",
            "estimated_savings_monthly": monthly,
            "estimated_savings_annual": round(monthly * 12, 2),
            "severity": _severity_from_savings(monthly),
            "description": (
                f"Managed disk '{disk.get('name')}' ({size_gb} GB, {disk.get('skuName')}) "
                "is unattached and accruing storage costs without being used."
            ),
            "recommendation": "Delete this disk if it is no longer needed, or re-attach it to a virtual machine.",
            "details": disk,
        })
    return findings


def evaluate_orphaned_public_ips(public_ips: List[Dict]) -> List[Dict]:
    findings = []
    for pip in public_ips:
        if pip.get("associated"):
            continue
        # Static unassociated Standard IPs ≈ $3.65/mo; Basic ≈ $0.004/hr = $2.88/mo
        sku = (pip.get("skuName") or "Basic").lower()
        monthly = 3.65 if sku == "standard" else 2.88
        findings.append({
            "category": "orphaned_public_ips",
            "display_name": CATEGORY_DISPLAY["orphaned_public_ips"],
            "resource_id": pip.get("id"),
            "resource_name": pip.get("name"),
            "subscription_id": pip.get("subscriptionId"),
            "resource_group": pip.get("resourceGroup"),
            "resource_type": "microsoft.network/publicipaddresses",
            "estimated_savings_monthly": monthly,
            "estimated_savings_annual": round(monthly * 12, 2),
            "severity": "low",
            "description": (
                f"Public IP address '{pip.get('name')}' is not associated with any resource "
                "and is generating idle charges."
            ),
            "recommendation": "Delete this Public IP address if it is no longer needed.",
            "details": pip,
        })
    return findings


def evaluate_orphaned_app_service_plans(plans: List[Dict]) -> List[Dict]:
    findings = []
    for plan in plans:
        sites = plan.get("numberOfSites")
        if sites is None or sites > 0:
            continue
        sku = (plan.get("skuName") or "").lower()
        monthly = _ASP_COST.get(sku, 0)
        findings.append({
            "category": "orphaned_app_service_plans",
            "display_name": CATEGORY_DISPLAY["orphaned_app_service_plans"],
            "resource_id": plan.get("id"),
            "resource_name": plan.get("name"),
            "subscription_id": plan.get("subscriptionId"),
            "resource_group": plan.get("resourceGroup"),
            "resource_type": "microsoft.web/serverfarms",
            "estimated_savings_monthly": monthly,
            "estimated_savings_annual": round(monthly * 12, 2),
            "severity": _severity_from_savings(monthly),
            "description": (
                f"App Service Plan '{plan.get('name')}' ({plan.get('skuName')}) "
                "has no deployed apps and is incurring idle compute charges."
            ),
            "recommendation": "Delete this App Service Plan or deploy applications to it.",
            "details": plan,
        })
    return findings


def evaluate_orphaned_vms(vms: List[Dict]) -> List[Dict]:
    findings = []
    for vm in vms:
        state = (vm.get("powerState") or "").lower()
        if "deallocated" not in state and "stopped" not in state:
            continue
        findings.append({
            "category": "orphaned_vms",
            "display_name": CATEGORY_DISPLAY["orphaned_vms"],
            "resource_id": vm.get("id"),
            "resource_name": vm.get("name"),
            "subscription_id": vm.get("subscriptionId"),
            "resource_group": vm.get("resourceGroup"),
            "resource_type": "microsoft.compute/virtualmachines",
            "estimated_savings_monthly": 0.0,
            "estimated_savings_annual": 0.0,
            "severity": "medium",
            "description": (
                f"Virtual machine '{vm.get('name')}' ({vm.get('vmSize')}) is deallocated. "
                "Compute charges are not incurred but OS disks and attached data disks still accrue storage costs."
            ),
            "recommendation": (
                "If this VM is no longer needed, delete it along with its OS disk and attached data disks "
                "to fully eliminate costs. If it is needed, consider using Azure Reserved Instances."
            ),
            "details": vm,
        })
    return findings


def evaluate_orphaned_sql_databases(databases: List[Dict]) -> List[Dict]:
    findings = []
    for db in databases:
        status = (db.get("status") or "").lower()
        if status not in ("paused", "inaccessible", "disabled", "offline"):
            continue
        findings.append({
            "category": "orphaned_sql_databases",
            "display_name": CATEGORY_DISPLAY["orphaned_sql_databases"],
            "resource_id": db.get("id"),
            "resource_name": db.get("name"),
            "subscription_id": db.get("subscriptionId"),
            "resource_group": db.get("resourceGroup"),
            "resource_type": "microsoft.sql/servers/databases",
            "estimated_savings_monthly": 0.0,
            "estimated_savings_annual": 0.0,
            "severity": "medium",
            "description": (
                f"Azure SQL Database '{db.get('name')}' is in '{db.get('status')}' state. "
                "Storage costs continue to accrue even when the database is not active."
            ),
            "recommendation": "Delete the database if it is no longer needed, or archive its data to Blob Storage.",
            "details": db,
        })
    return findings


def evaluate_orphaned_sql_mi(instances: List[Dict]) -> List[Dict]:
    findings = []
    for mi in instances:
        state = (mi.get("state") or "").lower()
        if state not in ("stopped", "failed", "inaccessible"):
            continue
        findings.append({
            "category": "orphaned_sql_mi",
            "display_name": CATEGORY_DISPLAY["orphaned_sql_mi"],
            "resource_id": mi.get("id"),
            "resource_name": mi.get("name"),
            "subscription_id": mi.get("subscriptionId"),
            "resource_group": mi.get("resourceGroup"),
            "resource_type": "microsoft.sql/managedinstances",
            "estimated_savings_monthly": 0.0,
            "estimated_savings_annual": 0.0,
            "severity": "high",
            "description": (
                f"SQL Managed Instance '{mi.get('name')}' is in '{mi.get('state')}' state. "
                "SQL Managed Instance is billed for vCores continuously regardless of usage."
            ),
            "recommendation": "Delete this SQL Managed Instance if it is no longer needed.",
            "details": mi,
        })
    return findings


def run_all_inventory_findings(inventory: Dict[str, List[Dict]]) -> List[Dict]:
    findings: List[Dict] = []
    findings += evaluate_orphaned_managed_disks(inventory.get("managed_disks", []))
    findings += evaluate_orphaned_public_ips(inventory.get("public_ips", []))
    findings += evaluate_orphaned_app_service_plans(inventory.get("app_service_plans", []))
    findings += evaluate_orphaned_vms(inventory.get("virtual_machines", []))
    findings += evaluate_orphaned_sql_databases(inventory.get("sql_databases", []))
    findings += evaluate_orphaned_sql_mi(inventory.get("sql_managed_instances", []))
    return findings
