#!/bin/sh
# Download a private Python runtime into this OffGrid folder on first launch.
#
# The portable app is tiny on purpose, but many people do not have Python
# installed. This script removes that cliff without asking the user to touch
# the system: the runtime lives in runtime/python inside OffGrid and is used
# only by these launchers. It needs curl or wget and tar, which macOS and
# desktop Linux already provide.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
RUNTIME="$ROOT/runtime/python"
ARCHIVE="$ROOT/.downloads/python-runtime.tar.gz"
TMP="$ROOT/runtime/python-bootstrap"

# A previous download may have been interrupted. Do not trust a file just
# because it exists: run it once before deciding setup is finished.
if [ -x "$RUNTIME/bin/python3" ] && "$RUNTIME/bin/python3" -c 'import sys' >/dev/null 2>&1; then
  exit 0
fi

case "$(uname -s 2>/dev/null || echo unknown)" in
  Darwin) OS="darwin" ;;
  Linux) OS="linux" ;;
  *) echo "OffGrid: automatic Python setup supports macOS and Linux only." >&2; exit 1 ;;
esac

case "$(uname -m 2>/dev/null || echo unknown)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64) ARCH="x64" ;;
  *) echo "OffGrid: this CPU architecture is not supported by automatic setup." >&2; exit 1 ;;
esac

case "$OS/$ARCH" in
  darwin/arm64) TRIPLE="aarch64-apple-darwin" ;;
  darwin/x64) TRIPLE="x86_64-apple-darwin" ;;
  linux/arm64) TRIPLE="aarch64-unknown-linux-gnu" ;;
  linux/x64) TRIPLE="x86_64-unknown-linux-gnu" ;;
  *) echo "OffGrid: this computer is not supported by automatic setup." >&2; exit 1 ;;
esac

download() {
  mkdir -p "$ROOT/.downloads" "$ROOT/runtime"
  echo "  Downloading a private Python runtime (about 65 MB, one time)..."
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 --progress-bar -o "$ARCHIVE" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$ARCHIVE" "$1"
  else
    echo "OffGrid: curl or wget is needed for the one-time Python download." >&2
    return 1
  fi
}

echo "  OffGrid: first run needs a small Python runtime."
echo "  It is stored inside this folder, not installed on the computer."

# A stable, immutable python-build-standalone release. Pinning it avoids the
# anonymous GitHub API limits and makes every platform use the same version.
URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-${TRIPLE}-install_only.tar.gz"

if ! download "$URL"; then
  echo "OffGrid: the Python download failed. Check the internet connection and try again." >&2
  exit 1
fi

rm -rf "$TMP"
mkdir -p "$TMP"
if ! tar -xzf "$ARCHIVE" -C "$TMP" 2>/dev/null; then
  echo "OffGrid: the downloaded Python archive could not be extracted." >&2
  rm -rf "$TMP" "$ARCHIVE"
  exit 1
fi
if [ ! -d "$TMP/python" ]; then
  echo "OffGrid: the downloaded Python archive had an unexpected layout." >&2
  rm -rf "$TMP" "$ARCHIVE"
  exit 1
fi
rm -rf "$RUNTIME"
mv "$TMP/python" "$RUNTIME"
rm -rf "$TMP" "$ARCHIVE"

if ! "$RUNTIME/bin/python3" -c 'import sys' >/dev/null 2>&1; then
  echo "OffGrid: the private Python runtime did not start correctly." >&2
  exit 1
fi

echo "  Python is ready. Starting OffGrid..."
exit 0
