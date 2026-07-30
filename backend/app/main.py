import asyncio
import contextlib
import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine
from .api.routes import assessments, subscriptions
from .errors import register_error_handlers
from .logging_config import configure_logging
from .middleware import RequestContextMiddleware
from .services.pricing import daily_refresh_loop, get_pricing_engine

configure_logging(settings.log_level)
logger = logging.getLogger("cat.main")

if not settings.verify_token_signature:
    logger.warning(
        "SECURITY: verify_token_signature is DISABLED — access tokens are NOT signature-checked. "
        "This re-opens a tenant-isolation bypass. Enable it (VERIFY_TOKEN_SIGNATURE=true) for any "
        "non-local deployment."
    )

# Auto-create tables (Alembic handles migrations in production)
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the daily pricing-cache refresh (no-op until prices are first queried).
    refresh_task: asyncio.Task | None = None
    if settings.pricing_daily_refresh:
        refresh_task = asyncio.create_task(daily_refresh_loop(get_pricing_engine()))
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task


app = FastAPI(title="Azure Cost Assessment Platform", version="1.0.0", lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(subscriptions.router, prefix="/api/subscriptions", tags=["subscriptions"])
app.include_router(assessments.router, prefix="/api/assessments", tags=["assessments"])

# Serve React build in production (Azure App Service)
frontend_dist = pathlib.Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")


@app.get("/api/health")
def health():
    return {"status": "ok"}
