#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -f ".venv/bin/activate" ]; then
    echo "[RUN.SH] Virtual environment not found. Running setup.sh first..."
    ./setup.sh
fi

source .venv/bin/activate

echo "========================================================"
echo "Starting The Connector Full-Stack Services"
echo "Backend:  http://localhost:8301 (API & Docs: /docs)"
echo "Kokoro:  http://localhost:8302 (when installed)"
echo "Frontend: http://localhost:5173"
echo "========================================================"

# Graceful cleanup on SIGINT / SIGTERM / EXIT
cleanup() {
    echo -e "\n[RUN.SH] Stopping background services..."
    kill $(jobs -p) 2>/dev/null || true
    wait 2>/dev/null || true
    echo "[RUN.SH] All services stopped."
}
trap cleanup SIGINT SIGTERM EXIT

# Start backend in background
python run_backend.py --reload &
BACKEND_PID=$!

# Start the isolated Kokoro backend when its dedicated environment is installed.
if [ -x "python-kokoro/.venv/bin/python" ]; then
    bash ./run_kokoro.sh &
    KOKORO_PID=$!
else
    echo "[RUN.SH] Kokoro is not installed; run ./setup.sh --with-kokoro to enable speech."
fi

# Start frontend in foreground
(cd frontend && npm run dev) &
FRONTEND_PID=$!

wait
