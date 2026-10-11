#!/usr/bin/env python3
"""Plan and run The Connector's API, frontend, and isolated speech service."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from backend.network import lan_urls_for_host
from backend.app.user_settings import UserSettingsError, initialize_port_settings

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
LOCAL_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
KOKORO_PYTHON = ROOT / "python-kokoro" / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


class RunError(RuntimeError):
    """Raised when the application cannot be started."""


def load_env_file() -> None:
    """Load launcher settings without replacing variables set by the caller."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        env_file = ROOT / ".env"
        if env_file.is_file():
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    else:
        load_dotenv(ROOT / ".env", override=False)


def parse_port(value: str | int, label: str) -> int:
    try:
        port = int(value)
    except (ValueError, TypeError) as error:
        raise RunError(f"{label} must be an integer") from error
    if not 1 <= port <= 65535:
        raise RunError(f"{label} must be between 1 and 65535")
    return port


def resolve_host(host: str) -> tuple[int, str]:
    try:
        addresses = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RunError(f"Cannot resolve bind address {host!r}: {error}") from error
    if not addresses:
        raise RunError(f"Cannot resolve bind address {host!r}")
    family, _, _, _, address = addresses[0]
    return family, address[0]


def available_port(
    start: int, reserved: set[int] | None = None, *,
    host: str = "127.0.0.1", family: int = socket.AF_INET,
) -> int:
    reserved = reserved or set()
    for port in range(start, 65536):
        if port in reserved:
            continue
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
            except OSError as error:
                if error.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue
                raise RunError(f"Cannot bind to {host}:{port}: {error}") from error
        return port
    raise RunError(f"No available TCP port was found at or above {start}")


def service_url(host: str, port: int) -> str:
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    hosts = parser.add_mutually_exclusive_group()
    hosts.add_argument("--host", "-BindHost", help="bind address (default: BACKEND_HOST, HOST, or 0.0.0.0)")
    hosts.add_argument("--local", "-Local", action="store_true", help="bind only to 127.0.0.1")
    parser.add_argument("--backend-port", "-BackendPort", type=int, help="first API port to try (default: settings.json connector_port, initialized from port_mapping.json)")
    parser.add_argument("--frontend-port", "-UiPort", type=int, help="first UI port to try (default: settings.json react_port, initialized from port_mapping.json)")
    parser.add_argument("--kokoro-port", "-KokoroPort", type=int, help="first speech port to try (default: settings.json kokoro_port, initialized from port_mapping.json)")
    reload_flags = parser.add_mutually_exclusive_group()
    reload_flags.add_argument("--no-reload", "-NoReload", action="store_true", help="disable development backend reload")
    reload_flags.add_argument("--reload", "-Reload", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--completion-no-database-access", "-CompletionNoDatabaseAccess", action="store_true")
    parser.add_argument("--print-plan-json", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def make_plan(args: argparse.Namespace) -> dict:
    try:
        preferences = initialize_port_settings()
    except UserSettingsError as error:
        raise RunError(str(error)) from error
    family, host = resolve_host("127.0.0.1" if args.local else args.host or os.environ.get("BACKEND_HOST") or os.environ.get("HOST", "0.0.0.0"))
    requested = {
        "backend": parse_port(args.backend_port if args.backend_port is not None else preferences["connector_port"], "backend port"),
        "frontend": parse_port(args.frontend_port if args.frontend_port is not None else preferences["react_port"], "frontend port"),
        "kokoro": parse_port(args.kokoro_port if args.kokoro_port is not None else preferences["kokoro_port"], "Kokoro port"),
    }
    reserved: set[int] = set()
    ports = {}
    for label, port in requested.items():
        ports[label] = available_port(port, reserved, host=host, family=family)
        reserved.add(ports[label])
    # Vite and the API connect to Kokoro through the chosen local bind address.
    plan = {
        "host": host,
        "requested_ports": requested,
        "ports": ports,
        "urls": {label: service_url(host, port) for label, port in ports.items()},
        "lan_urls": {label: lan_urls_for_host(host, port) for label, port in ports.items()},
    }
    environment = os.environ.copy()
    merge_cors_origins(environment, ports["frontend"], [
        plan["urls"]["frontend"], *plan["lan_urls"]["frontend"],
    ])
    if "CORS_ORIGINS" in environment:
        plan["cors_origins"] = environment["CORS_ORIGINS"]
    return plan


def merge_cors_origins(
    environment: dict[str, str], frontend_port: int, extra_origins: list[str],
) -> None:
    """Include selected UI origins in an explicitly restricted CORS setting.

    The Connector's Pydantic settings accept a JSON array. Its default wildcard
    already permits LAN clients, so preserve that default when unset.
    """
    current = environment.get("CORS_ORIGINS")
    if current is None:
        return
    try:
        origins = json.loads(current)
    except (TypeError, ValueError) as error:
        raise RunError("CORS_ORIGINS must be a JSON array of origins") from error
    if not isinstance(origins, list) or any(not isinstance(origin, str) for origin in origins):
        raise RunError("CORS_ORIGINS must be a JSON array of origins")
    if "*" in origins:
        return
    for origin in [f"http://localhost:{frontend_port}", f"http://127.0.0.1:{frontend_port}", *extra_origins]:
        if origin not in origins:
            origins.append(origin)
    environment["CORS_ORIGINS"] = json.dumps(origins)


def service_environment(plan: dict) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CONNECTOR_BACKEND_PORT"] = str(plan["ports"]["backend"])
    environment["CONNECTOR_BACKEND_URL"] = plan["urls"]["backend"]
    environment["VITE_BACKEND_URL"] = plan["urls"]["backend"]
    environment["KOKORO_BASE_URL"] = plan["urls"]["kokoro"]
    if "cors_origins" in plan:
        environment["CORS_ORIGINS"] = plan["cors_origins"]
    return environment


def print_plan(plan: dict) -> None:
    for label, requested in plan["requested_ports"].items():
        actual = plan["ports"][label]
        if actual != requested:
            print(f"[run] {label.title()} port {requested} is busy or reserved; using {actual}.")
    print("=" * 62)
    print("Starting The Connector Full-Stack Services")
    print(f"Backend:  {plan['urls']['backend']} (API docs: {plan['urls']['backend']}/docs)")
    print(f"Frontend: {plan['urls']['frontend']}")
    print(f"Kokoro:   {plan['urls']['kokoro']} (speech backend)")
    for url in plan["lan_urls"]["frontend"]:
        print(f"LAN:      {url}")
    for url in plan["lan_urls"]["backend"]:
        print(f"LAN API:  {url}/docs")
    for url in plan["lan_urls"]["kokoro"]:
        print(f"LAN TTS:  {url}/health")
    if plan["lan_urls"]["frontend"]:
        print("Open a LAN URL on another device connected to the same network.")
    print("Press Ctrl+C to stop all services.")
    print("=" * 62, flush=True)


def in_active_environment() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or bool(
        os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX")
    )


def select_runtime() -> Path:
    runtime = Path(sys.executable) if in_active_environment() else LOCAL_PYTHON
    if not runtime.is_file() or not KOKORO_PYTHON.is_file():
        print("[run] Required environment is missing; running setup...", flush=True)
        if subprocess.call([sys.executable, str(ROOT / "setup.py")], cwd=ROOT):
            raise RunError("Setup failed")
    if not runtime.is_file():
        raise RunError(f"Python environment not found at {runtime}")
    if not KOKORO_PYTHON.is_file():
        raise RunError(f"Required Kokoro environment not found at {KOKORO_PYTHON}")
    return runtime


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    # Services get their own process groups so reload and npm descendants stop too.
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    elif process.poll() is None:
        process.terminate()
    else:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)


def main() -> int:
    args = parse_args()
    load_env_file()
    if args.print_plan_json:
        print(json.dumps(make_plan(args)))
        return 0
    runtime = select_runtime()
    if Path(sys.executable).resolve() != runtime.resolve():
        return subprocess.call([str(runtime), str(ROOT / "run.py"), *sys.argv[1:]], cwd=ROOT)
    # Retain the Windows job-object supervisor for console-close process cleanup.
    if os.name == "nt":
        command = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "run.ps1"), "-PythonExecutable", sys.executable]
        for key, parameter in (("host", "-BindHost"), ("backend_port", "-BackendPort"), ("frontend_port", "-UiPort"), ("kokoro_port", "-KokoroPort")):
            if getattr(args, key) is not None:
                command.extend([parameter, str(getattr(args, key))])
        if args.local:
            command.append("-Local")
        if args.no_reload:
            command.append("-NoReload")
        if args.completion_no_database_access:
            command.append("-CompletionNoDatabaseAccess")
        if not args.no_reload:
            command.append("-Reload")
        return subprocess.call(command, cwd=ROOT)
    plan = make_plan(args)
    npm = shutil.which("npm")
    if npm is None or not (FRONTEND / "node_modules").is_dir():
        raise RunError("Frontend dependencies are missing; run setup first")
    print_plan(plan)
    environment = service_environment(plan)
    backend_command = [sys.executable, str(ROOT / "run_backend.py"), "--host", plan["host"], "--port", str(plan["ports"]["backend"]), "--strict-port"]
    if not args.no_reload:
        backend_command.append("--reload")
    if args.completion_no_database_access:
        backend_command.append("--completion-no-database-access")
    commands = [
        ("Connector API", backend_command, ROOT),
        ("Kokoro speech", [str(KOKORO_PYTHON), "-u", str(ROOT / "python-kokoro" / "server.py"), "--host", plan["host"], "--port", str(plan["ports"]["kokoro"]), "--device", "auto"], ROOT / "python-kokoro"),
        ("Frontend", [npm, "run", "dev", "--", "--host", plan["host"], "--port", str(plan["ports"]["frontend"]), "--strictPort"], FRONTEND),
    ]
    processes = []
    def handle_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        for label, command, directory in commands:
            processes.append((label, subprocess.Popen(command, cwd=directory, env=environment, start_new_session=True)))
        while True:
            for label, process in processes:
                status = process.poll()
                if status is not None:
                    print(f"[run] {label} exited with code {status}; stopping all services.")
                    return status if status else 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[run] Stopping services...")
        return 0
    finally:
        for _label, process in reversed(processes):
            stop(process)
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RunError, OSError) as error:
        print(f"[run] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
