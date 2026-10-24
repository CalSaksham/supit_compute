#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
printf '\nSUPIT is running locally.\nOpen http://127.0.0.1:%s in your browser.\nPress Control-C to stop.\n\n' "${SUPIT_PORT:-8001}"
exec python3 -m http.server "${SUPIT_PORT:-8001}" --bind 127.0.0.1 --directory web
