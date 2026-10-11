import React, { useEffect, useRef, useState } from 'react';
import { getSettings, updateSettings } from '../services/api';

export function DataStorageFields({ settings, folder = '', onChange, onSave, onReset, onRetry, loading, saving, disabled, error, notice }) {
  const unavailable = disabled || loading || saving || !settings;
  const unchanged = !settings?.configuration_error && folder.trim() === (settings?.configured_data_dir || '').trim();

  return (
    <section className="storage-setting" aria-labelledby="storage-setting-title">
      <label htmlFor="data-storage-folder" className="control-label" id="storage-setting-title">Data storage folder</label>
      <input
        id="data-storage-folder" type="text" value={folder} onChange={(event) => onChange(event.target.value)}
        placeholder={settings?.default_data_dir || '%TEMP%\\the_connector'}
        disabled={unavailable} aria-describedby="data-storage-help" autoComplete="off" spellCheck={false}
      />
      <p id="data-storage-help" className="storage-setting-description">
        Enter an absolute folder on the computer running The Connector. Windows environment variables such as %TEMP% are supported.
        Leave blank to use the default folder.
      </p>
      {loading && <p className="storage-setting-description" role="status">Loading folder settings…</p>}
      {settings && (
        <div className="storage-setting-paths">
          <p>Running folder: <code>{settings.data_dir}</code></p>
          <p>Default folder: <code>{settings.default_data_dir}</code></p>
          <p>Settings file: <code>{settings.settings_path}</code></p>
        </div>
      )}
      <p className="storage-setting-description">
        Save and restart The Connector to apply a folder change. Existing files are not moved.
        To keep existing data, copy it to the new folder while the app is stopped. Environment overrides take precedence over this setting.
      </p>
      {settings?.restart_required && <p className="storage-setting-notice" role="status">The saved folder differs from the running folder. Restart The Connector to apply it, unless an environment override is active.</p>}
      {error && <div className="storage-setting-error" role="alert">{error}</div>}
      {notice && <p className="storage-setting-notice" role="status">{notice}</p>}
      <div className="storage-setting-actions">
        <button type="button" className="config-button primary" onClick={onSave} disabled={unavailable || unchanged}>
          {saving ? 'Saving…' : 'Save folder'}
        </button>
        <button type="button" className="config-button" onClick={onReset} disabled={unavailable || !folder}>Use default</button>
        {!settings && error && <button type="button" className="config-button" onClick={onRetry} disabled={disabled || loading}>Retry</button>}
      </div>
    </section>
  );
}

export function DataStorageSetting({ disabled }) {
  const [settings, setSettings] = useState(null);
  const [folder, setFolder] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    getSettings().then((result) => {
      if (cancelled) return;
      setSettings(result);
      setFolder(result.configured_data_dir || '');
      setError(result.configuration_error || '');
    }).catch((err) => {
      if (!cancelled) setError('Could not load folder settings: ' + err.message);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [refresh]);

  const changeFolder = (value) => {
    setFolder(value);
    setError('');
    setNotice('');
  };

  const saveFolder = async () => {
    if (disabled || loading || saving || !settings) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const result = await updateSettings({ data_dir: folder.trim() });
      if (!mounted.current) return;
      setSettings(result);
      setFolder(result.configured_data_dir || '');
      setNotice('Folder setting saved.');
    } catch (err) {
      if (mounted.current) setError('Could not save folder settings: ' + err.message);
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  return (
    <DataStorageFields
      settings={settings} folder={folder} onChange={changeFolder} onSave={saveFolder}
      onReset={() => changeFolder('')} onRetry={() => setRefresh((value) => value + 1)}
      loading={loading} saving={saving} disabled={disabled} error={error} notice={notice}
    />
  );
}
