# One-shot dev startup: create venv, install deps, run server
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt --quiet

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
