#!/bin/sh
# macOS double-click launcher: opens Terminal, runs the shared launcher.
cd "$(dirname "$0")" || exit 1
exec ./offgrid.sh "$@"
