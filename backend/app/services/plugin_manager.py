"""Install ZIP plugins and supervise their Python backends."""
from __future__ import annotations

from contextlib import closing
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import zipfile

MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
logger = logging.getLogger(__name__)


class PluginError(ValueError):
    pass


class PluginNotFound(PluginError):
    pass


class PluginConflict(PluginError):
    pass


def archive_path(name):
    path = PurePosixPath(name)
    reserved = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
    if not name or path.is_absolute() or "\\" in name or any(
        part in {"", ".", ".."} or any(c in part for c in '<>:"|?*') or part.endswith((" ", "."))
        or reserved.match(part) or any(ord(c) < 32 for c in part)
        for part in name.rstrip("/").split("/")
    ):
        raise PluginError("Unsafe ZIP path")
    if not path.parts or any(part.startswith(".") for part in path.parts):
        raise PluginError("Hidden files and runtime environments cannot be installed from ZIP")
    return path


def validate_zip(payload):
    if len(payload) > MAX_ZIP_BYTES:
        raise PluginError("Plugin ZIP exceeds 10 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        files = archive.infolist()
        if not files or len(files) > 1000 or sum(f.file_size for f in files) > MAX_EXTRACTED_BYTES:
            raise PluginError("Plugin ZIP exceeds extraction limits")
        names = set()
        for item in files:
            path = archive_path(item.orig_filename)
            key = str(path).casefold()
            if key in names or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                raise PluginError("Duplicate, encrypted, or symbolic-link ZIP entry")
            names.add(key)
        required = {"instruction.md", "plugin.json", "ui/package.json", "backend/pyproject.toml", "run.bat", "run.sh"}
        if not required.issubset({f.filename for f in files if not f.is_dir()}):
            raise PluginError("ZIP must contain instruction.md, plugin.json, ui/package.json, backend/pyproject.toml, run.bat, and run.sh at its root")
        if archive.testzip() is not None:
            raise PluginError("Plugin ZIP contains a corrupt file")
        manifest = json.loads(archive.read("plugin.json"))
        if not isinstance(manifest, dict) or not ID_PATTERN.fullmatch(str(manifest.get("id", ""))):
            raise PluginError("Plugin ID must be lowercase snake_case")
        if not isinstance(manifest.get("version"), str) or not 0 < len(manifest["version"]) <= 64:
            raise PluginError("Plugin version is required")
        backend, ui, webhook = (manifest.get(key) for key in ("backend", "ui", "webhook"))
        if not all(isinstance(value, dict) for value in (backend, ui, webhook)):
            raise PluginError("backend, ui, and webhook objects are required")
        if backend.get("language") != "python" or backend.get("path") != "backend":
            raise PluginError("This installer supports Python backends under backend/")
        if not isinstance(backend.get("entry"), str) or not MODULE_PATTERN.fullmatch(backend["entry"]):
            raise PluginError("backend.entry must be module:function")
        entry = ui.get("entry")
        if not isinstance(entry, str) or not str(archive_path(entry)).startswith("ui/") or not entry.endswith(".js") or entry not in archive.namelist():
            raise PluginError("ui.entry must name a browser-ready JavaScript module under ui/")
        if webhook.get("method") != "POST" or not re.fullmatch(r"/[A-Za-z0-9_/-]+", str(webhook.get("path", ""))):
            raise PluginError("webhook must provide a POST path")
        if manifest.get("permissions", []) not in ([], ["connector-database:read"]):
            raise PluginError("Unsupported plugin permission")
        tool = manifest.get("tool", {})
        if not isinstance(tool, dict) or not isinstance(tool.get("parameters", {"type": "object"}), dict):
            raise PluginError("tool.parameters must be a JSON Schema object")
        return archive, manifest
    except PluginError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, RuntimeError, NotImplementedError) as exc:
        raise PluginError("Invalid plugin ZIP or manifest") from exc


class PluginManager:
    def __init__(self, db_path, directory, agent_provider):
        self.db_path = Path(db_path)
        self.directory = Path(directory).resolve()
        self.agent_provider = agent_provider
        self._lock = threading.RLock()
        self._running = {}
        self._errors = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS plugins (id TEXT PRIMARY KEY, manifest TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0)")

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def _folder(self, plugin_id):
        if not ID_PATTERN.fullmatch(plugin_id):
            raise PluginNotFound("Plugin not found")
        target = self.directory / plugin_id
        if target.resolve().parent != self.directory:
            raise PluginError("Plugin directory is outside the managed root")
        return target

    def get(self, plugin_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT manifest, enabled FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
        if row is None:
            raise PluginNotFound("Plugin not found")
        manifest = json.loads(row[0])
        running = self._running.get(plugin_id)
        alive = bool(running and running["process"].poll() is None)
        if row[1] and not alive and plugin_id not in self._errors:
            self._errors[plugin_id] = "Backend is not running; disable and enable the plugin to retry."
        return {"id": plugin_id, "version": manifest["version"], "enabled": bool(row[1]),
                "status": "running" if alive else "error" if plugin_id in self._errors else "disabled",
                "error": self._errors.get(plugin_id), "manifest": manifest}

    def list(self):
        with self._lock, closing(self._connect()) as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM plugins ORDER BY id")]
            return [self.get(plugin_id) for plugin_id in ids]

    def install(self, payload):
        archive, manifest = validate_zip(payload)
        plugin_id = manifest["id"]
        with self._lock, archive:
            try:
                self.get(plugin_id)
            except PluginNotFound:
                pass
            else:
                raise PluginConflict("Plugin is already installed; uninstall it before installing another version")
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self._folder(plugin_id)
            if target.exists():
                raise PluginConflict("A plugin folder with this ID already exists")
            with tempfile.TemporaryDirectory(prefix="install-", dir=self.directory) as staging:
                for item in archive.infolist():
                    destination = Path(staging).joinpath(*archive_path(item.filename).parts)
                    if item.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                Path(staging).rename(target)
                try:
                    with closing(self._connect()) as conn, conn:
                        conn.execute("INSERT INTO plugins(id, manifest) VALUES (?, ?)", (plugin_id, json.dumps(manifest)))
                except Exception:
                    shutil.rmtree(self._folder(plugin_id))
                    raise
            return self.get(plugin_id)

    @staticmethod
    def _run(command, folder):
        try:
            result = subprocess.run(command, cwd=folder, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=180,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PluginError(f"Plugin environment setup failed: {exc}") from exc
        if result.returncode:
            raise PluginError("Plugin environment setup failed: " + (result.stderr or result.stdout)[-2000:])

    def _prepare_runtime(self, folder):
        environment = folder / ".venv"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not (environment / "ready").exists() or not python.is_file():
            self._run([sys.executable, "-m", "venv", "--without-pip", str(environment)], folder)
            requirements = folder / "backend/requirements.txt"
            project = tomllib.loads((folder / "backend/pyproject.toml").read_text(encoding="utf-8"))
            dependencies = project.get("project", {}).get("dependencies", [])
            if not isinstance(dependencies, list) or any(not isinstance(dep, str) or dep.startswith("-") for dep in dependencies):
                raise PluginError("Python dependencies must be a list of package requirements")
            has_requirements = requirements.is_file() and requirements.read_text(encoding="utf-8").strip()
            if has_requirements or dependencies:
                self._run([str(python), "-m", "ensurepip"], folder)
            if dependencies:
                self._run([str(python), "-m", "pip", "install", "--", *dependencies], folder)
            if has_requirements:
                self._run([str(python), "-m", "pip", "install", "-r", str(requirements)], folder)
            (environment / "ready").touch()
        return str(python)

    def enable(self, plugin_id):
        with self._lock:
            record = self.get(plugin_id)
            running = self._running.get(plugin_id)
            if running and running["process"].poll() is None:
                return record
            if running:
                self._stop(plugin_id)
            agent = self.agent_provider()
            if any(tool.name == plugin_id for tool in agent.list_tools()):
                raise PluginConflict("An agent tool with this name is already registered")
            folder = self._folder(plugin_id)
            try:
                python = self._prepare_runtime(folder)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                manifest = record["manifest"]
                module, entry_function = manifest["backend"]["entry"].split(":")
                env = {**os.environ, "PYTHONPATH": str(folder / "backend"), "PYTHONUNBUFFERED": "1"}
                # Import and invoke exactly the module:function specified by the manifest.
                bootstrap = "import importlib, sys; getattr(importlib.import_module(sys.argv.pop(1)), sys.argv.pop(1))()"
                command = [python, "-c", bootstrap, module, entry_function, "--host", "127.0.0.1", "--port", str(port)]
                if "connector-database:read" in manifest.get("permissions", []):
                    command += ["--db-path", str(self.db_path.resolve())]
                log = (folder / "backend.log").open("ab")
                try:
                    process = subprocess.Popen(command, cwd=folder, env=env, stdout=log, stderr=log,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                finally:
                    log.close()
                self._running[plugin_id] = {"process": process, "port": port, "registered": False}
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise PluginError("Plugin backend exited during startup; check backend.log")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                            if response.status == 200 and json.load(response).get("status") == "ok":
                                break
                    except (OSError, ValueError):
                        pass
                    if time.monotonic() >= deadline:
                        raise PluginError("Plugin backend did not become healthy within 15 seconds; check backend.log")
                    time.sleep(0.1)
                tool = manifest.get("tool", {})
                handler = lambda args: self.invoke(plugin_id, args)
                agent.register_tool(name=plugin_id, description=tool.get("description", f"Run the {plugin_id} plugin"),
                                    parameters=tool.get("parameters", {"type": "object", "properties": {}}),
                                    handler=handler, kind="plugin")
                self._running[plugin_id]["registered"] = True
                self._running[plugin_id]["handler"] = handler
                with closing(self._connect()) as conn, conn:
                    conn.execute("UPDATE plugins SET enabled = 1 WHERE id = ?", (plugin_id,))
                self._errors.pop(plugin_id, None)
            except Exception as exc:
                self._stop(plugin_id)
                self._errors[plugin_id] = str(exc)
                if isinstance(exc, PluginError):
                    raise
                raise PluginError(f"Could not start plugin: {exc}") from exc
            return self.get(plugin_id)

    def _stop(self, plugin_id):
        running = self._running.pop(plugin_id, None)
        if not running:
            return
        if running["registered"]:
            self.agent_provider().unregister_plugin_tool(plugin_id, running["handler"])
        process = running["process"]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def disable(self, plugin_id):
        with self._lock:
            self.get(plugin_id)
            self._stop(plugin_id)
            with closing(self._connect()) as conn, conn:
                conn.execute("UPDATE plugins SET enabled = 0 WHERE id = ?", (plugin_id,))
            self._errors.pop(plugin_id, None)
            return self.get(plugin_id)

    def uninstall(self, plugin_id):
        with self._lock:
            self.disable(plugin_id)
            target = self._folder(plugin_id)
            if target.exists():
                shutil.rmtree(target)
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))

    def backend_url(self, plugin_id):
        with self._lock:
            record = self.get(plugin_id)
            if record["status"] != "running":
                raise PluginConflict("Enable the plugin before using it")
            return f"http://127.0.0.1:{self._running[plugin_id]['port']}"

    def invoke(self, plugin_id, arguments):
        manifest = self.get(plugin_id)["manifest"]
        request = urllib.request.Request(self.backend_url(plugin_id) + manifest["webhook"]["path"],
                                         data=json.dumps(arguments).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except OSError as exc:
            raise PluginError(f"Plugin request failed: {exc}") from exc

    def asset(self, plugin_id, asset_path):
        record = self.get(plugin_id)
        if record["status"] != "running":
            raise PluginConflict("Enable the plugin before opening its UI")
        relative = archive_path(asset_path)
        if relative.parts[0] != "ui":
            raise PluginNotFound("Only UI assets are served")
        root = self._folder(plugin_id)
        target = root.joinpath(*relative.parts).resolve()
        if not target.is_relative_to(root / "ui") or not target.is_file():
            raise PluginNotFound("UI asset not found")
        return target

    def restore(self):
        for record in self.list():
            if record["enabled"]:
                try:
                    self.enable(record["id"])
                except PluginError:
                    logger.exception("Could not restore plugin %s", record["id"])

    def shutdown(self):
        with self._lock:
            for plugin_id in list(self._running):
                self._stop(plugin_id)
