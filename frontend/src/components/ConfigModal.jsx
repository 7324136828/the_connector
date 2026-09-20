import React, { useState, useEffect, useRef } from 'react';
import { getExampleConfig, getExampleConfigUrl, getModelCapabilities, loadConfigFile, recordConfigLoad, validateConfig } from '../services/api';
import { getConfigRoutes, setRouteEffort, suggestedModelId } from './configHelpers';

export function ConfigModal({ isOpen, onClose, onConfigSaved, config, models = [], activeSessionId, libraryEntry, libraryMode = false, onSaveCopy }) {
  const [configText, setConfigText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [fileName, setFileName] = useState('');
  const [customCapabilities, setCustomCapabilities] = useState({});
  const [metadata, setMetadata] = useState({ name: '', model_id: '', description: '', active: true, context_length: '' });
  const modelIdEdited = useRef(false);
  const dialogRef = useRef(null);
  const busyRef = useRef(false);

  let parsedConfig = null;
  try { parsedConfig = JSON.parse(configText); } catch { /* Editing may temporarily produce incomplete JSON. */ }
  const routes = getConfigRoutes(parsedConfig);
  const routeKeys = JSON.stringify([...new Set(routes.map((route) => route.key))]);

  useEffect(() => {
    if (!isOpen) return;
    setConfigText(config ? JSON.stringify(config, null, 2) : '');
    setError('');
    setFileName('');
    setMetadata({ name: libraryEntry?.name || '', model_id: libraryEntry?.model_id || '', description: libraryEntry?.description || '', active: libraryEntry?.active ?? true, context_length: libraryEntry?.context_length ?? '' });
    modelIdEdited.current = Boolean(libraryEntry?.id);
  }, [isOpen, config, libraryEntry]);

  useEffect(() => {
    if (!isOpen) return;
    const previousFocus = document.activeElement;
    dialogRef.current?.focus();
    return () => previousFocus?.focus();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      const missing = JSON.parse(routeKeys).filter((key) => {
        const [provider, model] = JSON.parse(key);
        return !models.some((item) => item.provider === provider && item.id === model) && !customCapabilities[key];
      });
      const results = await Promise.allSettled(missing.map(async (key) => {
        const [provider, model] = JSON.parse(key);
        return [key, await getModelCapabilities(provider, model)];
      }));
      if (cancelled) return;
      const updates = {};
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') updates[result.value[0]] = result.value[1];
        else updates[missing[index]] = { error: true };
      });
      if (Object.keys(updates).length) setCustomCapabilities((previous) => ({ ...previous, ...updates }));
    }, 300);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [isOpen, routeKeys, models]);

  const runAction = async (action) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setLoading(true);
    setError('');
    try { await action(); }
    catch (err) { setError(err.message); }
    finally { busyRef.current = false; setLoading(false); }
  };

  const readConfig = () => {
    if (!configText.trim()) throw new Error('Choose a config.json file, use the example, or enter your configuration.');
    let value;
    try { value = JSON.parse(configText); }
    catch (err) { throw new Error('Invalid JSON: ' + err.message); }
    if (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.sequences)) {
      throw new Error('Configuration must be a JSON object containing a sequences list.');
    }
    return value;
  };

  const handleUpload = (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    runAction(async () => {
      const record = await loadConfigFile(file);
      setConfigText(JSON.stringify(record.config, null, 2));
      setFileName(file.name);
    });
  };

  const handleDownload = () => runAction(async () => {
    const normalized = await validateConfig(readConfig());
    setConfigText(JSON.stringify(normalized, null, 2));
    const url = URL.createObjectURL(new Blob([JSON.stringify(normalized, null, 2) + '\n'], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'config.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  const handleSave = () => runAction(async () => {
    const normalized = await validateConfig(readConfig());
    if (libraryMode) {
      if (!metadata.name.trim()) throw new Error('Enter a name for this configuration.');
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(metadata.model_id)) throw new Error('Model ID must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens (100 characters maximum).');
      const contextLength = metadata.context_length === '' ? null : Number(metadata.context_length);
      if (contextLength !== null && (!Number.isSafeInteger(contextLength) || contextLength < 1)) throw new Error('Context limit must be a positive whole number.');
      await onConfigSaved(normalized, { ...metadata, name: metadata.name.trim(), context_length: contextLength });
    } else {
      await onConfigSaved(normalized, { name: fileName || 'Session configuration', source: 'editor' });
    }
    onClose();
  });

  const handleKeyboard = (event) => {
    if (event.key === 'Escape' && !busyRef.current) onClose();
    if (event.key !== 'Tab') return;
    const controls = [...dialogRef.current.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled])')];
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={() => { if (!loading) onClose(); }}>
      <div ref={dialogRef} className="modal-dialog config-dialog" role="dialog" aria-modal="true" aria-labelledby="config-title" tabIndex={-1} onKeyDown={handleKeyboard} onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title" id="config-title">{libraryMode ? (libraryEntry?.id ? 'Edit saved configuration' : 'Save reusable configuration') : 'Session configuration'}</h2>
          <button className="config-close" type="button" onClick={onClose} disabled={loading} aria-label="Close configuration">×</button>
        </div>
        <div className="modal-body">
          <p className="config-description">
            {libraryMode ? 'Save this configuration for reuse. Active configurations are also available as models through the compatible API.' : activeSessionId ? 'Edit the config.json saved with this session. Changes apply to its next message.' : 'Choose a config.json for your new session. It defines the models, routing, effort, and memory settings.'}
          </p>
          <p className="config-description">Valid uploads are saved automatically in Configuration library → Load history for later reuse.</p>
          {libraryMode && (
            <div className="library-fields">
              <label>Name<input value={metadata.name} maxLength={200} disabled={loading} onChange={(event) => setMetadata((previous) => ({ ...previous, name: event.target.value, ...(!modelIdEdited.current ? { model_id: suggestedModelId(event.target.value) } : {}) }))} placeholder="My assistant" /></label>
              <label>API model ID<input value={metadata.model_id} maxLength={100} disabled={loading || Boolean(libraryEntry?.id)} onChange={(event) => { modelIdEdited.current = true; setMetadata((previous) => ({ ...previous, model_id: event.target.value })); }} placeholder="my-assistant" /></label>
              <p className="config-description library-field-help">A unique, permanent ID used to select this configuration in API clients.</p>
              <label>Description<input value={metadata.description} maxLength={2000} disabled={loading} onChange={(event) => setMetadata((previous) => ({ ...previous, description: event.target.value }))} placeholder="Optional description" /></label>
              <label>Context limit (tokens, optional)<input type="number" min="1" step="1" value={metadata.context_length} disabled={loading} onChange={(event) => setMetadata((previous) => ({ ...previous, context_length: event.target.value }))} placeholder="Derive from configured models" /></label>
              <p className="config-description library-field-help">Actual context limit of the routing configuration; use the lowest limit across its models.</p>
              <label className="library-active-field"><input type="checkbox" checked={metadata.active} disabled={loading} onChange={(event) => setMetadata((previous) => ({ ...previous, active: event.target.checked }))} /> Active for new chats and API clients</label>
            </div>
          )}
          <div className="config-actions">
            <label className="config-file-label">
              Choose config.json
              <input type="file" accept=".json,application/json" aria-label="Choose config.json file" onChange={handleUpload} disabled={loading} />
            </label>
            <button className="config-button" disabled={loading} onClick={() => runAction(async () => {
              const example = await getExampleConfig();
              const record = await recordConfigLoad({ config: example, name: 'Example configuration', source: 'editor' });
              setConfigText(JSON.stringify(record.config, null, 2));
              setFileName('Example configuration');
            })}>Use example</button>
            <a className="config-button" href={getExampleConfigUrl()} download="config.json">Download example</a>
          </div>
          {fileName && <p className="config-description config-filename">{fileName}</p>}
          {error && <div className="config-error" role="alert">{error}</div>}
          <label className="config-editor-label" htmlFor="config-json-editor">config.json</label>
          <textarea
            id="config-json-editor" className="json-textarea"
            value={configText} onChange={(event) => { setConfigText(event.target.value); setError(''); }}
            disabled={loading} spellCheck={false}
            placeholder='Choose a file, use the example, or paste your config.json here.'
          />
          {routes.length > 0 && (
            <section className="config-efforts" aria-label="Model effort settings">
              <h3>Model effort</h3>
              <p className="config-description">Choose an effort level for each supported model. Changes are written to the JSON above.</p>
              {routes.map((route) => {
                const capability = models.find((item) => item.provider === route.provider && item.id === route.model) || customCapabilities[route.key];
                const levels = capability?.effort_levels || [];
                const defaultEffort = capability?.default_effort || levels[0];
                const invalidEffort = route.effort && !levels.includes(route.effort);
                const selectId = 'effort-' + route.path.join('-');
                return (
                  <div className="config-effort-row" key={selectId}>
                    <label htmlFor={levels.length ? selectId : undefined}>
                      <span className="config-route-number">{route.label}</span>
                      <span>{route.provider} / {route.model}</span>
                    </label>
                    {levels.length ? (
                      <select
                        id={selectId} value={route.effort || ''} disabled={loading}
                        onChange={(event) => setConfigText(JSON.stringify(setRouteEffort(readConfig(), route.path, event.target.value), null, 2))}
                      >
                        <option value="">Default ({defaultEffort})</option>
                        {invalidEffort && <option value={route.effort}>{route.effort} (unsupported)</option>}
                        {levels.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
                      </select>
                    ) : (
                      <span className="config-description">{!capability ? 'Checking support…' : capability.error ? 'Could not check support' : 'No effort setting'}</span>
                    )}
                  </div>
                );
              })}
            </section>
          )}
          {parsedConfig && (
            <p className="config-memory-note">
              {parsedConfig.past_memory === false
                ? 'Past memory is off.'
                : parsedConfig.memory_scope === 'session'
                  ? 'Past memory includes saved messages from this session.'
                  : 'Past memory includes saved messages from this session and other conversations.'}
            </p>
          )}
        </div>
        <div className="modal-footer">
          {!libraryMode && onSaveCopy && <button className="config-button" disabled={loading || !configText.trim()} onClick={() => runAction(async () => { onSaveCopy(await validateConfig(readConfig())); })}>Save to library</button>}
          <button className="config-button" onClick={handleDownload} disabled={loading || !configText.trim()}>Download config.json</button>
          <button className="config-button primary" onClick={handleSave} disabled={loading || !configText.trim()}>
            {loading ? 'Working…' : libraryMode ? 'Save configuration' : activeSessionId ? 'Save session config' : 'Use configuration'}
          </button>
        </div>
      </div>
    </div>
  );
}
