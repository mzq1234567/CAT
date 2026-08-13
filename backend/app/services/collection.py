"""
Data-collection quality model (Batch 2).

The overriding rule for the whole Azure data pipeline: **missing data is NOT zero.** A throttled,
timed-out, unauthorised or failed Azure call must never be silently turned into "no resources" /
"zero cost" / "zero utilisation" — that would recreate the exact financial-integrity problem Batch 1
fixed. Instead every stage records what it actually collected vs what failed, and the run computes an
explicit COMPLETE / PARTIAL / FAILED data-quality state.

`RetryStats` is the low-level counter the retry layer feeds (429s, retries, exhaustions). It is shared
with the AzureClient so a whole run's throttling is measurable. `CollectionReport` is the per-run
aggregate the pipeline fills in and stores on the Assessment: detailed enough for troubleshooting
(diagnostics JSON), summarised into ONE concise, professional line for the client UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

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
    billing_failed_subs: int = 0
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

    def client_message(self) -> Optional[str]:
        """One concise, professional line for the client UI — or None when collection was COMPLETE.

        Deliberately free of HTTP status codes, stack traces and developer terminology (those live in
        the diagnostics/logs). It only tells the client that some data was missing and, crucially, that
        the affected recommendations were EXCLUDED from quantified savings rather than guessed.
        """
        quality = self.data_quality()
        if quality == COMPLETE:
            return None
        if quality == FAILED:
            return ("Azure resource data could not be collected for this assessment (the Azure APIs did "
                    "not respond successfully). No findings are shown — please re-run shortly.")
        return ("Some Azure data could not be collected for this assessment. Affected recommendations "
                "have been excluded from quantified savings; re-run shortly for complete results.")
