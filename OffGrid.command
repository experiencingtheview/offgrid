#!/bin/sh
# Clear macOS launcher at the top of an OffGrid folder.
# Double-click this file to open the one-click setup in your browser.
cd "$(dirname "$0")" || exit 1
exec ./launch/offgrid.command "$@"
