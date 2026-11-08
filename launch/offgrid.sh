#!/bin/sh
# OffGrid launcher (Linux / macOS).
#
# Starts the bundled model server if it is not already running, waits for it to
# answer, then hands over to the agent. Works with a bundled portable python or
# the system one, whichever exists first.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
PORT="${OFFGRID_PORT:-8089}"
BASE="http://127.0.0.1:$PORT/v1"

# ---- python ---------------------------------------------------------------
if [ -x "$ROOT/runtime/python/bin/python3" ]; then
  PY="$ROOT/runtime/python/bin/python3"
elif [ -x "$ROOT/runtime/python/python.exe" ]; then
  PY="$ROOT/runtime/python/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
  PY=$(command -v python)
else
  echo "OffGrid: no python found. Bundle one with scripts/setup_usb.py" >&2
  exit 1
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# ---- helpers --------------------------------------------------------------
probe() {
  "$PY" - "$BASE" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1] + "/models", timeout=2).read(64)
except Exception:
    raise SystemExit(1)
PYEOF
}

mkdir -p "$ROOT/logs" "$ROOT/run"

# ---- model server ---------------------------------------------------------
MODEL=$(find "$ROOT/models" -maxdepth 1 -name '*.gguf' 2>/dev/null | head -n 1)
SERVER=""
for candidate in "$ROOT/runtime/llama/llama-server" "$ROOT/runtime/llama/build/bin/llama-server"; do
  if [ -x "$candidate" ]; then SERVER="$candidate"; break; fi
done
if [ -z "$SERVER" ] && [ -d "$ROOT/runtime/llama" ]; then
  SERVER=$(find "$ROOT/runtime/llama" -name 'llama-server' -type f 2>/dev/null | head -n 1)
fi

if [ -n "$MODEL" ] && [ -x "$SERVER" ] && [ -z "${OFFGRID_NO_SERVER:-}" ]; then
  if ! probe; then
    echo "OffGrid: loading $(basename "$MODEL") on port $PORT (first token takes a moment)..."
    "$SERVER" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
      -c "${OFFGRID_CONTEXT:-8192}" --jinja -t "${OFFGRID_THREADS:-0}" \
      >>"$ROOT/logs/llama.log" 2>&1 &
    echo $! > "$ROOT/run/llama.pid"
    waited=0
    while [ "$waited" -lt 180 ]; do
      if probe; then break; fi
      sleep 1
      waited=$((waited + 1))
    done
    if ! probe; then
      echo "OffGrid: the model server did not come up. Last log lines:" >&2
      tail -n 20 "$ROOT/logs/llama.log" >&2
      echo "Continuing anyway; run 'offgrid doctor' once it is fixed." >&2
    fi
  fi
fi

# ---- the agent ------------------------------------------------------------
exec "$PY" -m offgrid "$@"
