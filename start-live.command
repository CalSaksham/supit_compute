#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/live"
MODEL=gemma3:4b

if lsof -ti tcp:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  printf '\nSUPIT live is already running. Open http://127.0.0.1:8787/demo.html\n\n'
  exit 0
fi
if ! command -v ollama >/dev/null; then
  printf '\nOllama is not installed. Get it from https://ollama.com/download, then run this again.\n\n'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  printf '\nFirst run: setting up the live backend (about a minute)...\n'
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if ! ollama list 2>/dev/null | grep -q "^$MODEL"; then
  printf '\nDownloading %s (about 3 GB, once)...\n' "$MODEL"
  ollama pull "$MODEL"
fi

printf '\nStarting SUPIT live: three local Ollama servers play Lambda, CoreWeave and Together AI.\n'
printf 'This takes 20-40 seconds. Press Control-C to stop everything.\n'
exec .venv/bin/python -m supit_live
