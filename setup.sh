#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "========================================================"
echo "[SETUP.SH] Checking Python 3..."
echo "========================================================"
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed or not in PATH."
    exit 1
fi

echo "[SETUP.SH] Dispatching setup.py..."
python3 setup.py
echo "[SETUP.SH] Setup finished successfully."
