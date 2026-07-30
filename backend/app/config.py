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
    token_enforce_audience: bool = False
    token_allowed_audiences: List[str] = [
        "https://management.azure.com/",
        "https://management.azure.com",
        "https://management.core.windows.net/",
    ]

    # Max subscriptions accepted per assessment request (bounds fan-out / abuse).
    max_subscriptions_per_assessment: int = 50

    # Azure API resilience (Step 8)
    azure_max_retries: int = 4
    azure_retry_base_delay: float = 0.5

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
