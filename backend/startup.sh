#!/bin/bash
# Azure App Service (Linux) startup command — set as:  bash backend/startup.sh
#
# The REPO ROOT is what gets deployed (backend/ + frontend/dist/ + the root requirements.txt shim), so
# main.py's ../frontend/dist lookup resolves. Dependencies are installed at deploy time by App Service's
# Oryx build (SCM_DO_BUILD_DURING_DEPLOYMENT=true) into the `antenv` virtualenv, which the platform puts
# on PYTHONPATH before running this script — so no pip install here.
#
# Oryx COMPRESSES the built app and unpacks it to a random /tmp/<id>/ at boot (not /home/site/wwwroot),
# running this script from there — so never hard-code the app path; resolve it from this script's location.
set -e

# SQLite lives on /home (App Service's persistent share) — outside the app dir, which is replaced on
# every deploy. SQLite does not create parent directories. DATABASE_URL points here.
mkdir -p /home/data

cd "$(dirname "$0")"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
