#!/usr/bin/env python3
"""Build portable Debug (local) and Release (LAN) Windows applications."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import venv
import uuid
import wave
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
KOKORO = ROOT / "python-kokoro"
OUTPUT = ROOT / "dist"
WORK = ROOT / "build"
LOCAL_VENV = ROOT / ".venv"
LOCAL_PYTHON = LOCAL_VENV / "Scripts" / "python.exe"
APPLICATION = "server"
PORT_MAPPING = ROOT / "port_mapping.json"
PROFILES = {"Debug": "127.0.0.1", "Release": "0.0.0.0"}
ARTIFACTS = ("server.exe", "python-kokoro", "public", "run.bat", "runtime.json", "port_mapping.json")
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


def pip_cache_options() -> list[str | Path]:
    # Explicit --cache-dir would otherwise override the CI environment setting.
    if os.environ.get("PIP_NO_CACHE_DIR") == "1":
        return ["--no-cache-dir"]
    return ["--cache-dir", WORK / "pip-cache"]


def select_kokoro_python(device: str, *, skip_install: bool, require_gpu: bool = True) -> Path:
    """Keep the speech freezer and PyTorch out of the API's Python environment."""
    environment = checked_path(WORK / f"kokoro-venv-{device}", WORK)
    python = environment / "Scripts" / "python.exe"
    if not python.is_file():
        if skip_install:
            raise BuildError(
                f"Prepared Kokoro build environment is missing: {python}. "
                f"Run build.bat --kokoro-device {device} once without --skip-install."
            )
        launcher = shutil.which("py.exe") or shutil.which("py")
        if launcher is None:
            raise BuildError("Install Python 3.12 and its Windows launcher to build Kokoro")
        run([launcher, "-3.12", "-m", "venv", environment])
    if not python.is_file():
        raise BuildError(f"Kokoro build environment has no interpreter: {python}")
    run([python, "-c", "import sys; assert sys.version_info[:2] == (3, 12), "
         "'The isolated Kokoro build environment must use Python 3.12'"])
    if not skip_install:
        if device == "cuda":
            run([python, "-m", "pip", "install", *pip_cache_options(),
                 "-r", KOKORO / "requirements-cuda.txt"])
        else:
            run([python, "-m", "pip", "install", *pip_cache_options(),
                 "torch==2.11.0", "--index-url", "https://download.pytorch.org/whl/cpu"])
        run([python, "-m", "pip", "install", *pip_cache_options(),
             "-r", KOKORO / "requirements.txt", "-r", KOKORO / "requirements-build.txt"])
    expected_cuda = device == "cuda"
    run([python, "-c", "import kokoro, torch, PyInstaller; "
         f"assert bool(torch.version.cuda) == {expected_cuda!r}, "
         "'Kokoro build environment has the wrong CPU/CUDA PyTorch build'"])
    if expected_cuda and require_gpu:
        # Loading CUDA-enabled wheels alone does not prove the local driver/GPU
        # can execute them. Fail before freezing a package that cannot run.
        run([python, "-c", "import torch; "
             "assert torch.cuda.is_available(), 'CUDA is not available to PyTorch'; "
             "assert torch.ones(1, device='cuda').add_(1).item() == 2; "
             "print('Kokoro CUDA preflight passed:', torch.cuda.get_device_name())"])
    return python


def kokoro_pyinstaller_command(python: Path, stage: Path) -> list[str | Path]:
    """Freeze the independent speech service with its complete native runtime."""
    command: list[str | Path] = [
        python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--console", "--noupx", "--name", "kokoro",
        "--distpath", stage / "kokoro-frozen",
        "--workpath", stage / "kokoro-pyinstaller", "--specpath", stage,
        "--add-data", f"{stage / 'kokoro-model'}{os.pathsep}kokoro-model",
    ]
    for package in ("kokoro", "misaki", "en_core_web_sm", "espeakng_loader"):
        command.extend(["--collect-all", package])
    # The phonemizer's transitive CSVW imports open the language registry JSON;
    # static module analysis does not include these files or phonemizer profiles.
    for package in ("language_tags", "phonemizer"):
        command.extend(["--collect-data", package])
    for package in ("kokoro", "torch", "transformers", "spacy", "en-core-web-sm", "phonemizer-fork"):
        command.extend(["--copy-metadata", package])
    # Kokoro imports AlbertConfig/AlbertModel through Transformers' lazy API.
    for module in (
        "transformers.models.albert.configuration_albert",
        "transformers.models.albert.modeling_albert",
    ):
        command.extend(["--hidden-import", module])
    command.append(KOKORO / "server.py")
    return command


def kokoro_bundle_device(bundle: Path) -> str:
    try:
        configuration = json.loads((bundle / "runtime.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"Invalid Kokoro runtime defaults in {bundle}: {error}") from error
    if not isinstance(configuration, dict) or configuration.get("device") not in ("cpu", "cuda"):
        raise BuildError(f"Invalid Kokoro runtime device in {bundle}; expected cpu or cuda")
    return configuration["device"]


def validate_kokoro_bundle(bundle: Path) -> None:
    device = kokoro_bundle_device(bundle)
    if not (bundle / "kokoro.exe").is_file():
        raise BuildError(f"Kokoro executable is missing from {bundle}")
    runtime = bundle / "_internal"
    if not runtime.is_dir() or not any(runtime.glob("python3*.dll")):
        raise BuildError(f"Kokoro Python runtime is missing from {bundle}")
    if not (runtime / "torch" / "lib" / "torch_cpu.dll").is_file():
        raise BuildError(f"Kokoro PyTorch runtime is missing from {bundle}")
    if device == "cuda":
        libraries = runtime / "torch" / "lib"
        if not (libraries / "torch_cuda.dll").is_file() or not any(libraries.glob("cudart64*.dll")):
            raise BuildError(f"Kokoro CUDA PyTorch runtime is missing from {bundle}")
    if not (runtime / "language_tags" / "data" / "json" / "index.json").is_file():
        raise BuildError(f"Kokoro phonemizer language registry is missing from {bundle}")
    for name in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
        if not (runtime / "kokoro-model" / name).is_file():
            raise BuildError(f"Kokoro model asset {name} is missing from {bundle}")


def prepare_artifacts(stage: Path, *, defaults_path: Path = PORT_MAPPING) -> Path:
    """Create two portable packages from the shared frontend and executable."""
    validate_port_defaults(defaults_path)
    validate_kokoro_bundle(stage / "python-kokoro")
    packages = stage / "packages"
    checked_path(packages, stage)
    packages.mkdir()
    for profile, host in PROFILES.items():
        package = packages / profile
        checked_path(package, packages)
        package.mkdir()
        shutil.copy2(stage / "server.exe", package / "server.exe")
        shutil.copytree(stage / "python-kokoro", package / "python-kokoro")
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
    validate_kokoro_bundle(stage / "python-kokoro")
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


def validate_kokoro_health(health: dict, device: str, *, model_loaded: bool = False) -> None:
    """Prove the delivered setting and loaded model both use the selected device."""
    if health.get("device") != device:
        raise BuildError(f"Frozen Kokoro is using {health.get('device')!r}; expected {device}")
    if model_loaded:
        model_devices = health.get("modelDevices")
        if not isinstance(model_devices, dict) or not model_devices:
            raise BuildError("Frozen Kokoro did not report loaded model devices after synthesis")
        if any(not isinstance(actual, str) or (actual != "cpu" if device == "cpu"
                                              else not actual.startswith("cuda"))
               for actual in model_devices.values()):
            raise BuildError(f"Frozen Kokoro model used unexpected devices: {model_devices}; expected {device}")


def smoke_test(package: Path, workspace: Path, *, timeout: float = 120.0) -> None:
    """Verify all three frozen listeners and synthesize speech through the UI API.

    All state created by this check belongs to the disposable build workspace.
    Bundled English assets must make the speech check work without downloading.
    """
    device = kokoro_bundle_device(package / "python-kokoro")
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
        "HF_HOME": str(workspace / "huggingface"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    })
    # Exercise the delivered assets rather than any developer asset override.
    environment.pop("KOKORO_MODEL_DIR", None)
    environment.pop("KOKORO_BASE_URL", None)
    environment.pop("KOKORO_DEVICE", None)
    environment.pop("PODCAST_DEVICE", None)
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
    with socket.socket() as backend, socket.socket() as frontend, socket.socket() as kokoro:
        backend.bind(("127.0.0.1", 0))
        frontend.bind(("127.0.0.1", 0))
        kokoro.bind(("127.0.0.1", 0))
        backend_port = backend.getsockname()[1]
        frontend_port = frontend.getsockname()[1]
        kokoro_port = kokoro.getsockname()[1]
    saved_settings.update({"react_port": frontend_port, "connector_port": backend_port,
                           "kokoro_port": kokoro_port})
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
                    with opener.open(f"http://127.0.0.1:{backend_port}/api/health", timeout=5) as response:
                        health = json.loads(response.read())
                        if response.status != 200 or health.get("status") != "ok":
                            continue
                    with opener.open(f"http://127.0.0.1:{frontend_port}/", timeout=1) as response:
                        if response.status != 200:
                            continue
                    with opener.open(f"http://127.0.0.1:{frontend_port}/api/speech/health", timeout=5) as response:
                        speech = json.loads(response.read())
                        if (response.status != 200 or speech.get("status") != "ok"
                                or speech.get("service") != "python-kokoro"):
                            continue
                        validate_kokoro_health(speech, device)
                    with opener.open(f"http://127.0.0.1:{frontend_port}/api/speech/voices", timeout=5) as response:
                        voices = json.loads(response.read())
                        if response.status != 200 or "af_heart" not in voices.get("voices", []):
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
                    request = Request(
                        f"http://127.0.0.1:{frontend_port}/api/speech",
                        data=json.dumps({"content": "Kokoro is ready.", "actor": "af_heart"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    print(f"[build] Checking frozen offline speech synthesis: {package.name}", flush=True)
                    try:
                        with opener.open(request, timeout=120) as response:
                            audio = response.read()
                            if response.status != 200:
                                raise BuildError("Frozen speech synthesis did not return HTTP 200")
                        with wave.open(io.BytesIO(audio), "rb") as wav:
                            if wav.getframerate() != 24000 or wav.getnframes() == 0:
                                raise BuildError("Frozen speech synthesis returned empty or invalid WAV audio")
                    except (OSError, URLError, wave.Error, EOFError) as error:
                        raise BuildError(f"Frozen offline speech synthesis failed: {error}") from error
                    with opener.open(f"http://127.0.0.1:{frontend_port}/api/speech/health", timeout=5) as response:
                        if response.status != 200:
                            raise BuildError("Frozen Kokoro health check failed after synthesis")
                        validate_kokoro_health(json.loads(response.read()), device, model_loaded=True)
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


def smoke_test_cuda_on_cpu(package: Path, workspace: Path) -> None:
    """Exercise the CUDA bundle without a GPU, restoring its delivered device.

    This verifies frozen imports, listeners, and offline speech on CPU. CUDA
    kernels still need validation on a machine with a compatible NVIDIA GPU.
    """
    bundle = package / "python-kokoro"
    if kokoro_bundle_device(bundle) != "cuda":
        raise BuildError("CPU validation of a CUDA package requires a CUDA bundle")
    profile = bundle / "runtime.json"
    original = profile.read_bytes()
    print(f"[build] Testing CUDA bundle on CPU; GPU execution is not tested: {package.name}", flush=True)
    try:
        profile.write_text(json.dumps({"device": "cpu"}) + "\n", encoding="ascii")
        smoke_test(package, workspace)
    finally:
        profile.write_bytes(original)


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
    parser.add_argument(
        "--kokoro-device", choices=("cpu", "cuda"), default="cuda",
        help="Bundle CUDA PyTorch (default); use cpu explicitly for machines without a CUDA GPU",
    )
    parser.add_argument(
        "--cuda-smoke-device", choices=("cpu", "cuda"), default="cuda",
        help="Test CUDA packages on cuda (default), or on cpu for CI without a GPU; "
             "the delivered CUDA package still requires a GPU",
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
    kokoro_python = select_kokoro_python(
        options.kokoro_device, skip_install=options.skip_install,
        require_gpu=options.cuda_smoke_device == "cuda",
    )
    if not options.skip_install:
        run([python, "-m", "pip", "install", *pip_cache_options(),
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
        run([kokoro_python, KOKORO / "prepare_assets.py", stage / "kokoro-model"])
        run(kokoro_pyinstaller_command(kokoro_python, stage), env=freeze_env)
        kokoro_bundle = checked_path(stage / "kokoro-frozen" / "kokoro", stage)
        (kokoro_bundle / "runtime.json").write_text(
            json.dumps({"device": options.kokoro_device}, indent=2) + "\n", encoding="ascii",
        )
        validate_kokoro_bundle(kokoro_bundle)
        kokoro_bundle.rename(stage / "python-kokoro")
        # Validate both portable folders rather than PyInstaller's work/spec files.
        artifacts = prepare_artifacts(stage)
        for profile, host in PROFILES.items():
            validate_artifacts(artifacts / profile, host=host)
            run([artifacts / profile / "server.exe", "--help"])
            if options.kokoro_device == "cuda" and options.cuda_smoke_device == "cpu":
                smoke_test_cuda_on_cpu(artifacts / profile, stage / f"smoke-{profile.lower()}")
            else:
                smoke_test(artifacts / profile, stage / f"smoke-{profile.lower()}")
            validate_kokoro_bundle(artifacts / profile / "python-kokoro")
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
    print(f"[build] Kokoro ({options.kokoro_device}) is bundled and starts alongside the backend.")
    print("[build] Ollama and Python/plugin execution runtimes are external services/tools.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError) as error:
        print(f"\n[build] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
