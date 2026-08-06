import { AssessmentStatus } from "../../types";

/**
 * One caption per backend stage.
 *
 * These describe progress in the customer's language, never the implementation. The loading screen
 * must not name Azure Advisor, Resource Graph, Cost Management, the retail price API or any other
 * internal dependency — the product is an assessment engine, not a wrapper around those services,
 * and naming them on screen both leaks architecture and cheapens the result.
 *
 * `at` is the progress value the backend reports on entering the stage; the bar may drift toward the
 * next stage's value but never past it, so motion never overstates real progress.
 */
export interface Stage {
  key: AssessmentStatus;
  caption: string;
  at: number;
}

export const STAGES: Stage[] = [
  { key: "fetching_resources", caption: "Discovering Azure resources", at: 15 },
  { key: "fetching_metrics", caption: "Sampling utilisation metrics", at: 35 },
  { key: "running_advisor", caption: "Reviewing optimisation signals", at: 55 },
  { key: "calculating_prices", caption: "Reading billing information", at: 70 },
  { key: "detecting_findings", caption: "Calculating optimisation opportunities", at: 85 },
  { key: "generating_report", caption: "Preparing your assessment", at: 95 },
];

export function stageIndexFor(status: AssessmentStatus): number {
  return STAGES.findIndex((s) => s.key === status);
}

/** The single caption shown for the current backend state. */
export function captionFor(status: AssessmentStatus): string {
  if (status === "queued") return "Connecting securely to Azure";
  if (status === "completed") return "Finalising results";
  return STAGES.find((s) => s.key === status)?.caption ?? "Preparing your assessment";
}
