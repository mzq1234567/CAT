#!/bin/bash
# Azure App Service (Linux) startup command — set as:  bash backend/startup.sh
#
# The REPO ROOT is what gets deployed to /home/site/wwwroot (backend/ + frontend/dist/ + the root
# requirements.txt shim), so main.py's ../frontend/dist lookup resolves. Dependencies are installed
# at deploy time by App Service's Oryx build (SCM_DO_BUILD_DURING_DEPLOYMENT=true) into the `antenv`
# virtualenv, which App Service activates before running this script — so no pip install here.
set -e

# SQLite lives OUTSIDE wwwroot (wwwroot is replaced on every deploy); /home is persistent storage.
# SQLite does not create parent directories, so make sure it exists. DATABASE_URL points here.
mkdir -p /home/data

cd /home/site/wwwroot/backend
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
