/**
 * The "consulting layer" for recommendations.
 *
 * The backend gives us hard facts (savings, description, recommendation, per-resource details). This
 * module adds the business framing a consultant would put around each finding — a one-line summary,
 * the business value, implementation effort, and prerequisites — plus helpers to count and list the
 * resources a finding affects (without dumping long lists by default).
 */
import type { Finding } from "../../types";

export type Effort = "Low" | "Medium" | "High";

export interface CategoryMeta {
  /** Short, plain-language summary for the card face (n = affected resource count). */
  summary: (n: number) => string;
  /** One line on why the business should care. */
  businessValue: string;
  effort: Effort;
  /** How the change is actually made — the effort in one sentence. */
  effortNote: string;
  /** A condition that must hold for the saving to be real, if any. */
  prerequisites?: string;
  /** Singular noun for the affected resource ("Virtual Machine"). */
  noun: string;
}

const NOUN: Record<string, string> = {
  windows_ahb: "Virtual Machine", ri_vm: "Virtual Machine", idle_vms: "Virtual Machine",
  oversized_vms: "Virtual Machine", vm_rightsizing: "Virtual Machine", deallocated_vms: "Virtual Machine",
  disk_rightsizing: "Managed Disk", unattached_managed_disks: "Managed Disk",
  managed_disk_reserved_capacity: "Managed Disk", orphaned_snapshots: "Snapshot",
  sql_db_rightsizing: "SQL Database", paused_sql_databases: "SQL Database",
  sql_db_reserved_capacity: "SQL Database", sql_mi_rightsizing: "SQL Managed Instance",
  stopped_sql_managed_instances: "SQL Managed Instance", sql_mi_reserved_capacity: "SQL Managed Instance",
  sql_ahb: "SQL resource", cosmos_reserved_capacity: "Cosmos DB", mysql_reserved_capacity: "Database",
  orphaned_public_ips: "Public IP", empty_load_balancers: "Load Balancer",
  idle_nat_gateways: "NAT Gateway", bastion_hosts: "Bastion Host",
  idle_app_service_plans: "App Service Plan", app_service_plan_rightsizing: "App Service Plan",
  app_service_reserved_capacity: "App Service", azure_files_reserved_capacity: "Storage account",
  backup_redundancy: "Backup vault", backup_policy_review: "Backup policy", incremental_backup: "Backup",
  advisor_cost: "Resource",
};

export function nounFor(category: string): string {
  return NOUN[category] || "Resource";
}

const plural = (noun: string, n: number) => `${noun}${n === 1 ? "" : noun.endsWith("s") ? "es" : "s"}`;

// A reusable "reserve capacity" profile for the reserved-capacity families.
const reserved = (noun: string): CategoryMeta => ({
  summary: (n) => `Reserve ${n} steady ${plural(noun, n)} to lock in a lower committed rate.`,
  businessValue: "Convert predictable, always-on usage to a committed rate — the biggest lever for steady workloads.",
  effort: "Low",
  effortNote: "A reservation purchase in the portal — no change to the running resources.",
  prerequisites: "A 1- or 3-year commitment; best for capacity you will keep running for the full term.",
  noun,
});

// A reusable "delete orphan" profile.
const orphan = (noun: string): CategoryMeta => ({
  summary: (n) => `${n} ${plural(noun, n)} ${n === 1 ? "is" : "are"} provisioned but serve nothing — pure waste.`,
  businessValue: "Remove resources that keep billing while delivering no value.",
  effort: "Low",
  effortNote: "Delete the orphaned resource after a quick dependency check.",
  prerequisites: "Confirm nothing references it before deleting.",
  noun,
});

// A reusable "rightsize" profile.
const rightsize = (noun: string): CategoryMeta => ({
  summary: (n) => `${n} over-provisioned ${plural(noun, n)} can be sized down to match real demand.`,
  businessValue: "Stop paying for capacity that peak demand never uses.",
  effort: "Medium",
  effortNote: "Resize during a maintenance window and validate performance afterwards.",
  prerequisites: "Confirm peak-load headroom; a brief restart may be required.",
  noun,
});

const META: Record<string, CategoryMeta> = {
  windows_ahb: {
    summary: (n) => `Apply Windows Server licences you already own to drop the licence charge on ${n} ${plural("VM", n)}.`,
    businessValue: "Cut Azure's Windows Server licence premium using licences you already hold — pure licence optimisation, no infrastructure change.",
    effort: "Low",
    effortNote: "Set licenseType=Windows_Server on each VM. No downtime, no resize.",
    prerequisites: "You must own eligible Windows Server licences with active Software Assurance (or subscription licences).",
    noun: "Virtual Machine",
  },
  sql_ahb: {
    summary: (n) => `Apply SQL Server licences you already own to drop the licence charge on ${n} ${plural("resource", n)}.`,
    businessValue: "Cut Azure's SQL Server licence premium using licences you already hold.",
    effort: "Low",
    effortNote: "Switch each SQL resource to Azure Hybrid Benefit licensing. No downtime.",
    prerequisites: "You must own eligible SQL Server licences with active Software Assurance.",
    noun: "SQL resource",
  },
  ri_vm: {
    summary: (n) => `Reserve ${n} steady production ${plural("VM", n)} to lock in a lower rate than pay-as-you-go.`,
    businessValue: "Convert predictable, always-on compute to a committed rate — the single biggest lever for steady workloads.",
    effort: "Low",
    effortNote: "A reservation purchase in the portal — the running VMs are untouched.",
    prerequisites: "A 1- or 3-year commitment; best for VMs you'll keep running. Untagged VMs are assumed production — verify first.",
    noun: "Virtual Machine",
  },
  idle_vms: {
    summary: (n) => `${n} ${plural("VM", n)} ${n === 1 ? "is" : "are"} running but effectively idle — paying for compute that does no work.`,
    businessValue: "Stop paying for compute that isn't doing anything — the cleanest saving there is.",
    effort: "Low",
    effortNote: "Deallocate (or delete) after confirming the VM is genuinely unused.",
    prerequisites: "Check it isn't a warm standby or a scheduled/burst workload before deallocating.",
    noun: "Virtual Machine",
  },
  deallocated_vms: {
    summary: (n) => `${n} deallocated ${plural("VM", n)} still bill for attached disks and IPs.`,
    businessValue: "Reclaim storage and network spend for machines that are switched off anyway.",
    effort: "Low",
    effortNote: "Delete unused disks/IPs, or delete the VM if it's no longer needed.",
    prerequisites: "Confirm the VM won't be restarted before removing its disks.",
    noun: "Virtual Machine",
  },
  oversized_vms: rightsize("Virtual Machine"),
  vm_rightsizing: rightsize("Virtual Machine"),
  disk_rightsizing: {
    summary: (n) => `${n} Premium ${plural("disk", n)} run well below their tier — Standard SSD would serve the same load for less.`,
    businessValue: "Match disk tier to real IOPS/throughput instead of paying for Premium headroom you don't use.",
    effort: "Medium",
    effortNote: "Change the disk SKU during a maintenance window; a brief detach/restart applies.",
    prerequisites: "Excludes latency-sensitive disks (e.g. SQL servers). Confirm the workload tolerates Standard SSD.",
    noun: "Managed Disk",
  },
  sql_db_rightsizing: rightsize("SQL Database"),
  sql_mi_rightsizing: rightsize("SQL Managed Instance"),
  app_service_plan_rightsizing: rightsize("App Service Plan"),
  unattached_managed_disks: orphan("Managed Disk"),
  orphaned_public_ips: orphan("Public IP"),
  orphaned_snapshots: orphan("Snapshot"),
  empty_load_balancers: orphan("Load Balancer"),
  idle_nat_gateways: orphan("NAT Gateway"),
  bastion_hosts: {
    summary: (n) => `${n} Bastion ${plural("host", n)} bill hourly regardless of use.`,
    businessValue: "Drop always-on Bastion charges where access is rare or can be gated another way.",
    effort: "Low",
    effortNote: "Remove the Bastion host, or switch to a just-in-time access pattern.",
    prerequisites: "Confirm no team relies on it for routine access.",
    noun: "Bastion Host",
  },
  idle_app_service_plans: {
    summary: (n) => `${n} App Service ${plural("plan", n)} host no running apps but keep billing.`,
    businessValue: "Stop paying for empty hosting plans.",
    effort: "Low",
    effortNote: "Delete the empty plan, or scale it to Free/Shared.",
    prerequisites: "Confirm no app is about to be deployed to it.",
    noun: "App Service Plan",
  },
  paused_sql_databases: {
    summary: (n) => `${n} SQL ${plural("database", n)} sit paused/inactive yet still incur cost.`,
    businessValue: "Reclaim spend on databases that aren't serving traffic.",
    effort: "Low",
    effortNote: "Move to serverless auto-pause, or remove if no longer needed.",
    prerequisites: "Confirm the database isn't needed for occasional/seasonal use.",
    noun: "SQL Database",
  },
  stopped_sql_managed_instances: {
    summary: (n) => `${n} SQL Managed ${plural("Instance", n)} are stopped but still reserve billable capacity.`,
    businessValue: "Stop paying reserved capacity for instances that aren't running.",
    effort: "Medium",
    effortNote: "Right-size or remove the instance once you confirm it's no longer needed.",
    prerequisites: "Confirm the instance isn't a planned-restart or DR resource.",
    noun: "SQL Managed Instance",
  },
  sql_db_reserved_capacity: reserved("SQL Database"),
  sql_mi_reserved_capacity: reserved("SQL Managed Instance"),
  managed_disk_reserved_capacity: reserved("Managed Disk"),
  cosmos_reserved_capacity: reserved("Cosmos DB"),
  mysql_reserved_capacity: reserved("Database"),
  app_service_reserved_capacity: reserved("App Service"),
  azure_files_reserved_capacity: reserved("Storage account"),
  backup_redundancy: {
    summary: (n) => `${n} backup ${plural("vault", n)} use geo-redundant storage where local redundancy may suffice.`,
    businessValue: "Right-size backup redundancy to real recovery requirements instead of defaulting to the priciest tier.",
    effort: "Low",
    effortNote: "Switch vault storage redundancy (GRS → LRS/ZRS) where the RPO allows.",
    prerequisites: "Confirm the recovery objective doesn't require cross-region durability.",
    noun: "Backup vault",
  },
  backup_policy_review: {
    summary: (n) => `${n} backup ${plural("policy", n)} retain more than the recovery objective needs.`,
    businessValue: "Trim over-long retention to what compliance and recovery actually require.",
    effort: "Low",
    effortNote: "Adjust retention on the backup policy.",
    prerequisites: "Confirm retention limits with compliance before reducing.",
    noun: "Backup policy",
  },
  incremental_backup: {
    summary: (n) => `${n} ${plural("backup", n)} could use incremental snapshots to cut storage cost.`,
    businessValue: "Reduce snapshot storage by capturing only changed data.",
    effort: "Low",
    effortNote: "Enable incremental snapshots on the affected resources.",
    noun: "Backup",
  },
  advisor_cost: {
    summary: (n) => `Azure Advisor flags ${n} cost ${plural("optimisation", n)} on your resources.`,
    businessValue: "Act on Microsoft's own cost guidance for your environment.",
    effort: "Medium",
    effortNote: "Follow the Advisor recommendation for each resource.",
    noun: "Resource",
  },
};

export function metaFor(category: string): CategoryMeta {
  return (
    META[category] || {
      summary: (n) => `${n} ${plural(nounFor(category), n)} flagged for cost optimisation.`,
      businessValue: "Reduce spend on resources that cost more than they need to.",
      effort: "Medium",
      effortNote: "Review the recommended change and apply during a maintenance window.",
      noun: nounFor(category),
    }
  );
}

const EFFORT_ORDER: Record<Effort, number> = { Low: 0, Medium: 1, High: 2 };
export function effortRank(e: Effort): number {
  return EFFORT_ORDER[e];
}

export interface AffectedResource {
  name: string;
  sku?: string;
  region?: string;
  monthly_savings?: number;
}

/**
 * Resources a finding affects, WITHOUT dumping the list into the UI by default: callers show the
 * `count` (e.g. "12 Virtual Machines") and reveal `items` only on request.
 */
export function affectedResources(finding: Finding): { count: number; items: AffectedResource[] } {
  const d = (finding.details || {}) as Record<string, unknown>;
  const eligible = d.eligible_vms as AffectedResource[] | undefined;
  if (Array.isArray(eligible) && eligible.length) {
    return { count: (d.eligible_count as number) ?? eligible.length, items: eligible };
  }
  const items = d.reservation_items as (AffectedResource & { sku?: string })[] | undefined;
  if (Array.isArray(items) && items.length) {
    const mapped = items.map((v) => ({ name: v.name || v.sku || "—", sku: v.sku, region: v.region, monthly_savings: v.monthly_savings }));
    return { count: (d.item_count as number) ?? mapped.length, items: mapped };
  }
  if (finding.resource_name) {
    return {
      count: 1,
      items: [{
        name: finding.resource_name,
        sku: finding.resource_type || undefined,
        region: finding.resource_group || undefined,
        monthly_savings: finding.estimated_savings_monthly,
      }],
    };
  }
  return { count: 0, items: [] };
}

/** "12 Virtual Machines" / "1 Managed Disk" — the affected-resources label. */
export function affectedLabel(finding: Finding): string {
  const { count } = affectedResources(finding);
  const noun = nounFor(finding.category);
  return `${count} ${plural(noun, count)}`;
}
