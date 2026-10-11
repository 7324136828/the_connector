#!/usr/bin/env python3
"""Build portable Debug (local) and Release (LAN) Windows applications."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import venv
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
OUTPUT = ROOT / "dist"
WORK = ROOT / "build"
LOCAL_VENV = ROOT / ".venv"
LOCAL_PYTHON = LOCAL_VENV / "Scripts" / "python.exe"
APPLICATION = "server"
PORT_MAPPING = ROOT / "port_mapping.json"
PROFILES = {"Debug": "127.0.0.1", "Release": "0.0.0.0"}
ARTIFACTS = ("server.exe", "public", "run.bat", "runtime.json", "port_mapping.json")
INITIAL_CONFIGURATION = "config.json"

GENAI_HOOK = """from PyInstaller.utils.hooks import collect_submodules, is_module_or_submodule

hiddenimports = collect_submodules(
    "google.genai",
    filter=lambda name: not is_module_or_submodule(name, "google.genai.tests")
    and name != "google.genai._test_api_client",
)
"""

LAUNCHER = """@echo off
setlocal
if not exist "%~dp0server.exe" (
  echo [run] ERROR: Backend executable is missing. Rebuild with build.bat. 1>&2
  exit /b 1
)
"%~dp0server.exe" --initialize-settings
if errorlevel 1 exit /b %ERRORLEVEL%
"%~dp0server.exe" %*
exit /b %ERRORLEVEL%
"""


class BuildError(RuntimeError):
    """Raised when a portable build cannot be completed."""


def validate_port_defaults(path: Path) -> dict[str, int]:
    from backend.app.user_settings import UserSettingsError, load_port_mapping

    try:
        return load_port_mapping(path)
    except (UserSettingsError, OSError) as error:
        raise BuildError(f"Invalid port mapping at {path}: {error}") from error


def checked_path(path: Path, parent: Path) -> Path:
    """Reject symlinks/junctions that resolve outside the intended directory."""
    resolved = path.resolve()
    allowed = parent.resolve()
    if resolved == allowed or not resolved.is_relative_to(allowed):
        raise BuildError(f"Unsafe build path outside {allowed}: {path}")
    return resolved


def run(
    command: list[str | Path], *, cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    rendered = [str(part) for part in command]
    print(f"[build] $ {subprocess.list2cmdline(rendered)}", flush=True)
    completed = subprocess.run(rendered, cwd=cwd, env=env, check=False)
    if completed.returncode:
        raise BuildError(f"Command failed with exit code {completed.returncode}")


def select_python() -> Path:
    if (
        sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        or os.environ.get("VIRTUAL_ENV")
        or os.environ.get("CONDA_PREFIX")
    ):
        print(f"[build] Using active Python environment: {sys.prefix}")
        return Path(sys.executable)
    if not LOCAL_PYTHON.is_file():
        checked_path(LOCAL_VENV, ROOT)
        print(f"[build] Creating Python environment at {LOCAL_VENV}")
        venv.EnvBuilder(with_pip=True).create(LOCAL_VENV)
    if not LOCAL_PYTHON.is_file():
        raise BuildError(f"Python environment has no interpreter: {LOCAL_PYTHON}")
    return LOCAL_PYTHON


def pyinstaller_command(python: Path, stage: Path) -> list[str | Path]:
    command: list[str | Path] = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--noupx",
        "--name", APPLICATION,
        "--distpath", stage / "frozen",
        "--workpath", stage / "pyinstaller",
        "--specpath", stage,
        "--paths", ROOT,
        "--additional-hooks-dir", stage / "hooks",
    ]
    # Uvicorn loads these configured implementations by their string names.
    for module in (
        "backend.app.main",
        "backend.network",
        "google.genai",
        "uvicorn.logging",
        "uvicorn.loops.asyncio",
        "uvicorn.lifespan.on",
        "uvicorn.protocols.http.h11_impl",
    ):
        command.extend(["--hidden-import", module])
    # Provider SDK resources are loaded lazily through importlib; static import
    # analysis alone can miss classes used by chat and model-list requests.
    for package in ("openai", "anthropic"):
        command.extend(["--collect-submodules", package])
    command.append(BACKEND / "packaged.py")
    return command


def prepare_artifacts(stage: Path, *, defaults_path: Path = PORT_MAPPING) -> Path:
    """Create two portable packages from the shared frontend and executable."""
    validate_port_defaults(defaults_path)
    packages = stage / "packages"
    checked_path(packages, stage)
    packages.mkdir()
    for profile, host in PROFILES.items():
        package = packages / profile
        checked_path(package, packages)
        package.mkdir()
        shutil.copy2(stage / "server.exe", package / "server.exe")
        shutil.copytree(stage / "public", package / "public")
        (package / "run.bat").write_text(LAUNCHER, encoding="ascii", newline="\r\n")
        (package / "runtime.json").write_text(
            json.dumps({"host": host}, indent=2) + "\n", encoding="ascii",
        )
        shutil.copy2(defaults_path, package / "port_mapping.json")
        # Routing configuration is intentionally outside the one-file executable
        # so users can edit it. Never copy API credentials from the source .env.
        shutil.copy2(stage / INITIAL_CONFIGURATION, package / INITIAL_CONFIGURATION)
    return packages


def validate_artifacts(stage: Path, *, host: str) -> None:
    if not (stage / "server.exe").is_file():
        raise BuildError("PyInstaller did not produce the backend executable")
    if not (stage / "public" / "index.html").is_file():
        raise BuildError("Vite did not produce public/index.html")
    validate_port_defaults(stage / "port_mapping.json")
    try:
        configuration = json.loads((stage / INITIAL_CONFIGURATION).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"Invalid packaged routing configuration: {error}") from error
    if not isinstance(configuration, dict):
        raise BuildError("Packaged routing configuration must be a JSON object")
    if json.loads((stage / "runtime.json").read_text(encoding="ascii")) != {"host": host}:
        raise BuildError(f"Unexpected runtime defaults in {stage}")
    batch_files = [path.relative_to(stage) for path in stage.rglob("*")
                   if path.is_file() and path.suffix.lower() == ".bat"]
    if batch_files != [Path("run.bat")]:
        raise BuildError(f"Unexpected batch files in portable artifacts: {batch_files}")


def smoke_test(package: Path, workspace: Path, *, timeout: float = 45.0) -> None:
    """Verify the frozen application starts and serves both listeners.

    /api/health depends on the separately configured Kokoro service. API docs
    exercise backend imports without requiring speech or any provider keys.
    All state created by this check belongs to the disposable build workspace.
    """
    checked_path(workspace, package.parent.parent)
    workspace.mkdir()
    (workspace / "temp").mkdir()
    environment = os.environ.copy()
    environment.update({
        "APPDATA": str(workspace / "settings"),
        "CONNECTOR_SETTINGS_PATH": str(workspace / "settings" / "settings.json"),
        "SETTINGS_PATH": str(workspace / "settings" / "settings.json"),
        "TEMP": str(workspace / "temp"),
        "TMP": str(workspace / "temp"),
        "DATA_DIR": str(workspace / "data"),
        "DB_PATH": str(workspace / "data" / "connector.db"),
        "PLUGIN_DIR": str(workspace / "data" / "plugins"),
        "RESPONSE_LOG_DIR": str(workspace / "data" / "logs"),
        "AGENT_PYTHON_ENV_DIR": str(workspace / "data" / "python"),
        "AGENT_PYTHON_ENVS_DIR": str(workspace / "data" / "python-envs"),
        "JOBS_DIR": str(workspace / "data" / "jobs"),
    })
    defaults = validate_port_defaults(package / "port_mapping.json")

    settings_path = Path(environment["CONNECTOR_SETTINGS_PATH"])

    def initialize_settings(
        launcher_root: Path = package, settings_env: dict[str, str] = environment,
    ) -> dict:
        initialized = subprocess.run(
            [str(launcher_root / "run.bat"), "--initialize-settings"],
            cwd=launcher_root, env=settings_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=15,
        )
        if initialized.returncode:
            raise BuildError("Frozen settings initialization failed: "
                             + initialized.stdout.decode("utf-8", errors="replace"))
        path = Path(settings_env["CONNECTOR_SETTINGS_PATH"])
        return json.loads(path.read_text(encoding="utf-8-sig"))

    # Use the generated launcher itself for both first startup and a partial
    # preferences file. Neither case may need an installed Python interpreter.
    saved_settings = initialize_settings()
    if any(saved_settings.get(name) != value for name, value in defaults.items()):
        raise BuildError(f"Frozen settings initialization produced unexpected defaults: {saved_settings}")
    # Prove the frozen executable reads its delivered sidecar rather than using
    # compiled defaults. Only this disposable launcher copy uses modified ports.
    custom_package = checked_path(workspace / "custom-defaults", workspace)
    custom_package.mkdir()
    for name in ("server.exe", "run.bat"):
        shutil.copy2(package / name, custom_package / name)
    custom_defaults = {name: (port + 300) % 65535 + 1 for name, port in defaults.items()}
    (custom_package / "port_mapping.json").write_text(json.dumps(custom_defaults), encoding="utf-8")
    custom_environment = dict(environment)
    custom_environment["CONNECTOR_SETTINGS_PATH"] = str(workspace / "custom-settings" / "settings.json")
    custom_environment["SETTINGS_PATH"] = custom_environment["CONNECTOR_SETTINGS_PATH"]
    if initialize_settings(custom_package, custom_environment) != custom_defaults:
        raise BuildError("Frozen launcher did not initialize ports from its modified sidecar")
    partial_settings = {"react_port": defaults["react_port"] % 65535 + 1, "smoke_setting": "preserved"}
    settings_path.write_text(json.dumps(partial_settings), encoding="utf-8")
    saved_settings = initialize_settings()
    expected_settings = dict(defaults, **partial_settings)
    if saved_settings != expected_settings:
        raise BuildError(f"Frozen launcher changed partial settings: {saved_settings}")
    print(f"[build] Frozen launcher settings checks passed: {package.name} (delivered/custom mapping)", flush=True)
    # Reserve distinct port numbers together, then release them for the child.
    with socket.socket() as backend, socket.socket() as frontend:
        backend.bind(("127.0.0.1", 0))
        frontend.bind(("127.0.0.1", 0))
        backend_port = backend.getsockname()[1]
        frontend_port = frontend.getsockname()[1]
    saved_settings.update({"react_port": frontend_port, "connector_port": backend_port})
    settings_path.write_text(json.dumps(saved_settings), encoding="utf-8")
    # Exercise the delivered batch preflight and saved ports without CLI port
    # overrides; both the preflight and the server must preserve existing values.
    command = [str(package / "run.bat"), "--host", "127.0.0.1"]
    print(f"[build] Starting frozen application smoke test: {package.name}", flush=True)
    opener = build_opener(ProxyHandler({}))
    log_path = workspace / "startup.log"
    with log_path.open("w+b") as log:
        process = subprocess.Popen(
            command, cwd=package, env=environment, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        failure = "Application did not become ready before the startup timeout"
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    failure = f"Application exited during startup (exit code {process.returncode})"
                    break
                try:
                    with opener.open(f"http://127.0.0.1:{backend_port}/docs", timeout=1) as response:
                        if response.status != 200:
                            continue
                    with opener.open(f"http://127.0.0.1:{frontend_port}/", timeout=1) as response:
                        if response.status != 200:
                            continue
                    with opener.open(f"http://127.0.0.1:{frontend_port}/api/settings", timeout=1) as response:
                        preferences = json.loads(response.read())
                    if (
                        Path(preferences.get("data_dir", "")).resolve() != (workspace / "data").resolve()
                        or Path(preferences.get("settings_path", "")).resolve()
                        != (workspace / "settings" / "settings.json").resolve()
                    ):
                        raise BuildError(f"Frozen application used unexpected storage paths: {preferences}")
                    # An unmatched API path must reach the backend and retain
                    # its JSON error, rather than returning the frontend HTML.
                    try:
                        with opener.open(Request(f"http://127.0.0.1:{frontend_port}/v1/__build_check__"), timeout=1):
                            continue
                    except HTTPError as response:
                        with response:
                            if response.code != 404 or json.loads(response.read()) != {"detail": "Not Found"}:
                                continue
                    else:
                        continue
                    if json.loads(settings_path.read_text(encoding="utf-8-sig")) != saved_settings:
                        raise BuildError("Portable launcher changed existing settings values")
                    print(f"[build] Frozen application smoke test passed: {package.name}", flush=True)
                    return
                except (OSError, URLError, ValueError):
                    pass
                time.sleep(0.1)
            log.flush()
            log.seek(0)
            details = log.read().decode("utf-8", errors="replace")[-8000:]
            raise BuildError(f"{failure}. Startup log: {log_path}\n{details}")
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # A one-file PyInstaller application has a bootloader parent
                    # and an application child. Killing only the parent leaves
                    # the child serving requests and locking its working folder.
                    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                    stopped = subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
                    )
                    if stopped.returncode and process.poll() is None:
                        raise BuildError("Could not stop the frozen smoke-test process tree: "
                                         + stopped.stdout.decode("utf-8", errors="replace"))
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def publish_artifacts(stage: Path, *, repository: Path = ROOT) -> None:
    """Publish both profiles together, preserving their data and local settings."""
    output = repository / "dist"
    checked_path(output, repository)
    checked_path(stage, repository / "build")
    output.mkdir(exist_ok=True)
    previous = stage / "previous"
    checked_path(previous, stage)
    previous.mkdir()
    installed: list[tuple[str, str]] = []
    backed_up: list[tuple[str, str]] = []
    artifact_names: dict[str, tuple[str, ...]] = {}
    # Check every mutation target before moving anything. In particular, a
    # junction used for one output must not leave another output half updated.
    for profile in PROFILES:
        checked_path(stage / profile, stage)
        checked_path(output / profile, output)
        checked_path(previous / profile, previous)
        # Once installed, config.json belongs to the user like .env/settings.
        # New installations receive the source routing config; rebuilds retain it.
        configuration = output / profile / INITIAL_CONFIGURATION
        checked_path(configuration, output / profile)
        artifact_names[profile] = ARTIFACTS + (() if configuration.exists() else (INITIAL_CONFIGURATION,))
        for name in artifact_names[profile]:
            checked_path(stage / profile / name, stage / profile)
            checked_path(output / profile / name, output / profile)
            checked_path(previous / profile / name, previous / profile)
    for profile in PROFILES:
        (output / profile).mkdir(exist_ok=True)
        (previous / profile).mkdir()
    try:
        for profile in PROFILES:
            for name in artifact_names[profile]:
                source = stage / profile / name
                target = output / profile / name
                backup = previous / profile / name
                checked_path(source, stage / profile)
                checked_path(target, output / profile)
                checked_path(backup, previous / profile)
                if target.exists():
                    target.rename(backup)
                    backed_up.append((profile, name))
                source.rename(target)
                installed.append((profile, name))
    except OSError as error:
        # A locked artifact in either profile restores both previous packages.
        # User state in each distribution is never moved.
        for profile, name in reversed(installed):
            target = output / profile / name
            checked_path(target, output / profile)
            target.rename(stage / profile / name)
        for profile, name in reversed(backed_up):
            backup = previous / profile / name
            checked_path(backup, previous / profile)
            backup.rename(output / profile / name)
        raise BuildError(
            "Could not replace Debug/Release artifacts; close the running application "
            f"and retry. {error}"
        ) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install", action="store_true",
        help="Reuse installed Python/npm dependencies instead of installing them",
    )
    options = parser.parse_args(argv)
    if os.name != "nt":
        raise BuildError("Build the Windows executable on Windows")
    if sys.version_info < (3, 11):
        raise BuildError("Python 3.11 or newer is required (plugins use tomllib)")
    # Fail before installing dependencies, testing, or compiling if the shipped
    # source of default ports is missing or invalid.
    validate_port_defaults(PORT_MAPPING)
    npm = shutil.which("npm.cmd")
    if npm is None:
        raise BuildError("Node.js and npm were not found on PATH")
    if not (BACKEND / "packaged.py").is_file():
        raise BuildError("Backend packaged entrypoint is missing")
    checked_path(WORK, ROOT)
    WORK.mkdir(exist_ok=True)
    python = select_python()
    if not options.skip_install:
        run([python, "-m", "pip", "install", "--cache-dir", WORK / "pip-cache",
             "-r", BACKEND / "requirements.txt",
             "-r", BACKEND / "requirements-build.txt"])
        run([npm, "ci", "--cache", WORK / "npm-cache"], cwd=FRONTEND)
    run([npm, "test"], cwd=FRONTEND)

    stage = WORK / ("portable-" + uuid.uuid4().hex)
    checked_path(stage, WORK)
    stage.mkdir()
    published = False
    try:
        run([npm, "run", "build", "--", "--outDir", stage / "public"], cwd=FRONTEND)
        shutil.copy2(ROOT / INITIAL_CONFIGURATION, stage / INITIAL_CONFIGURATION)
        hooks = checked_path(stage / "hooks", stage)
        hooks.mkdir()
        (hooks / "hook-google.genai.py").write_text(GENAI_HOOK, encoding="utf-8")
        freeze_env = os.environ.copy()
        freeze_env["PYINSTALLER_CONFIG_DIR"] = str(WORK / "pyinstaller-cache")
        checked_path(WORK / "pyinstaller-cache", WORK)
        run(pyinstaller_command(python, stage), env=freeze_env)
        frozen = stage / "frozen" / f"{APPLICATION}.exe"
        checked_path(frozen, stage)
        frozen.rename(stage / "server.exe")
        # Validate both portable folders rather than PyInstaller's work/spec files.
        artifacts = prepare_artifacts(stage)
        for profile, host in PROFILES.items():
            validate_artifacts(artifacts / profile, host=host)
            run([artifacts / profile / "server.exe", "--help"])
            smoke_test(artifacts / profile, stage / f"smoke-{profile.lower()}")
        publish_artifacts(artifacts)
        published = True
    finally:
        if published:
            checked_path(stage, WORK)
            shutil.rmtree(stage)
        else:
            print(f"[build] Build files retained for troubleshooting: {stage}")

    print(f"\n[build] Ready: {OUTPUT / 'Debug'} (local) and {OUTPUT / 'Release'} (LAN)")
    print("[build] Local: dist\\Debug\\run.bat --frontend 4000 --backend 9000")
    print("[build] LAN:   dist\\Release\\run.bat --frontend 4000 --backend 9000")
    print("[build] Ports: settings.json; missing values are filled from port_mapping.json")
    print("[build] Kokoro, Ollama and Python/plugin execution runtimes are external services/tools.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError) as error:
        print(f"\n[build] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
