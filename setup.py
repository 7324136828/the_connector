#!/usr/bin/env python3
"""Cross-platform automated setup orchestrator for The Connector."""

import argparse
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kokoro-device",
        choices=("cpu", "cuda"),
        default="cpu",
        help="required Kokoro PyTorch build (default: cpu)",
    )
    return parser.parse_args()


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

    kokoro_python = ["py", "-3.12"] if os.name == "nt" else ["python3.12"]
    try:
        result = subprocess.run(
            [*kokoro_python, "-c", "import sys; print(sys.version.split()[0])"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SystemExit("Error: Python 3.12 is required for the Kokoro speech service.") from exc
    print(f"[OK] Kokoro Python version: {result.stdout.strip()}")


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


def install_kokoro(device: str) -> None:
    log(f"Installing the isolated Kokoro service ({device})...")
    if os.name == "nt":
        subprocess.check_call([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT_DIR / "python-kokoro" / "setup.ps1"),
            "-Device", device,
        ])
    else:
        subprocess.check_call(["bash", str(ROOT_DIR / "setup_kokoro.sh"), device])
    print("[OK] Kokoro speech service installed successfully")


def main() -> None:
    args = parse_args()
    print("=" * 60)
    print("  The Connector - Automated Environment Provisioning")
    print("=" * 60)

    check_prerequisites()
    py_bin, _ = create_virtualenv()
    install_backend(py_bin)
    install_frontend()
    setup_env()
    install_kokoro(args.kokoro_device)

    log("Setup completed successfully!")
    print("\nNext steps:")
    if os.name == "nt":
        print("  Run: run.bat to start Frontend, Connector API, and Kokoro")
    else:
        print("  Run: ./run.sh to start Frontend, Connector API, and Kokoro")
    print("=" * 60)


if __name__ == "__main__":
    main()
