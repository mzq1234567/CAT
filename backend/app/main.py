from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pathlib

from .config import settings
from .database import Base, engine
from .api.routes import assessments, subscriptions

# Auto-create tables (Alembic handles migrations in production)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Azure Cost Assessment Platform", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(subscriptions.router, prefix="/api/subscriptions", tags=["subscriptions"])
app.include_router(assessments.router, prefix="/api/assessments", tags=["assessments"])

# Serve React build in production (Azure App Service)
frontend_dist = pathlib.Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")


@app.get("/api/health")
def health():
    return {"status": "ok"}
