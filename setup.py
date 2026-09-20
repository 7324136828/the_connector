#!/usr/bin/env python3
"""Cross-platform automated setup orchestrator for The Connector."""

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
VENV_DIR = ROOT_DIR / ".venv"


def log(msg: str) -> None:
    print(f"\n[SETUP] {msg}", flush=True)


def check_prerequisites() -> None:
    log("Checking prerequisites...")
    if sys.version_info < (3, 10):
        sys.exit("Error: Python 3.10 or higher is required.")
    print(f"[OK] Python version: {sys.version.split()[0]}")

    npm_bin = "npm.cmd" if os.name == "nt" else "npm"
    if not shutil.which(npm_bin) and not shutil.which("npm"):
        sys.exit("Error: Node.js and npm are required. Please install Node.js.")
    print("[OK] Node.js & npm detected")


def create_virtualenv() -> tuple[Path, Path]:
    log(f"Configuring Python virtual environment at {VENV_DIR}...")
    if not VENV_DIR.exists():
        venv.create(VENV_DIR, with_pip=True)

    if os.name == "nt":
        py_bin = VENV_DIR / "Scripts" / "python.exe"
        pip_bin = VENV_DIR / "Scripts" / "pip.exe"
    else:
        py_bin = VENV_DIR / "bin" / "python"
        pip_bin = VENV_DIR / "bin" / "pip"

    if not py_bin.exists() or not pip_bin.exists():
        sys.exit(f"Error: Virtual environment executables not found in {VENV_DIR}")

    print(f"[OK] Virtualenv ready: {py_bin}")
    return py_bin, pip_bin


def install_backend(py_bin: Path) -> None:
    log("Installing backend dependencies...")
    subprocess.check_call([str(py_bin), "-m", "pip", "install", "--upgrade", "pip"])
    req_file = BACKEND_DIR / "requirements.txt"
    if req_file.exists():
        subprocess.check_call([str(py_bin), "-m", "pip", "install", "-r", str(req_file)])
    print("[OK] Backend dependencies installed successfully")


def install_frontend() -> None:
    log("Installing frontend dependencies...")
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.check_call([npm_cmd, "install"], cwd=str(FRONTEND_DIR), shell=(os.name == "nt"))
    print("[OK] Frontend dependencies installed successfully")


def setup_env() -> None:
    log("Setting up environment configuration...")
    env_example = ROOT_DIR / ".env.example"
    env_target = ROOT_DIR / ".env"
    if env_example.exists() and not env_target.exists():
        shutil.copy(env_example, env_target)
        print("[OK] Created .env from .env.example")
    elif env_target.exists():
        print("[OK] Existing .env preserved")


def main() -> None:
    print("=" * 60)
    print("  The Connector - Automated Environment Provisioning")
    print("=" * 60)

    check_prerequisites()
    py_bin, _ = create_virtualenv()
    install_backend(py_bin)
    install_frontend()
    setup_env()

    log("Setup completed successfully!")
    print("\nNext steps:")
    if os.name == "nt":
        print("  Run: run.bat to start both Frontend & Backend")
    else:
        print("  Run: ./run.sh to start both Frontend & Backend")
    print("=" * 60)


if __name__ == "__main__":
    main()
