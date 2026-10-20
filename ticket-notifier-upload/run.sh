#!/usr/bin/env bash
# Start Ticket Watch.
#
# Chromium runs in the background (--headless=new); nothing is displayed.
# Values already in the environment win over .env, so you can override any
# setting for one run:  BMS_CATALOG_SCOPE=off ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    key=${line%%=*}
    [ -n "${!key:-}" ] || export "$key=${line#*=}"
  done < .env
fi

CMD=(python3 -m uvicorn app.main:app
     --host "${BMS_HOST:-127.0.0.1}" --port "${BMS_PORT:-${PORT:-8000}}")

# Cloudflare blocks headless browsers, so Chrome runs with a real window
# (minimised on a desktop). On a server with no display, xvfb provides one
# and nothing is ever drawn.
if [[ "$(uname)" != "Darwin" && -z "${DISPLAY:-}" && "${BMS_HEADLESS:-false}" != "true" ]]; then
  command -v xvfb-run >/dev/null || { echo "install xvfb:  sudo apt-get install -y xvfb"; exit 1; }
  echo "no display detected - running Chrome under xvfb"
  exec xvfb-run -a --server-args="-screen 0 1400x950x24" "${CMD[@]}"
fi
exec "${CMD[@]}"
