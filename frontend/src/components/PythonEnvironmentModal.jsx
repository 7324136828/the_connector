import React, { useEffect, useState } from 'react';
import {
  createPythonEnvironment, getPythonEnvironments, selectPythonEnvironment,
} from '../services/api';

export function PythonEnvironmentModal({ isOpen, onClose, onSelectionChange }) {
  const [environments, setEnvironments] = useState([]);
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadEnvironments = async () => {
    const records = await getPythonEnvironments();
    setEnvironments(records);
    return records;
  };

  useEffect(() => {
    if (!isOpen) return;
    setError('');
    setLoading(true);
    loadEnvironments().catch((err) => setError(err.message)).finally(() => setLoading(false));
  }, [isOpen]);

  const runAction = async (action) => {
    try {
      setLoading(true);
      setError('');
      const selected = await action();
      await loadEnvironments();
      onSelectionChange?.(selected);
      return selected;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setLoading(false);
    }
  };

  const createEnvironment = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const created = await runAction(() => createPythonEnvironment({ name: trimmed, select: true }));
    if (created) setName('');
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-dialog venv-dialog" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="venv-title">
        <div className="modal-header">
          <div>
            <h2 className="modal-title" id="venv-title">Python execution environments</h2>
            <p className="venv-subtitle">Choose where agent Python and installed packages run.</p>
          </div>
          <button type="button" className="config-close" aria-label="Close Python environments" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          {error && <div className="venv-error" role="alert">{error}</div>}
          <section className="venv-create">
            <label htmlFor="venv-name">Create a virtual environment</label>
            <div className="venv-create-row">
              <input
                id="venv-name" value={name} maxLength={64}
                onChange={(event) => setName(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') createEnvironment(); }}
                placeholder="Data science"
                disabled={loading}
              />
              <button type="button" className="config-button primary" onClick={createEnvironment} disabled={loading || !name.trim()}>
                {loading ? 'Working…' : 'Create environment'}
              </button>
            </div>
            <p>New environments are created under the system temporary directory and selected automatically.</p>
          </section>

          <section className="venv-list" aria-label="Available Python environments">
            {environments.map((environment) => (
              <article className={'venv-card ' + (environment.selected ? 'selected' : '')} key={environment.id}>
                <div className="venv-card-main">
                  <div className="venv-name-row">
                    <strong>{environment.name}</strong>
                    {environment.is_default && <span className="badge-tag badge-mock">Default</span>}
                    <span className={'venv-status ' + (environment.ready ? 'ready' : '')}>
                      {environment.ready ? 'Ready' : 'Created on first use'}
                    </span>
                  </div>
                  <code title={environment.path}>{environment.path}</code>
                </div>
                <button
                  type="button" className={'config-button ' + (environment.selected ? 'primary' : '')}
                  disabled={loading || environment.selected}
                  onClick={() => runAction(() => selectPythonEnvironment(environment.id))}
                >
                  {environment.selected ? 'Selected' : 'Use environment'}
                </button>
              </article>
            ))}
            {!loading && environments.length === 0 && <p className="venv-empty">No Python environments are available.</p>}
          </section>
        </div>
        <div className="modal-footer">
          <button type="button" className="config-button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
