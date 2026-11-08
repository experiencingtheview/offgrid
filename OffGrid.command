#!/bin/sh
# Clear macOS launcher at the top of an OffGrid folder.
# Double-click this file to start interactive chat.
cd "$(dirname "$0")" || exit 1
exec ./launch/offgrid.command "$@"
