"""Find a real Python interpreter for child scripts and virtual environments."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


class PythonRuntimeError(RuntimeError):
    """The separately installed execution runtime is unavailable."""


def runtime_python_executable() -> str:
    """Never use the frozen application as a ``python -m/-c`` interpreter.

    The executable bundles Python for its own API, but not a general-purpose
    interpreter for agent tools and plugin virtual environments.
    """
    explicit = os.environ.get("CONNECTOR_PYTHON_EXECUTABLE", "").strip()
    if explicit:
        candidate = Path(os.path.expandvars(explicit)).expanduser()
        if not candidate.is_file() or (
            getattr(sys, "frozen", False)
            and candidate.resolve() == Path(sys.executable).resolve()
        ):
            raise PythonRuntimeError(
                "CONNECTOR_PYTHON_EXECUTABLE must point to an installed Python interpreter, "
                "not server.exe."
            )
        return str(candidate.resolve())
    if not getattr(sys, "frozen", False):
        return sys.executable
    for command in ("python", "python3"):
        executable = shutil.which(command)
        if executable and Path(executable).resolve() != Path(sys.executable).resolve():
            return executable
    raise PythonRuntimeError(
        "Python is required for agent scripts and plugin environments. Install Python "
        "and set CONNECTOR_PYTHON_EXECUTABLE in the environment or the .env beside server.exe."
    )
