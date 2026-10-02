/** Standalone ES module: no React dependency, bundler, or global CSS. */
export async function fetchCounts({ apiBaseUrl = '', sessionId = null, includeSystemSessions = false, signal } = {}) {
  const query = new URLSearchParams({ include_system_sessions: String(includeSystemSessions) });
  if (sessionId !== null) query.set('session_id', sessionId);
  const response = await fetch(`${apiBaseUrl.replace(/\/$/, '')}/api/count_message?${query}`, { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `Count request failed (${response.status})`);
  return data;
}

/** Return update(options), refresh(), and unmount() lifecycle methods. */
export function mount(container, options = {}) {
  const root = document.createElement('div');
  container.append(root);
  const shadow = root.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { display:block; color:#e8edf5; font:14px system-ui,sans-serif; }
      section { padding:20px; background:#171e2c; border:1px solid #35425a; border-radius:12px; }
      h2 { margin:0 0 12px; font-size:18px; } p { line-height:1.5; }
      label { display:block; margin:12px 0; } input[type=text] { box-sizing:border-box; width:100%; padding:8px; }
      button { padding:8px 16px; cursor:pointer; } button:disabled { cursor:wait; }
      dl { display:flex; flex-wrap:wrap; gap:24px; } dt { color:#b4c0d4; } dd { margin:4px 0; font-size:28px; }
      [role=alert] { color:#ffb4b4; } small { color:#b4c0d4; }
    </style>
    <section aria-label="Message count plugin">
      <h2>Message count</h2>
      <p>Saved text messages exchanged with the Connector, including closed chats.</p>
      <label>Session ID (leave empty for all chats)<input type="text" data-session></label>
      <label><input type="checkbox" data-system> Include system sessions</label>
      <button type="button">Refresh counts</button>
      <p role="alert" hidden></p>
      <dl aria-live="polite"><div><dt>User</dt><dd data-user>—</dd></div>
        <div><dt>Connector</dt><dd data-assistant>—</dd></div><div><dt>Total</dt><dd data-total>—</dd></div></dl>
      <small data-status>Loading…</small>
    </section>`;
  let config = { apiBaseUrl: '', sessionId: null, includeSystemSessions: false, pollIntervalMs: 10000, ...options };
  let controller;
  let timer;
  let removed = false;
  const sessionInput = shadow.querySelector('[data-session]');
  const systemInput = shadow.querySelector('[data-system]');
  const button = shadow.querySelector('button');
  const error = shadow.querySelector('[role=alert]');
  const status = shadow.querySelector('[data-status]');
  function syncControls() {
    sessionInput.value = config.sessionId ?? '';
    systemInput.checked = config.includeSystemSessions;
  }
  async function refresh() {
    if (removed) return;
    clearTimeout(timer);
    controller?.abort();
    const current = new AbortController();
    controller = current;
    button.disabled = true;
    error.hidden = true;
    status.textContent = 'Loading…';
    try {
      const result = await fetchCounts({ ...config, signal: current.signal });
      if (removed || controller !== current) return;
      for (const [selector, key] of [['user', 'user_messages'], ['assistant', 'assistant_messages'], ['total', 'total_messages']]) {
        shadow.querySelector(`[data-${selector}]`).textContent = String(result[key]);
      }
      status.textContent = `${result.sessions} saved session(s) · Updated ${new Date(result.counted_at).toLocaleTimeString()}`;
    } catch (err) {
      if (removed || controller !== current) return;
      if (err.name !== 'AbortError') {
        error.textContent = err.message;
        error.hidden = false;
        for (const selector of ['user', 'assistant', 'total']) shadow.querySelector(`[data-${selector}]`).textContent = '—';
        status.textContent = 'Counts unavailable';
      }
    } finally {
      if (!removed && controller === current) {
        button.disabled = false;
        if (config.pollIntervalMs > 0) timer = setTimeout(refresh, config.pollIntervalMs);
      }
    }
  }
  button.addEventListener('click', refresh);
  sessionInput.addEventListener('change', () => { config.sessionId = sessionInput.value.trim() || null; refresh(); });
  systemInput.addEventListener('change', () => { config.includeSystemSessions = systemInput.checked; refresh(); });
  syncControls();
  refresh();
  return {
    refresh,
    update(nextOptions) { config = { ...config, ...nextOptions }; syncControls(); return refresh(); },
    unmount() { removed = true; clearTimeout(timer); controller?.abort(); root.remove(); },
  };
}
