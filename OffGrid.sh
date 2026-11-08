#!/bin/sh
# Clear launcher at the top of an OffGrid folder.
#
# Run it directly (./OffGrid.sh) or double-click it in a file manager that can
# execute scripts. With no arguments it starts interactive chat.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
exec "$HERE/launch/offgrid.sh" "$@"
