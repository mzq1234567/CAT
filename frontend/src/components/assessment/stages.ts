import { AssessmentStatus } from "../../types";

/**
 * Copy for the running-assessment screen — always in the customer's language, never the implementation.
 *
 * The loading screen must not name Azure Advisor, Resource Graph, Cost Management, the retail price API,
 * tokens, retries, pagination or any other internal dependency — the product is an assessment engine,
 * not a wrapper around those services, and naming them on screen both leaks architecture and cheapens
 * the result.
 *
 * `caption` is the primary line and is bound 1:1 to the backend's real `status`. `sublines` are secondary
 * descriptors that rotate WHILE a stage holds, so a longer assessment reads as continuous progress rather
 * than a frozen line — without ever fabricating progress (the bar still only advances when the backend
 * confirms the stage changed). `at` is the progress value the backend reports on entering the stage; the
 * bar may drift toward the next stage's value but never past it.
 */
export interface Stage {
  key: AssessmentStatus;
  caption: string;
  sublines: string[];
  at: number;
}

export const STAGES: Stage[] = [
  {
    key: "fetching_resources",
    caption: "Discovering your Azure environment",
    sublines: ["Mapping subscriptions and resources", "Reviewing resource configurations", "Cataloguing what's deployed"],
    at: 15,
  },
  {
    key: "fetching_metrics",
    caption: "Analyzing utilization patterns",
    sublines: ["Understanding real workload demand", "Looking for under-used capacity", "Measuring how resources are used"],
    at: 35,
  },
  {
    key: "running_advisor",
    caption: "Evaluating infrastructure efficiency",
    sublines: ["Weighing optimization signals", "Assessing configuration efficiency", "Finding where spend can be reduced"],
    at: 55,
  },
  {
    key: "calculating_prices",
    caption: "Reviewing your Azure spend signals",
    sublines: ["Aligning resources with current Azure pricing", "Grounding figures in your real costs", "Reviewing billed usage"],
    at: 70,
  },
  {
    key: "detecting_findings",
    caption: "Identifying optimization opportunities",
    sublines: ["Quantifying potential savings", "Validating each opportunity", "Keeping only what the evidence supports"],
    at: 85,
  },
  {
    key: "generating_report",
    caption: "Building your assessment",
    sublines: ["Assembling your optimization summary", "Preparing your recommendations", "Almost ready"],
    at: 95,
  },
];

const QUEUED_SUBLINES = ["Establishing a secure connection", "Getting things ready"];

export function stageIndexFor(status: AssessmentStatus): number {
  return STAGES.findIndex((s) => s.key === status);
}

/** The single primary caption shown for the current backend state. */
export function captionFor(status: AssessmentStatus): string {
  if (status === "queued") return "Connecting securely to Azure";
  if (status === "completed") return "Finalizing your results";
  return STAGES.find((s) => s.key === status)?.caption ?? "Preparing your assessment";
}

/** The rotating secondary descriptors for the current backend state (customer language only). */
export function sublinesFor(status: AssessmentStatus): string[] {
  if (status === "queued") return QUEUED_SUBLINES;
  if (status === "completed") return ["Bringing your results together"];
  return STAGES.find((s) => s.key === status)?.sublines ?? [];
}
