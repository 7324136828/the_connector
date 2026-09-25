#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
service_python="python-kokoro/.venv/bin/python"
if [[ ! -x "$service_python" ]]; then
    echo "Run ./setup_kokoro.sh first to prepare the Python 3.12 environment." >&2
    exit 1
fi

exec "$service_python" -u python-kokoro/server.py --host 127.0.0.1 --port 8302 "$@"
