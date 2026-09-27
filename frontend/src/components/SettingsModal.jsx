import React, { useEffect, useRef } from 'react';
import { VoiceActorSetting } from './VoiceActorSetting';

export function SettingsModal({
  isOpen, onClose, disabled, hasConfig,
  pastMemory, onTogglePastMemory, showSystemSessions, onToggleShowSystemSessions,
  agentMode, onToggleAgentMode, speechActor, onSpeechActorChange,
  speechVoices, speechDefaultActor, speechVoicesLoading, speechVoicesError, onRetrySpeechVoices,
}) {
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const previousFocus = document.activeElement;
    dialogRef.current?.focus();
    return () => previousFocus?.focus();
  }, [isOpen]);

  if (!isOpen) return null;

  const handleKeyboard = (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
    }
    if (event.key !== 'Tab') return;
    const controls = dialogRef.current.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled])');
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

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-dialog settings-dialog" id="settings-dialog" ref={dialogRef} tabIndex={-1}
        role="dialog" aria-modal="true" aria-labelledby="settings-title"
        onKeyDown={handleKeyboard} onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <h2 className="modal-title" id="settings-title">Settings</h2>
          <button type="button" className="config-close" aria-label="Close settings" onClick={onClose}>×</button>
        </div>
        <div className="modal-body settings-controls">
          <VoiceActorSetting
            actor={speechActor} onChange={onSpeechActorChange}
            voices={speechVoices} defaultActor={speechDefaultActor}
            loading={speechVoicesLoading} error={speechVoicesError}
            onRetry={onRetrySpeechVoices} disabled={disabled}
          />
          <div className="control-row">
            <label htmlFor="past-memory" className="control-label" title="Include saved conversation memory according to this session's config.json.">Past Memory</label>
            <label className="switch">
              <input id="past-memory" type="checkbox" checked={pastMemory} onChange={(event) => onTogglePastMemory(event.target.checked)} disabled={disabled || !hasConfig} />
              <span className="slider" />
            </label>
          </div>
          <div className="control-row">
            <label htmlFor="show-system-sessions" className="control-label" title="Include API-created system sessions in the session list.">Show System Sessions</label>
            <label className="switch">
              <input id="show-system-sessions" type="checkbox" checked={showSystemSessions} onChange={(event) => onToggleShowSystemSessions(event.target.checked)} disabled={disabled} />
              <span className="slider" />
            </label>
          </div>
          <div className="control-row">
            <label htmlFor="agent-mode" className="control-label" title="Let the configured models use tools to complete a task.">Agentic Mode</label>
            <label className="switch">
              <input id="agent-mode" type="checkbox" checked={agentMode} onChange={(event) => onToggleAgentMode(event.target.checked)} disabled={disabled} />
              <span className="slider" />
            </label>
          </div>
        </div>
        <div className="modal-footer">
          <button type="button" className="config-button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
