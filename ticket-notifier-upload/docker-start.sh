#!/usr/bin/env bash
# Chrome needs a display even when nothing is shown; xvfb provides one.
# Railway injects $PORT - bind to it or the deploy is marked unhealthy.
set -euo pipefail
exec xvfb-run -a --server-args="-screen 0 1400x950x24" \
  python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
