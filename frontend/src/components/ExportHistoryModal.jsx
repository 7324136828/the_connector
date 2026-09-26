import React, { useEffect, useRef, useState } from 'react';
import { getAllHistoryExportUrl, getExportZipUrl } from '../services/api';

export function ExportHistoryModal({ isOpen, onClose, activeSessionId, onClearAll }) {
  const [confirmation, setConfirmation] = useState('');
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState('');
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const previousFocus = document.activeElement;
    setConfirmation('');
    setClearing(false);
    setError('');
    window.setTimeout(() => dialogRef.current?.focus(), 0);
    return () => previousFocus?.focus();
  }, [isOpen]);

  if (!isOpen) return null;

  const close = () => { if (!clearing) onClose(); };
  const handleKeyboard = (event) => {
    if (event.key === 'Escape') close();
  };
  const clearHistory = async () => {
    if (confirmation !== 'DELETE' || clearing) return;
    setClearing(true);
    setError('');
    try {
      await onClearAll();
      onClose();
    } catch (err) {
      setError(err.message);
      setClearing(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={close}>
      <div
        className="modal-dialog export-dialog" ref={dialogRef} tabIndex={-1}
        role="dialog" aria-modal="true" aria-labelledby="export-history-title"
        onKeyDown={handleKeyboard} onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <h2 className="modal-title" id="export-history-title">Export options</h2>
          <button type="button" className="config-close" onClick={close} disabled={clearing} aria-label="Close export options">×</button>
        </div>
        <div className="modal-body export-options">
          <section className="export-option">
            <div>
              <h3>Export Current History</h3>
              <p>Download this chat, its metadata, tool traces, and transcripts as a ZIP archive.</p>
            </div>
            {activeSessionId ? (
              <a className="config-button primary" href={getExportZipUrl(activeSessionId)} download>Export current</a>
            ) : (
              <button type="button" className="config-button primary" disabled>No active chat</button>
            )}
          </section>

          <section className="export-option">
            <div>
              <h3>Export All History</h3>
              <p>Download all active, closed, user, and system chat sessions in one ZIP archive.</p>
            </div>
            <a className="config-button" href={getAllHistoryExportUrl()} download>Export all</a>
          </section>

          <section className="export-option export-danger-zone">
            <div>
              <h3>Clear All History</h3>
              <p>Permanently delete every chat and its derived conversation memory. This cannot be undone.</p>
            </div>
            <label className="export-confirm-label">
              Type <strong>DELETE</strong> to confirm
              <input
                type="text" value={confirmation} disabled={clearing}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="off" spellCheck="false"
              />
            </label>
            {error && <div className="config-error" role="alert">{error}</div>}
            <button
              type="button" className="config-button danger" disabled={confirmation !== 'DELETE' || clearing}
              onClick={clearHistory}
            >
              {clearing ? 'Clearing history…' : 'Clear all history'}
            </button>
          </section>
        </div>
      </div>
    </div>
  );
}
