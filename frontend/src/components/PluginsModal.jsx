import React, { useEffect, useRef, useState } from 'react';
import { disablePlugin, enablePlugin, getPlugins, installPlugin, uninstallPlugin } from '../services/api';

export function pluginFrameUrl(id, sessionId = null) {
  const base = `/api/plugins/${encodeURIComponent(id)}/frame`;
  return sessionId ? `${base}?${new URLSearchParams({ session_id: sessionId })}` : base;
}

export function PluginsModal({ isOpen, onClose, activeSessionId }) {
  const [plugins, setPlugins] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(null);
  const [notice, setNotice] = useState('');
  const fileInput = useRef(null);
  const currentPlugin = plugins.find((plugin) => plugin.id === selected && plugin.status === 'running');

  useEffect(() => {
    if (!isOpen) return undefined;
    let cancelled = false;
    const refresh = () => getPlugins().then((records) => { if (!cancelled) setPlugins(records); })
      .catch((err) => { if (!cancelled) setError(err.message); });
    refresh();
    const timer = setInterval(refresh, 5000);
    const closeOnEscape = (event) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => { cancelled = true; clearInterval(timer); window.removeEventListener('keydown', closeOnEscape); };
  }, [isOpen, onClose]);

  async function act(action, success) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await action();
      setPlugins(await getPlugins());
      setNotice(success);
      return result;
    } catch (err) { setError(err.message); return null; }
    finally { setBusy(false); }
  }

  async function upload(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { setError('Plugin ZIP exceeds 10 MiB'); return; }
    await act(() => installPlugin(file), 'Plugin installed. Enable it to start its backend and agent tool.');
  }

  if (!isOpen) return null;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-dialog plugins-dialog" role="dialog" aria-modal="true" aria-labelledby="plugins-title" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h2 id="plugins-title" className="modal-title">Plugins</h2>
          <button type="button" className="config-close" aria-label="Close plugins" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          <p className="plugins-description">Install a plugin ZIP, enable its backend and agent tool, then open its interface.</p>
          <p className="plugins-description">Plugins run code on this computer. Install packages from sources you trust.</p>
          <input ref={fileInput} type="file" accept=".zip,application/zip" onChange={upload} hidden aria-label="Choose plugin ZIP" />
          <button type="button" className="config-button primary" disabled={busy} onClick={() => fileInput.current.click()}>{busy ? 'Working…' : 'Install plugin ZIP'}</button>
          {error && <p className="venv-error" role="alert">{error}</p>}
          {notice && <p role="status">{notice}</p>}
          <div className="plugins-list">
            {plugins.map((plugin) => (
              <article className="plugin-card" key={plugin.id}>
                <div><strong>{plugin.id}</strong> <span>v{plugin.version} · {plugin.status}</span>
                  {plugin.error && <p role="alert">{plugin.error}</p>}</div>
                <div className="config-actions">
                  <button className="config-button" disabled={busy} onClick={() => act(() => plugin.enabled ? disablePlugin(plugin.id) : enablePlugin(plugin.id), plugin.enabled ? 'Plugin disabled.' : 'Plugin enabled.')}>{plugin.enabled ? 'Disable' : 'Enable'}</button>
                  {plugin.status === 'error' && <button className="config-button" disabled={busy} onClick={() => act(() => enablePlugin(plugin.id), 'Plugin restarted.')}>Retry</button>}
                  <button className="config-button" disabled={busy || plugin.status !== 'running'} onClick={() => setSelected(plugin.id)}>Open UI</button>
                  <button className="config-button danger" disabled={busy} onClick={() => act(() => uninstallPlugin(plugin.id), 'Plugin uninstalled; chat history was kept.')}>Uninstall</button>
                </div>
              </article>
            ))}
            {!plugins.length && <p>No plugins installed. Choose plugin.zip to get started.</p>}
          </div>
          {currentPlugin && <section className="plugin-interface" aria-label={`${selected} interface`}>
            <div className="plugin-interface-header"><strong>{selected}</strong><button className="config-button" onClick={() => setSelected(null)}>Close interface</button></div>
            <iframe title={`${selected} plugin`} src={pluginFrameUrl(selected, activeSessionId)} sandbox="allow-scripts" />
          </section>}
        </div>
        <div className="modal-footer"><button className="config-button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
