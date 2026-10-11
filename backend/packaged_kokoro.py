"""Own the isolated speech executable without importing its Python packages."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

import httpx


class RunError(RuntimeError):
    """Raised when the portable application cannot start or keep running."""


class WindowsProcessJob:
    """The OS stops speech and its descendants if the Connector is closed."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_operations", "write_operations", "other_operations",
                "read_bytes", "write_bytes", "other_bytes",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def speech_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Kokoro has its own Python 3.12 bundle, independent of server.exe's Python.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundle_path = Path(bundle).resolve()
        environment["PATH"] = os.pathsep.join(
            item for item in environment.get("PATH", "").split(os.pathsep)
            if not item or not Path(item.strip('"')).resolve().is_relative_to(bundle_path)
        )
        if sys.platform != "win32":
            if "LD_LIBRARY_PATH_ORIG" in environment:
                environment["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH_ORIG"]
            else:
                environment.pop("LD_LIBRARY_PATH", None)
    return environment


@contextmanager
def independent_dll_search() -> Iterator[None]:
    # PyInstaller's SetDllDirectory is inherited by subprocesses on Windows.
    # Reset it during launch so Kokoro cannot load the backend's Python DLLs.
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    length = kernel.GetDllDirectoryW(0, None)
    buffer = ctypes.create_unicode_buffer(length + 1)
    kernel.GetDllDirectoryW(len(buffer), buffer)
    if not kernel.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.SetDllDirectoryW(buffer.value or None):
            raise ctypes.WinError(ctypes.get_last_error())


class KokoroProcess:
    """Start, verify, monitor, and stop exactly the bundled speech process."""

    def __init__(self, root: Path, port: int, *, log_directory: Path | None = None) -> None:
        self.executable = root / "python-kokoro" / "kokoro.exe"
        self.profile_path = self.executable.parent / "runtime.json"
        self.device: str | None = None
        self.base_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.log_path = (log_directory if log_directory is not None else root / "logs") / "kokoro.log"
        self.process: subprocess.Popen | None = None
        self.log: BinaryIO | None = None
        self.job: WindowsProcessJob | None = None

    def start(self) -> None:
        if not self.executable.is_file():
            raise RunError(
                f"Bundled Kokoro is missing at {self.executable}; rebuild with build.bat "
                "and keep the complete python-kokoro folder beside server.exe."
            )
        try:
            profile = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
            if not isinstance(profile, dict) or profile.get("device") not in ("cuda", "cpu"):
                raise ValueError("device must be cuda or cpu")
        except (OSError, ValueError) as error:
            raise RunError(
                f"Invalid or missing bundled Kokoro runtime profile at {self.profile_path}; "
                "rebuild with build.bat and keep the complete python-kokoro folder "
                "beside server.exe."
            ) from error
        self.device = profile["device"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            try:
                if os.name == "nt":
                    reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                reservation.bind(("127.0.0.1", self.port))
            except OSError as error:
                raise RunError(
                    f"Cannot start bundled Kokoro at {self.base_url}: the speech port is "
                    "already in use. Close the service using it, choose --kokoro-port, "
                    "or set KOKORO_BASE_URL to use an external speech service."
                ) from error
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = self.log_path.open("ab", buffering=0)
            if os.name == "nt":
                self.job = WindowsProcessJob()
            print(
                f"[run] Starting bundled Kokoro at {self.base_url} "
                f"(device: {self.device}; log: {self.log_path})", flush=True,
            )
            environment = speech_environment()
            environment["KOKORO_DEVICE"] = self.device
            with independent_dll_search():
                self.process = subprocess.Popen(
                    [str(self.executable), "--host", "127.0.0.1", "--port", str(self.port),
                     "--device", self.device],
                    cwd=self.executable.parent, env=environment,
                    stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            if self.job is not None:
                self.job.assign(self.process)
        except OSError as error:
            self.stop()
            raise RunError(f"Cannot launch bundled Kokoro: {error}. Read {self.log_path}") from error

    def check_running(self) -> None:
        if self.process is None:
            raise RunError("Bundled Kokoro has not been started")
        code = self.process.poll()
        if code is not None:
            raise RunError(
                f"Kokoro stopped unexpectedly (exit code {code}); "
                f"the Connector has been stopped. Read {self.log_path}"
            )

    async def wait_ready(self, stopped: asyncio.Event, timeout: float = 120.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(trust_env=False, timeout=2.0) as client:
            while not stopped.is_set():
                self.check_running()
                try:
                    response = await client.get(f"{self.base_url}/health")
                    if response.status_code == 200:
                        health = response.json()
                        if (
                            not isinstance(health, dict) or health.get("status") != "ok"
                            or health.get("service") != "python-kokoro"
                            or not str(health.get("python", "")).startswith("3.12.")
                        ):
                            raise RunError(
                                f"Unexpected speech service at {self.base_url}/health; "
                                "the bundle must run python-kokoro on Python 3.12. "
                                f"Read {self.log_path}"
                            )
                        if self.device is None or health.get("device") != self.device:
                            raise RunError(
                                f"Bundled Kokoro reported device {health.get('device')!r}, "
                                f"but this bundle requires {self.device!r}; the Connector "
                                f"has been stopped. Read {self.log_path}"
                            )
                        self.check_running()
                        print(f"[run] Kokoro is ready at {self.base_url} ({self.device})", flush=True)
                        return True
                except (httpx.RequestError, ValueError):
                    # PyTorch/CUDA initialization can take several seconds.
                    pass
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RunError(
                        f"Kokoro did not become ready at {self.base_url}/health within "
                        f"{timeout:g}s; the Connector has been stopped. Read {self.log_path}"
                    )
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=min(0.25, remaining))
                except asyncio.TimeoutError:
                    pass
        return False

    async def monitor(self) -> None:
        while True:
            self.check_running()
            await asyncio.sleep(0.25)

    def stop(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            if self.job is not None:
                self.job.close()
                self.job = None
            if self.log is not None:
                self.log.close()
                self.log = None
