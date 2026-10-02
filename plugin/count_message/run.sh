#!/usr/bin/env sh
set -eu
PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${COUNT_MESSAGE_PYTHON:-python3}" "$PLUGIN_DIR/run.py" "$@"
