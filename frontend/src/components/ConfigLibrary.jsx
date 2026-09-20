import React, { useEffect, useRef, useState } from 'react';
import { ConfigModal } from './ConfigModal';
import { ConfigHistoryList } from './ConfigHistoryList';
import { suggestedModelId } from './configHelpers';
import { createLibraryConfig, deleteLibraryConfig, getConfigHistory, getLibraryConfigs, getLibraryConfigDownloadUrl, updateLibraryConfig } from '../services/api';

export function ConfigLibrary({ isOpen, onClose, onSelect, models, initialConfig }) {
  const [entries, setEntries] = useState([]);
  const [history, setHistory] = useState([]);
  const [tab, setTab] = useState('library');
  const [editing, setEditing] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(null);
  const [filter, setFilter] = useState('');
  const dialogRef = useRef(null);
  const busyRef = useRef(false);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    setDeleting(null);
    setFilter('');
    setTab('library');
    setEditing(initialConfig ? { config: initialConfig } : null);
    Promise.allSettled([getLibraryConfigs(), getConfigHistory()]).then(([libraryResult, historyResult]) => {
      if (cancelled) return;
      const failures = [];
      if (libraryResult.status === 'fulfilled') setEntries(libraryResult.value);
      else failures.push('Library: ' + libraryResult.reason.message);
      if (historyResult.status === 'fulfilled') setHistory(historyResult.value);
      else failures.push('History: ' + historyResult.reason.message);
      setError(failures.join(' '));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [isOpen, initialConfig]);

  useEffect(() => {
    if (!isOpen || editing) return;
    const previousFocus = document.activeElement;
    dialogRef.current?.focus();
    return () => previousFocus?.focus();
  }, [isOpen, editing]);

  const updateEntry = (record) => setEntries((previous) => [record, ...previous.filter((entry) => entry.id !== record.id)]);

  const runAction = async (action) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setLoading(true);
    setError('');
    try { await action(); }
    catch (err) { setError(err.message); }
    finally { busyRef.current = false; setLoading(false); }
  };

  const saveEntry = async (config, metadata) => {
    const { model_id, ...mutableMetadata } = metadata;
    const record = editing.id
      ? await updateLibraryConfig(editing.id, { ...mutableMetadata, config })
      : await createLibraryConfig({ ...metadata, config });
    updateEntry(record);
  };

  const selectEntry = (entry, source) => runAction(async () => {
    await onSelect(entry, source);
    onClose();
  });

  const openHistory = () => runAction(async () => {
    setTab('history');
    setFilter('');
    setHistory(await getConfigHistory());
  });

  const handleKeyboard = (event) => {
    if (event.key === 'Escape' && !busyRef.current) onClose();
    if (event.key !== 'Tab') return;
    const controls = [...dialogRef.current.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), summary, details[open] pre[tabindex]')];
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault(); last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first?.focus();
    }
  };

  if (!isOpen) return null;
  if (editing) return (
    <ConfigModal
      isOpen onClose={() => { setEditing(null); runAction(async () => setHistory(await getConfigHistory())); }} onConfigSaved={saveEntry}
      config={editing.config} models={models} libraryEntry={editing} libraryMode
    />
  );

  const query = filter.toLowerCase();
  const visibleEntries = entries.filter((entry) => [entry.name, entry.model_id, entry.description].some((value) => value?.toLowerCase().includes(query)));

  return (
    <div className="modal-overlay" onClick={() => { if (!loading) onClose(); }}>
      <div className="modal-dialog library-dialog" ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="library-title" onKeyDown={handleKeyboard} onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title" id="library-title">Configuration library</h2>
          <button type="button" className="config-close" aria-label="Close configuration library" onClick={onClose} disabled={loading}>×</button>
        </div>
        <div className="modal-body">
          <p className="config-description">Save and reuse named configurations. Active entries are available for new chats and as models in API clients. Existing chats keep their own saved configuration.</p>
          <nav className="library-tabs" aria-label="Configuration views">
            <button type="button" className={'config-button ' + (tab === 'library' ? 'primary' : '')} aria-pressed={tab === 'library'} disabled={loading} onClick={() => { setTab('library'); setFilter(''); }}>Saved library</button>
            <button type="button" className={'config-button ' + (tab === 'history' ? 'primary' : '')} aria-pressed={tab === 'history'} disabled={loading} onClick={openHistory}>Load history</button>
          </nav>
          {tab === 'history' && <p className="config-description">Previously loaded configurations are kept as snapshots, including uploaded files and saved sessions. Reuse a snapshot even if its library entry has changed or been removed. Identical configurations share one history entry.</p>}
          <div className="library-toolbar">
            <input type="search" aria-label={tab === 'history' ? 'Find configuration history' : 'Find saved configurations'} placeholder="Find configurations…" value={filter} onChange={(event) => setFilter(event.target.value)} />
            {tab === 'history' && <button className="config-button" disabled={loading} onClick={() => runAction(async () => setHistory(await getConfigHistory()))}>Refresh history</button>}
            <button className="config-button primary" disabled={loading} onClick={() => setEditing({ config: null })}>Add configuration</button>
          </div>
          {error && <div className="config-error" role="alert">{error}</div>}
          {loading && <p className="config-description" role="status">Loading configurations…</p>}
          {tab === 'history' ? <ConfigHistoryList records={history} filter={filter} loading={loading} onSelect={(entry) => selectEntry(entry, 'history')} onSave={(entry) => setEditing({ config: entry.config, name: entry.name, model_id: suggestedModelId(entry.name || 'My assistant') })} /> : <>
          {!loading && entries.length === 0 && <p className="library-empty">No saved configurations yet. Add one or upload a config.json to get started.</p>}
          {!loading && entries.length > 0 && visibleEntries.length === 0 && <p className="library-empty">No matching configurations.</p>}
          <div className="library-list">
            {visibleEntries.map((entry) => (
              <article className="library-card" key={entry.id}>
                <div className="library-card-heading">
                  <h3>{entry.name}</h3>
                  <span className={'badge-tag ' + (entry.active ? 'badge-openai' : 'badge-mock')}>{entry.active ? 'Active' : 'Inactive'}</span>
                </div>
                <p className="library-model-id">Model ID: <code>{entry.model_id}</code></p>
                {entry.description && <p className="config-description">{entry.description}</p>}
                {entry.context_length && <p className="config-description">Context limit: {entry.context_length.toLocaleString()} tokens</p>}
                <div className="config-actions">
                  <button className="config-button primary" disabled={loading || !entry.active} onClick={() => selectEntry(entry, 'library')}>Use in new chat</button>
                  <button className="config-button" disabled={loading} onClick={() => { setDeleting(null); setEditing(entry); }}>Edit</button>
                  <button className="config-button" disabled={loading} onClick={() => runAction(async () => updateEntry(await updateLibraryConfig(entry.id, { active: !entry.active })))}>{entry.active ? 'Deactivate' : 'Activate'}</button>
                  <a className="config-button" href={getLibraryConfigDownloadUrl(entry.id)} download="config.json">Download</a>
                  <button className="config-button danger" disabled={loading} onClick={() => setDeleting(entry.id)}>Delete</button>
                </div>
                {deleting === entry.id && (
                  <div className="library-delete-confirm" role="group" aria-label={'Confirm deletion of ' + entry.name}>
                    <p>Delete this saved configuration? Existing chat snapshots are kept.</p>
                    <div className="config-actions">
                      <button className="config-button danger" disabled={loading} onClick={() => runAction(async () => {
                        await deleteLibraryConfig(entry.id);
                        setEntries((previous) => previous.filter((record) => record.id !== entry.id));
                        setDeleting(null);
                      })}>Delete configuration</button>
                      <button className="config-button" disabled={loading} onClick={() => setDeleting(null)}>Cancel</button>
                    </div>
                  </div>
                )}
              </article>
            ))}
          </div>
          </>}
        </div>
      </div>
    </div>
  );
}
