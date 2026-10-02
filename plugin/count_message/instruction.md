# count_message

Counts the text messages exchanged between users and The Connector. This ZIP
contains a standalone npm microfrontend and an installable Python backend.
Python 3.10 or later is required; the backend has no runtime dependencies.
Node 18 or later is only required for npm installation or the npm preview server.

## Run the extracted plugin

For managed installation, open **Plugins** in the Connector sidebar, install
`plugin.zip`, then select **Enable** and **Open UI**. The Connector creates a
dedicated Python environment, supplies its actual database path, chooses a free
backend port, hosts this UI, and registers `count_message` as an agent tool.
Enabled plugins restart with the Connector. **Disable** stops the backend and
removes the managed tool; **Uninstall** removes the package and environment while
keeping chat history. No npm/pip command or `--register` is needed in this mode.

The commands below are for running the package independently.

Start The Connector at least once to initialize its database. Extract
`plugin.zip` into a dedicated folder, then run from that folder:

```cmd
run.bat
```

```sh
sh run.sh
```

Open **http://127.0.0.1:8403**. These scripts serve both the JavaScript UI and
Python API in one process. Press Ctrl+C to stop. No npm install or pip install
is needed for this mode. Windows uses `py -3`; set `COUNT_MESSAGE_PYTHON` to a
Python executable if needed. Unix uses `python3` with the same override.

The plugin reads the Connector SQLite database in read-only mode. It chooses
`DB_PATH`, then `DATA_DIR/connector.db`, then the system temp directory's
`the_connector/connector.db`. It does **not** automatically load the Connector's
`.env`. If the Connector customizes its database, pass the same absolute path:

```cmd
run.bat --db-path "C:/path/to/connector.db" --port 8403
```

```sh
sh run.sh --db-path /path/to/connector.db --port 8403
```

An unavailable database returns HTTP 503, not a misleading zero count. The
plugin needs filesystem read access to the live database and its SQLite WAL
files; run it on the Connector machine or provide an accessible SQLite backup.

## What counts

- One saved `user` or `assistant` row with nonblank text is one message.
- The total is `user_messages + assistant_messages`, not the number of turns.
- Active and closed chats are included. All matching sessions are counted,
  including those beyond the Connector sidebar's 50-session list.
- Web user sessions (`user_session=1`) are included by default. Enable
  `include_system_sessions` to include API-created system sessions too.
- System/tool role rows, blank text, tokens, tool steps, and memory summaries
  do not count. A message containing Markdown or JSON text counts once.
- Counts represent retained history. Clearing history reduces the count.
  Stateless `/v1/chat/completions` requests do not save paired user messages,
  so their separate `completions_response` records are excluded.
- A session filter uses the same system-session policy: a system session
  returns zero unless inclusion is enabled. A nonexistent ID returns 404.

## API and Python module

`GET /api/count_message` counts all retained web chat messages.
`GET /api/count_message?session_id=YOUR_ID&include_system_sessions=true` filters
the count. `POST /api/count_message` accepts the same fields as JSON; `{}`
counts all web chats. Invalid/unknown fields return 400. `GET /health` checks
database readiness.

Example response:

```json
{
  "plugin": "count_message",
  "session_id": null,
  "include_system_sessions": false,
  "sessions": 2,
  "user_messages": 3,
  "assistant_messages": 3,
  "total_messages": 6,
  "counted_at": "2026-10-01T12:00:00+00:00"
}
```

Install the Python module into an environment chosen by the Connector:

```sh
python -m pip install ./backend
count-message --db-path /absolute/path/to/connector.db
```

```python
from count_message import count_messages

result = count_messages(db_path="/absolute/path/to/connector.db")
session_result = count_messages(
    db_path="/absolute/path/to/connector.db", session_id="YOUR_ID"
)
```

The installed Python package serves the API. To serve the extracted UI too,
pass `--ui-dir /absolute/path/to/ui`, or use the ZIP's run scripts.

## Install as a Connector agent tool

With Connector already running, start the plugin with webhook registration:

```cmd
run.bat --register http://127.0.0.1:8301
```

```sh
sh run.sh --register http://127.0.0.1:8301
```

This uses the existing `POST /api/agent/register-tool` API to register the
`count_message` tool with a POST webhook at port 8403. It then appears in
`GET /api/agent/tools`. Example direct invocation:

```json
{"tool":"count_message","arguments":{}}
```

Send that JSON to Connector's `POST /api/agent/step`. Registration is in memory;
restart the plugin with `--register` after restarting Connector. The plugin
must keep running for calls to succeed. This manual registration mode is an
alternative to the Connector's managed ZIP installer. `plugin.json` supplies
the managed installer's runtime entries, permission, and agent tool schema.

## Install/import the npm microfrontend

From the Connector's `frontend` directory, install from extracted source:

```sh
npm install ../plugin/count_message/ui
```

For a ZIP extracted elsewhere, replace that path with its absolute `ui` path.
The package can also be distributed as an npm tarball with `npm pack` in `ui`.
No public npm publication is required or performed.

Import into a host UI:

```javascript
import { mount } from '@the-connector/count-message';

const plugin = mount(document.getElementById('count-message-slot'), {
  apiBaseUrl: 'http://127.0.0.1:8403',
  sessionId: null,                // all web chats; or a saved session ID
  includeSystemSessions: false,
  pollIntervalMs: 10000,           // 0 disables polling
});
plugin.update({ sessionId: 'YOUR_ID' });
// Call when removing the plugin from the host:
plugin.unmount();
```

React adapter example (add this component at the desired place in Connector):

```jsx
import { useEffect, useRef } from 'react';
import { mount } from '@the-connector/count-message';

export function MessageCountPlugin({ sessionId = null }) {
  const slot = useRef(null);
  const instance = useRef(null);
  useEffect(() => {
    instance.current = mount(slot.current, {
      apiBaseUrl: 'http://127.0.0.1:8403',
    });
    return () => { instance.current.unmount(); instance.current = null; };
  }, []);
  useEffect(() => { instance.current?.update({ sessionId }); }, [sessionId]);
  return <div ref={slot} />;
}
```

The widget uses Shadow DOM to contain its styles, polls every 10 seconds, and
cancels timers/requests on unmount. Errors replace stale counts with dashes.
The exported `fetchCounts(options)` is available for custom host interfaces.
Installing npm alone does not insert a widget into the host's layout.

For a separate npm UI server, run the Python API in one terminal, then:

```sh
cd ui
npm start
```

Open **http://127.0.0.1:5133**; the Node preview proxies the API to port 8403.
Set `COUNT_MESSAGE_UI_PORT` and `COUNT_MESSAGE_BACKEND_URL` to override those
defaults. `COUNT_MESSAGE_BACKEND_URL` must be an HTTP URL. For embedded UIs on
another origin, pass `--allow-origin http://your-host:your-port` to the backend.
The default CORS allowlist covers localhost/127.0.0.1 on ports 5130 and 5133.
For remote Connector installations, configure `--host`, `--endpoint` (the
webhook URL reachable from Connector), and the browser's `apiBaseUrl` to match.
The default launch binds to loopback; the count API has no authentication.

## Verify and rebuild

From this plugin directory:

```sh
python -m unittest discover -s backend/tests -v
cd ui
npm test
cd ..
python build_plugin.py
```

The build writes `plugin.zip` in this directory. To select a destination:

```sh
python build_plugin.py --output /path/to/plugin.zip
```

The archive has `instruction.md`, `ui/`, `backend/`, and the run scripts at its
root, without an extra enclosing directory. Dependencies, caches, build
artifacts, and local databases are not part of the distributable.
