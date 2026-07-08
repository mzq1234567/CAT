export type AssessmentStatus = "pending" | "running" | "completed" | "failed";

export interface Subscription {
  id: string;
  display_name: string;
  state: string;
  tenant_id: string;
}

export interface Finding {
  id: number;
  category: string;
  display_name: string;
  resource_id: string | null;
  resource_name: string | null;
  subscription_id: string | null;
  resource_group: string | null;
  resource_type: string | null;
  estimated_savings_monthly: number;
  estimated_savings_annual: number;
  severity: "high" | "medium" | "low";
  description: string;
  recommendation: string;
  details: Record<string, unknown> | null;
}

export interface AssessmentSummary {
  id: number;
  user_email: string;
  subscription_ids: string[];
  status: AssessmentStatus;
  total_savings_monthly: number;
  total_savings_annual: number;
  findings_count: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface Assessment extends AssessmentSummary {
  findings: Finding[];
}

export interface FindingsByCategory {
  category: string;
  display_name: string;
  count: number;
  total_monthly: number;
  total_annual: number;
}
