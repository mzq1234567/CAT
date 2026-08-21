from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    azure_client_id: str = ""
    database_url: str = "sqlite:///./cat.db"
    cors_origins: List[str] = ["http://localhost:5173"]

    # Pricing engine (Step 2)
    pricing_currency: str = "USD"
    pricing_cache_ttl_seconds: int = 86400  # 24h
    pricing_daily_refresh: bool = True

    # Rate limiting (Step 7)
    rate_limit_max_requests: int = 10
    rate_limit_window_seconds: int = 60
    log_level: str = "INFO"

    # Token security — verify the Azure AD RS256 signature (JWKS) on every request.
    # Secure by default; disabling it re-opens a tenant-isolation bypass (see security/token.py).
    verify_token_signature: bool = True
    # Audience is now enforced by default: the token must have been issued for Azure Resource Manager
    # (the resource we forward it to). The list covers every ARM audience form Entra mints (v1 URL with/
    # without trailing slash, the legacy management.core URL, and the ARM App ID GUID).
    token_enforce_audience: bool = True
    token_allowed_audiences: List[str] = [
        "https://management.azure.com/",
        "https://management.azure.com",
        "https://management.core.windows.net/",
        "797f4846-ba00-4b28-9c31-92eee91bb9ba",  # Azure Service Management App ID (ARM)
    ]
    # Verify the token was issued for THIS application (its `appid`/`azp` == our client id) and by a
    # legitimate Microsoft issuer for the token's own tenant. Requires `azure_client_id` to be set for the
    # app-id check; when unset (dev), the app-id check is skipped with a startup warning (issuer + audience
    # still enforced). See security/token.py.
    token_require_issuer: bool = True
    token_require_delegated: bool = True   # reject app-only tokens (must represent a signed-in user)

    # Max subscriptions accepted per assessment request (bounds fan-out / abuse).
    max_subscriptions_per_assessment: int = 50

    # Azure API resilience (Step 8). `azure_max_retries` is the retry budget for the fast ARM/metrics/
    # Resource-Graph calls (Cost Management + Consumption pass their own higher budget). Bumped to 6 so a
    # heavily-throttled run has more chances to recover before a call exhausts its retries — which is what
    # turns a run PARTIAL. Each wait is still bounded (Retry-After / exponential backoff, capped at 60s),
    # so more retries add patience under throttling without ever stalling indefinitely. Fully configurable.
    azure_max_retries: int = 6
    azure_retry_base_delay: float = 0.5

    # Azure API concurrency (Batch 2) — TWO documented limits, no hidden per-stage caps (see
    # azure_client.py header). `azure_max_concurrency` bounds ONE assessment's in-flight Azure requests;
    # sized to respect Azure Resource Graph's per-tenant limit (~15 queries/5s, and one run = one tenant)
    # with headroom while still parallelising — 8 keeps well under the limit yet far faster than serial.
    # `azure_global_max_concurrency` bounds TOTAL simultaneous requests across ALL concurrent assessments
    # (24 = 3 full-speed runs) so N runs can't multiply the per-run cap into uncontrolled aggregate traffic.
    azure_max_concurrency: int = 8
    azure_global_max_concurrency: int = 24

    # Reservation / Savings Plan recommendation basis:
    #   combined  — recommend for running VMs; high confidence when metrics show steady use, lower
    #               when usage is unconfirmed (always-on fallback). Azure Advisor wins when it has a rec.
    #   measured  — only when metrics show steady running.
    #   always_on — recommend for every running VM regardless of measured usage.
    #   advisor   — rely only on Azure Advisor's reservation recs.
    reservation_basis: str = "combined"

    # Dev-only findings debug reasoning (Step 6/11).
    # TODO: remove or gate behind admin-only role before prod.
    debug_findings_reasoning: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
