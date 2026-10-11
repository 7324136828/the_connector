"""Persist storage and service-port preferences outside the application install."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

APP_DIRECTORY = "the_connector"
PORT_FIELDS = ("react_port", "connector_port", "kokoro_port")


class UserSettingsError(ValueError):
    """A saved preference or settings file is invalid."""


def default_data_directory() -> Path:
    return Path(tempfile.gettempdir()) / APP_DIRECTORY


def settings_file_path() -> Path:
    override = os.environ.get("CONNECTOR_SETTINGS_PATH") or os.environ.get("SETTINGS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIRECTORY / "settings.json"


def port_mapping_path() -> Path:
    """Use defaults beside the portable executable, or in the source project."""
    runtime_root = os.environ.get("CONNECTOR_RUNTIME_ROOT")
    if runtime_root:
        root = Path(runtime_root).resolve()
    elif getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parents[2]
    return root / "port_mapping.json"


def load_port_mapping(path: Path | None = None) -> dict[str, int]:
    """Read project defaults without creating or modifying user preferences."""
    path = path or port_mapping_path()
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserSettingsError(f"Cannot read port mapping at {path}: {error}") from error
    if not isinstance(values, dict):
        raise UserSettingsError(f"Port mapping at {path} must be a JSON object.")
    _validate_ports(values, path)
    ports = {name: values[name] for name in PORT_FIELDS}
    if len(set(ports.values())) != len(ports):
        raise UserSettingsError(f"Service ports in {path} must be different.")
    return ports


def resolve_data_directory(value: str) -> Path:
    """Resolve a host-side absolute path; an empty preference restores temp."""
    if not isinstance(value, str):
        raise UserSettingsError("Data storage folder must be a string.")
    value = value.strip()
    if not value:
        return default_data_directory()
    # Support Windows-style variables even when preferences are edited on Unix.
    value = re.sub(r"%([^%]+)%", lambda match: os.environ.get(match[1], match[0]), value)
    value = os.path.expandvars(value)
    if re.search(r"%[^%]+%|\$\{[^}]+\}|\$[A-Za-z_][A-Za-z_0-9]*", value):
        raise UserSettingsError("Data storage folder contains an undefined environment variable.")
    if any(ord(character) < 32 for character in value):
        raise UserSettingsError("Data storage folder contains an invalid character.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise UserSettingsError("Data storage folder must be an absolute path.")
    if path.exists() and not path.is_dir():
        raise UserSettingsError("Data storage folder must point to a folder, not a file.")
    return path.resolve()


def load_user_settings(path: Path | None = None) -> dict[str, Any]:
    path = path or settings_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _write_settings({}, path, create_only=True)
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UserSettingsError(f"Cannot read settings JSON at {path}: {error}") from error
    if not isinstance(values, dict):
        raise UserSettingsError(f"Settings at {path} must be a JSON object.")
    return values


def initialize_port_settings(
    path: Path | None = None, *, defaults_path: Path | None = None,
) -> dict[str, Any]:
    """Fill only missing ports, then reread the settings used for startup."""
    defaults = load_port_mapping(defaults_path)
    path = path or settings_file_path()
    values = load_user_settings(path)
    completed = dict(values)
    for name, default in defaults.items():
        completed.setdefault(name, default)
    _validate_ports(completed, path)
    if completed != values:
        _write_settings(completed, path)
    # Use the persisted values, including changes made by another startup.
    saved = load_user_settings(path)
    _validate_ports(saved, path)
    return saved


def _validate_ports(values: dict[str, Any], path: Path) -> None:
    for name in PORT_FIELDS:
        value = values.get(name)
        if type(value) is not int or not 1 <= value <= 65535:
            raise UserSettingsError(f"{name} in {path} must be an integer between 1 and 65535.")


def save_data_directory(value: str, path: Path | None = None) -> dict[str, Any]:
    resolve_data_directory(value)
    path = path or settings_file_path()
    values = load_user_settings(path)
    values["data_dir"] = value.strip()
    _write_settings(values, path)
    return values


def _write_settings(values: dict[str, Any], path: Path, *, create_only: bool = False) -> None:
    descriptor, staging_name = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=path.parent)
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            # Publish complete JSON without replacing another startup's file.
            try:
                os.link(staging, path)
            except FileExistsError:
                pass
        else:
            os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
