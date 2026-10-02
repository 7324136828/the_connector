# Connector plugin contract

This reference describes the repository's current managed Python installer.
Recheck `backend/app/services/plugin_manager.py` and `backend/app/api/plugins.py`
when using it after host changes.

## Package layout

Create the following under `plugin/<plugin_id>/`; ZIP these contents directly,
without an enclosing plugin directory:

```text
instruction.md
plugin.json
ui/
  package.json
  src/index.js          # or a compiled browser-ready entry
  index.html            # standalone preview
backend/
  pyproject.toml
  <plugin_id>/
    __init__.py
    server.py
run.py
run.bat
run.sh
build_plugin.py
LICENSE
```

The installer requires `instruction.md`, `plugin.json`, `ui/package.json`,
`backend/pyproject.toml`, `run.bat`, and `run.sh`, plus the UI entry named in the
manifest. The other files above support the standalone distribution and rebuild.
Include any additional modules, assets, and runtime resources actually needed.

## Manifest example

Replace this example's identity, description, schema, and behavior for the
requested plugin:

```json
{
  "id": "example_plugin",
  "version": "1.0.0",
  "ui": {
    "package": "@the-connector/example-plugin",
    "path": "ui",
    "entry": "ui/src/index.js",
    "lifecycle": ["mount", "update", "unmount"]
  },
  "backend": {
    "package": "connector-example-plugin",
    "language": "python",
    "path": "backend",
    "entry": "example_plugin.server:main",
    "port": 8404
  },
  "webhook": { "method": "POST", "path": "/api/example_plugin" },
  "tool": {
    "description": "Describe this plugin's capability for the agent.",
    "parameters": {
      "type": "object",
      "properties": {},
      "additionalProperties": false
    }
  },
  "permissions": [],
  "launchers": { "windows": "run.bat", "unix": "run.sh" }
}
```

- ID: `^[a-z][a-z0-9_]{0,63}$`; avoid collisions with other plugins and tools.
- Version: a nonempty string of at most 64 characters; keep package versions
  consistent. Use semantic versions for releases.
- Backend: `language` must be `python`, `path` must be `backend`, and `entry`
  must be an importable dotted module plus a callable function separated by `:`.
- UI: `entry` must be an existing `.js` module under `ui/`.
- Webhook: `method` must be `POST`; path must match `/[A-Za-z0-9_/-]+`.
- Tool: give a useful description and an object JSON Schema matching the
  backend's accepted JSON input. The plugin ID is the registered tool name.
- Permissions: only `[]` or `["connector-database:read"]` are currently accepted.
  Request the latter only for Connector database reads. It supplies a path,
  not enforced filesystem isolation; implement SQLite read-only access yourself.

## Backend runtime

Connector creates a dedicated virtual environment, sets `PYTHONPATH` to the
extracted `backend/`, and imports and calls the manifest entry with CLI arguments:

```text
--host 127.0.0.1 --port <assigned_free_port>
--db-path <actual_connector_database>   # only for database permission
```

The entry callable must parse those arguments. Do not depend on an editable
installation, host application imports, host working directory, or host `.env`
loading. Place the Python module directly under `backend/`, or provide an entry
module there that resolves any alternative package layout.

Runtime dependencies come from `[project].dependencies` in `pyproject.toml`
and optional `backend/requirements.txt`. The installer does not run the project's
build backend or install `[project.scripts]`; define installable metadata for
standalone use and a working source entry for managed use.

Serve `GET /health` with HTTP 200 and JSON `{"status":"ok"}` when ready.
Startup must complete within the host's 15-second readiness window. Accept JSON
POSTs at `webhook.path` and return JSON. Agent calls use a 10-second HTTP timeout;
the browser proxy also uses a 10-second timeout. Design longer work as bounded
calls or job operations through the declared endpoint if needed.

Connector proxies only `/health` and the exact declared webhook path using GET
or POST. Extra routes, streaming endpoints, and WebSockets are not exposed by
the current proxy. Standalone launchers may serve `ui/` as well as the API and
must forward CLI arguments and stop their child processes on exit.

## UI runtime

Use an npm package with `"type": "module"` and an exported entry matching its
implementation. Export a synchronous `mount(container, options)` returning an
object with `unmount()`; `update(nextOptions)` is useful for standalone embedding.
Connector passes:

```javascript
{
  apiBaseUrl: '/api/plugins/<plugin_id>/proxy',
  sessionId: null // or the selected chat ID
}
```

Append the manifest webhook path to `apiBaseUrl` for API requests. The hosted
frame remounts for a selected-session change; it does not currently call
`update()`. Handle `sessionId` only when it is meaningful to the capability.

The UI runs in a sandboxed iframe with an opaque origin. Use passed options
instead of reaching into the host DOM or relying on host local storage. Render
untrusted values with text nodes or `textContent`. Cancel pending work and
release resources in `unmount()`.

The installer neither runs npm nor compiles JSX/TypeScript or resolves bare npm
imports. Plain browser ES modules work directly. If using a framework, build
before packaging and include the entry and all imported chunks/assets under
`ui/`, with browser-resolvable paths. Keep frontend source and npm metadata for
independent development.

## ZIP packaging and validation

Adapt `plugin/count_message/build_plugin.py`, including its relative paths and
regular-file permissions. Its current extension filter omits CSS, images, and
other asset types, and excludes `dist/`; change those filters if the new UI or
backend needs them. Include compiled UI output when applicable.

Exclude virtual environments, `node_modules`, caches, local databases, secrets,
logs, and the ZIP itself. Limits are 10 MiB compressed, 50 MiB uncompressed,
and 1,000 entries. Paths must be relative, use forward slashes, and have no
hidden components, traversal, Windows-reserved names, or unsupported characters.
Symlinks, encrypted archives, corruption, and case-insensitive duplicate paths
are rejected.

After building, run from the repository root with a Python 3.11+ interpreter
(the host validator imports `tomllib`), substituting the new plugin's ZIP path:

```sh
python -c "import sys; from pathlib import Path; sys.path.insert(0, 'backend'); from app.services.plugin_manager import validate_zip; archive, manifest = validate_zip(Path(sys.argv[1]).read_bytes()); archive.close(); print('Valid plugin:', manifest['id'])" plugin/example_plugin/plugin.zip
```

This validates the archive without installing or executing the plugin. Runtime
and UI smoke tests are still needed for a new implementation. For managed use,
the user uploads the ZIP in Connector's **Plugins** window, enables it, and opens
the UI. Installed IDs cannot be overwritten; do not uninstall an existing plugin
as a side effect of creating its replacement.
