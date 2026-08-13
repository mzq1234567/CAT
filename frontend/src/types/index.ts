// Assessment lifecycle states (mirror backend state machine — Step 5).
export type AssessmentStatus =
  | "queued"
  | "fetching_resources"
  | "fetching_metrics"
  | "running_advisor"
  | "calculating_prices"
  | "detecting_findings"
  | "generating_report"
  | "completed"
  | "failed";

export type Severity = "critical" | "high" | "medium" | "low";

export interface Subscription {
  id: string;
  display_name: string;
  state: string;
  tenant_id: string;
}

// Self-service readiness (Phase B). Each check is an honest probe of one data source in the
// signed-in user's context — "ok" | "warning" (degrades gracefully) | "unavailable" (blocking).
export type PreflightStatus = "ok" | "warning" | "unavailable";

export interface PreflightCheck {
  key: string;
  label: string;
  status: PreflightStatus;
  detail: string;
  blocking: boolean;
}

export interface PreflightResponse {
  subscription_id: string;
  subscription_name: string | null;
  tenant_id: string | null;
  ready: boolean;
  checks: PreflightCheck[];
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
  // Non-overlapping contribution to the total (RI vs right-sizing de-overlapped). Null → use estimated.
  // Aggregates (headline, donut, categories) sum these; individual cards show estimated_savings_*.
  counted_savings_monthly: number | null;
  counted_savings_annual: number | null;
  severity: Severity;
  confidence: number;
  description: string;
  recommendation: string;
  advisor_recommendation_id: string | null;
  validation_status: "validated" | "needs_review" | "unvalidated" | null;
  validation_variance_pct: number | null;
  actual_monthly_cost: number | null;
  // Financial evidence state: "quantified" (a defensible, countable saving) or "review" (a real signal
  // we can't price for this customer — shown as "Not quantified", never counted in any savings total).
  evidence_state: "quantified" | "review";
  // Canonical, client-safe "how this number was calculated" sentence (backend single source of truth,
  // shared with the PDF). Qualitative — the value source + method; money is formatted by the UI.
  basis: string | null;
  dismissed: boolean;
  // DEV-ONLY: populated only when backend DEBUG_FINDINGS_REASONING is enabled.
  debug_reason: string | null;
  details: Record<string, unknown> | null;
}

export interface AssessmentSummary {
  id: number;
  user_email: string;
  subscription_ids: string[];
  status: AssessmentStatus;
  progress: number;
  status_message: string | null;
  total_savings_monthly: number;
  total_savings_annual: number;
  findings_count: number;
  needs_review_count: number;
  total_resources: number | null;
  resource_type_count: number | null;
  // Real per-type counts from the scan, e.g. [{ type: "Virtual Machines", count: 16 }].
  major_resource_types: { type: string; count: number }[] | null;
  // Actual spend from Cost Management (null when billing access is unavailable)
  current_monthly_spend: number | null;
  current_annual_spend: number | null;
  spend_by_area: Record<string, number> | null;
  cost_data_available: boolean;
  // Per-resource billed cost was unavailable this run (Cost Management throttled) — grounded findings
  // were withheld and the UI shows a "re-run for accurate figures" banner.
  billing_detail_unavailable: boolean;
  // Azure data-collection quality: some Azure data could not be collected (throttle/error) when not
  // "complete". `data_quality_message` is the concise client line; missing data is never treated as zero.
  data_quality: "complete" | "partial" | "failed";
  data_quality_message: string | null;
  // Spend is an estimated run rate (new/migrated subscription, no complete billing month) over N days.
  spend_estimated: boolean;
  spend_period_days: number | null;
  currency: string;
  // Annual spend-growth rate from the recent trend (e.g. 0.12 = +12%/yr); null if too little history.
  observed_annual_growth: number | null;
  error_message: string | null;
  created_at: string;
  snapshot_at: string | null;
  completed_at: string | null;
}

/** One thing the pipeline actually did, emitted by the backend as it happened. */
export interface AssessmentEvent {
  id: number;
  timestamp: string;
  stage: string | null;
  message: string;
}

export interface Assessment extends AssessmentSummary {
  findings: Finding[];
  events: AssessmentEvent[];
}

export interface FindingsByCategory {
  category: string;
  display_name: string;
  count: number;
  total_monthly: number;
  total_annual: number;
}
