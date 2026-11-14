#!/bin/sh
# OffGrid launcher (Linux / macOS).
#
# With no arguments it opens the one-click setup in the browser. Explicit
# commands still get the bundled model server started first. Works with a
# private or system Python, whichever exists first.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
PORT="${OFFGRID_PORT:-8089}"
BASE="http://127.0.0.1:$PORT/v1"

# ---- python ---------------------------------------------------------------
PY=""
if [ -x "$ROOT/runtime/python/bin/python3" ]; then
  PY="$ROOT/runtime/python/bin/python3"
elif [ -x "$ROOT/runtime/python/python.exe" ]; then
  PY="$ROOT/runtime/python/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
  PY=$(command -v python)
fi

# Removing the Python-install step is the difference between "works" and
# "double-click does nothing" for most people. The runtime stays in this folder.
if [ -z "$PY" ]; then
  if [ -f "$HERE/bootstrap.sh" ]; then
    if ! sh "$HERE/bootstrap.sh"; then
      echo "OffGrid: setup could not install its private Python runtime." >&2
      exit 1
    fi
    if [ -x "$ROOT/runtime/python/bin/python3" ]; then
      PY="$ROOT/runtime/python/bin/python3"
    fi
  fi
fi

if [ -n "$PY" ] && ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  # An old system Python is worse than no Python: setup would fail later with
  # a syntax error. Use the private runtime instead, exactly as if none existed.
  PY=""
  if [ -f "$HERE/bootstrap.sh" ] && sh "$HERE/bootstrap.sh" && [ -x "$ROOT/runtime/python/bin/python3" ]; then
    PY="$ROOT/runtime/python/bin/python3"
  fi
fi

if [ -z "$PY" ]; then
  echo "OffGrid: setup could not find or install a compatible Python runtime." >&2
  echo "Check the internet connection and run this again." >&2
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
# The setup command starts and owns the model server itself. Starting a second
# copy here would make a double-click wait for the same model twice (and leave
# an orphan process when the setup window closes).
if [ "$#" -eq 0 ]; then
  exec "$PY" -m offgrid easy
fi
if [ "${1:-}" = "easy" ]; then
  shift
  exec "$PY" -m offgrid easy "$@"
fi

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
      -c "${OFFGRID_CONTEXT:-8192}" --jinja \
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
