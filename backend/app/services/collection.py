"""
Data-collection quality model (Batch 2).

The overriding rule for the whole Azure data pipeline: **missing data is NOT zero.** A throttled,
timed-out, unauthorised or failed Azure call must never be silently turned into "no resources" /
"zero cost" / "zero utilisation", that would recreate the exact financial-integrity problem Batch 1
fixed. Instead every stage records what it actually collected vs what failed, and the run computes an
explicit COMPLETE / PARTIAL / FAILED data-quality state.

`RetryStats` is the low-level counter the retry layer feeds (429s, retries, exhaustions). It is shared
with the AzureClient so a whole run's throttling is measurable. `CollectionReport` is the per-run
aggregate the pipeline fills in and stores on the Assessment: detailed enough for troubleshooting
(diagnostics JSON), summarised into ONE concise, professional line for the client UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# Data-quality states — kept as plain strings so they round-trip through the DB/schema cleanly.
COMPLETE = "complete"   # every foundational + enrichment call succeeded
PARTIAL = "partial"     # core inventory succeeded but some data could not be collected
FAILED = "failed"       # foundational data (resource inventory) could not be collected at all


@dataclass
class RetryStats:
    """Low-level retry/throttle counters, shared with the AzureClient for a whole run."""
    throttled_responses: int = 0   # HTTP 429 responses observed
    server_errors: int = 0         # retryable 5xx responses observed
    retries: int = 0               # total retry attempts made
    transport_errors: int = 0      # network/transport errors observed
    exhausted: int = 0             # calls that returned still-throttled after exhausting retries

    def as_dict(self) -> Dict[str, int]:
        return {
            "throttled_responses": self.throttled_responses,
            "server_errors": self.server_errors,
            "retries": self.retries,
            "transport_errors": self.transport_errors,
            "exhausted": self.exhausted,
        }


@dataclass
class CollectionReport:
    """Per-run tally of what Azure data was successfully collected vs failed.

    Filled in by the pipeline stage by stage. `data_quality()` derives the COMPLETE/PARTIAL/FAILED
    state; `client_message()` renders the single concise line for the UI; `diagnostics()` is the
    detailed internal record (stored as JSON, surfaced only in logs/debug, never as client prose).
    """
    subscriptions_requested: int = 0
    resources_discovered: Optional[int] = None    # None = the count query itself failed (NOT zero)

    inventory_buckets_total: int = 0
    inventory_failed_buckets: List[str] = field(default_factory=list)
    inventory_summary_failed: bool = False        # the resource-count query failed (count is unknown)

    metrics_requested: int = 0
    metrics_failed: int = 0                        # resources whose metric call FAILED (≠ genuinely empty)

    advisor_failed_subs: int = 0
    reservation_failed_subs: int = 0
    # Subscriptions whose Cost Management billing collection FINALLY failed. It is a SET (not a running
    # counter) so a transient 429 that later recovers on retry can REMOVE the subscription again — the
    # flag must reflect the final billing outcome, not that a 429 occurred at some point. `mark_billing_*`
    # are the only writers; `billing_failed_subs` (below) exposes the count for the existing int callers.
    billing_failed_sub_ids: Set[str] = field(default_factory=set)
    billing_detail_unavailable: bool = False       # per-resource billing missing (Batch 1 degraded state)
    pricing_failures: int = 0                      # live-price lookups that returned unavailable

    retry: RetryStats = field(default_factory=RetryStats)

    # ── recording helpers (called by the collection stages) ─────────────────────────
    def note_inventory(self, buckets_total: int, failed_buckets: List[str]) -> None:
        self.inventory_buckets_total = buckets_total
        self.inventory_failed_buckets = list(failed_buckets)

    def note_metrics(self, requested: int, failed: int) -> None:
        self.metrics_requested += requested
        self.metrics_failed += failed

    def mark_billing_failed(self, sub: str) -> None:
        """A billing query FAILED for this subscription (recorded per-sub so a later retry can clear it)."""
        self.billing_failed_sub_ids.add(sub)

    def mark_billing_recovered(self, sub: str) -> None:
        """A billing query SUCCEEDED for this subscription → it is no longer billing-failed (idempotent)."""
        self.billing_failed_sub_ids.discard(sub)

    @property
    def billing_failed_subs(self) -> int:
        """Count of subscriptions whose billing finally failed (kept as an int for existing callers)."""
        return len(self.billing_failed_sub_ids)

    # ── derived state ───────────────────────────────────────────────────────────────
    def _all_inventory_failed(self) -> bool:
        return (self.inventory_buckets_total > 0
                and len(self.inventory_failed_buckets) >= self.inventory_buckets_total)

    def _any_partial(self) -> bool:
        return bool(
            self.inventory_failed_buckets or self.inventory_summary_failed
            or self.metrics_failed or self.advisor_failed_subs or self.reservation_failed_subs
            or self.billing_failed_subs or self.billing_detail_unavailable
            or self.pricing_failures or self.retry.exhausted
        )

    def data_quality(self) -> str:
        # Resource inventory is foundational: if EVERY bucket failed, we have no defensible view of the
        # environment at all → FAILED (never presented as a clean "no findings" assessment).
        if self._all_inventory_failed():
            return FAILED
        return PARTIAL if self._any_partial() else COMPLETE

    def diagnostics(self) -> Dict:
        """Detailed internal record for troubleshooting (stored as JSON; not shown as client prose)."""
        return {
            "data_quality": self.data_quality(),
            "subscriptions_requested": self.subscriptions_requested,
            "resources_discovered": self.resources_discovered,
            "inventory_buckets_total": self.inventory_buckets_total,
            "inventory_failed_buckets": self.inventory_failed_buckets,
            "inventory_summary_failed": self.inventory_summary_failed,
            "metrics_requested": self.metrics_requested,
            "metrics_failed": self.metrics_failed,
            "advisor_failed_subs": self.advisor_failed_subs,
            "reservation_failed_subs": self.reservation_failed_subs,
            "billing_failed_subs": self.billing_failed_subs,
            "billing_detail_unavailable": self.billing_detail_unavailable,
            "pricing_failures": self.pricing_failures,
            "retry": self.retry.as_dict(),
        }

    def failed_sources(self) -> List[str]:
        """The specific data source(s) that did not fully collect, client-safe phrases, no HTTP codes.

        Named precisely so the client (and the logs) know EXACTLY what was missing rather than a vague
        "some data". Ordered by how much each matters to a cost assessment.
        """
        out: List[str] = []
        if self._all_inventory_failed():
            out.append("Azure resource inventory")
        elif self.inventory_failed_buckets:
            n = len(self.inventory_failed_buckets)
            out.append(f"some resource types ({n})")
        if self.billing_failed_subs:
            out.append(f"cost data ({self.billing_failed_subs} subscription"
                       f"{'s' if self.billing_failed_subs != 1 else ''})")
        elif self.billing_detail_unavailable:
            out.append("detailed per-resource cost")
        if self.metrics_failed:
            out.append(f"utilisation metrics ({self.metrics_failed} resource"
                       f"{'s' if self.metrics_failed != 1 else ''})")
        if self.reservation_failed_subs:
            out.append("reservation recommendations")
        if self.advisor_failed_subs:
            out.append("Azure Advisor recommendations")
        # A throttle-exhaustion that didn't map to a specific stage above (e.g. a supplementary call).
        if not out and self.retry.exhausted:
            out.append("some Azure data (the service was busy)")
        return out

    def client_message(self) -> Optional[str]:
        """One concise, professional line for the client UI, or None when collection was COMPLETE.

        Deliberately free of HTTP status codes, stack traces and developer terminology (those live in
        the diagnostics/logs). Now NAMES the specific source(s) that failed and tells the client that
        the affected recommendations were EXCLUDED from quantified savings rather than guessed.
        """
        quality = self.data_quality()
        if quality == COMPLETE:
            return None
        if quality == FAILED:
            return ("Azure resource data could not be collected for this assessment (the Azure APIs did "
                    "not respond successfully). No findings are shown, please re-run shortly.")
        sources = self.failed_sources()
        if sources:
            listed = sources[0] if len(sources) == 1 else (
                ", ".join(sources[:-1]) + f" and {sources[-1]}")
            return (f"Some Azure data could not be collected, {listed}. Affected recommendations were "
                    "excluded from quantified savings; re-run shortly for complete results.")
        return ("Some Azure data could not be collected for this assessment. Affected recommendations "
                "have been excluded from quantified savings; re-run shortly for complete results.")

    def stage_summary(self) -> Dict[str, str]:
        """A per-stage ok / partial / failed view for the backend logs, the single line that identifies
        WHICH collection operation failed on a real run (never logs tokens/secrets)."""
        return {
            "resource_inventory": "ok" if not self.inventory_failed_buckets
                                  else (f"partial ({len(self.inventory_failed_buckets)} of "
                                        f"{self.inventory_buckets_total} types failed: "
                                        f"{','.join(self.inventory_failed_buckets)})"),
            "resource_count": "ok" if not self.inventory_summary_failed else "unknown (count query failed)",
            "cost_data": ("failed" if self.billing_failed_subs else
                          "per-resource unavailable" if self.billing_detail_unavailable else "ok"),
            "metrics": "ok" if not self.metrics_failed else f"partial ({self.metrics_failed}/"
                       f"{self.metrics_requested} resources failed)",
            "advisor": "ok" if not self.advisor_failed_subs else f"{self.advisor_failed_subs} subs failed",
            "reservations": "ok" if not self.reservation_failed_subs
                            else f"{self.reservation_failed_subs} subs failed",
            "throttling": (f"429s={self.retry.throttled_responses} 5xx={self.retry.server_errors} "
                           f"retries={self.retry.retries} exhausted={self.retry.exhausted} "
                           f"transport={self.retry.transport_errors}"),
            "data_quality": self.data_quality(),
        }
