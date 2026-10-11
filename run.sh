#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# An active environment takes precedence, as in the sample project.
if [[ -n "${VIRTUAL_ENV:-}${CONDA_PREFIX:-}" ]]; then
    PYTHON_BIN="$(command -v python || command -v python3)"
    exec "$PYTHON_BIN" run.py "$@"
fi
if [[ -x .venv/bin/python ]]; then
    exec .venv/bin/python run.py "$@"
fi
exec python3 run.py "$@"
