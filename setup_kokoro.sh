#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

device="${1:-cpu}"
if [[ "$device" != "cpu" && "$device" != "cuda" ]]; then
    echo "Usage: ./setup_kokoro.sh [cpu|cuda]" >&2
    exit 2
fi
if ! command -v python3.12 >/dev/null 2>&1; then
    echo "Kokoro requires Python 3.12 (python3.12 was not found)." >&2
    exit 1
fi

python3.12 -m venv python-kokoro/.venv
service_python="python-kokoro/.venv/bin/python"
"$service_python" -m pip install --upgrade pip
if [[ "$device" == "cuda" ]]; then
    "$service_python" -m pip install -r python-kokoro/requirements-cuda.txt
else
    "$service_python" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
fi
"$service_python" -m pip install -r python-kokoro/requirements.txt
"$service_python" -c "import kokoro, torch; print('Kokoro environment ready; PyTorch', torch.__version__)"
echo "Run ./run_kokoro.sh to start Kokoro on http://127.0.0.1:8302."
