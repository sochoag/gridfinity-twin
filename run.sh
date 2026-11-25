#!/usr/bin/env bash
# Arranca Gridfinity Twin en http://127.0.0.1:8000  (solo local: no tiene autenticacion)
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m uvicorn server.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
