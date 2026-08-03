/**
 * Groups the backend's real finding categories into a small set of executive-friendly areas.
 * Used only for the "savings by area" view — every number behind it comes from real findings.
 */
export type Area = "Compute" | "Storage" | "Databases" | "Network" | "Other";

export const AREAS: Area[] = ["Compute", "Storage", "Databases", "Network", "Other"];

const CATEGORY_TO_AREA: Record<string, Area> = {
  // Compute
  idle_vms: "Compute",
  oversized_vms: "Compute",
  ri_vm: "Compute",
  savings_plan_vm: "Compute",
  vm_rightsizing: "Compute",
  deallocated_vms: "Compute",
  windows_ahb: "Compute",
  idle_app_service_plans: "Compute",
  app_service_reserved_capacity: "Compute",
  app_service_plan_rightsizing: "Compute",
  // Storage
  unattached_managed_disks: "Storage",
  managed_disk_reserved_capacity: "Storage",
  azure_files_reserved_capacity: "Storage",
  disk_rightsizing: "Storage",
  backup_redundancy: "Storage",
  backup_policy_review: "Storage",
  incremental_backup: "Storage",
  // Databases
  paused_sql_databases: "Databases",
  stopped_sql_managed_instances: "Databases",
  sql_ahb: "Databases",
  sql_db_reserved_capacity: "Databases",
  sql_mi_reserved_capacity: "Databases",
  sql_db_rightsizing: "Databases",
  sql_mi_rightsizing: "Databases",
  cosmos_reserved_capacity: "Databases",
  mysql_reserved_capacity: "Databases",
  // Network
  orphaned_public_ips: "Network",
  empty_load_balancers: "Network",
  idle_nat_gateways: "Network",
  bastion_hosts: "Network",
  // Storage
  orphaned_snapshots: "Storage",
};

export function areaForCategory(category: string): Area {
  return CATEGORY_TO_AREA[category] ?? "Other";
}

import type { Finding } from "../../types";

export interface AreaRollup {
  area: Area;
  savings: number;
  count: number;
}

/** Group findings into areas, summing annual savings; sorted by savings desc. */
export function rollupByArea(findings: Finding[]): AreaRollup[] {
  const map = new Map<Area, AreaRollup>();
  for (const f of findings) {
    const a = areaForCategory(f.category);
    const cur = map.get(a) ?? { area: a, savings: 0, count: 0 };
    cur.savings += f.estimated_savings_annual;
    cur.count += 1;
    map.set(a, cur);
  }
  return [...map.values()].sort((x, y) => y.savings - x.savings);
}
