import React from 'react';

export function Sidebar({
  sessions, activeSessionId, onSelectSession, onNewSession, onCloseSession,
  pastMemory, onTogglePastMemory, agentMode, onToggleAgentMode,
  onOpenConfig, onOpenTools, onOpenLibrary, disabled, hasConfig,
}) {
  return (
    <aside className="sidebar" aria-label="Chat sessions and settings">
      <div className="sidebar-header">
        <div className="brand-row">
          <div className="brand">
            <svg width="22" height="22" viewBox="0 0 100 100" fill="none" aria-hidden="true">
              <circle cx="50" cy="50" r="44" stroke="#10b981" strokeWidth="10" />
              <circle cx="30" cy="50" r="10" fill="#10b981" />
              <circle cx="70" cy="50" r="10" fill="#3b82f6" />
            </svg>
            <span>The Connector</span>
          </div>
        </div>
        <button className="btn-new-chat" onClick={onNewSession} disabled={disabled}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden="true">
            <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          <span>New Chat</span>
        </button>
      </div>
      <div className="sidebar-section-title">Chat Sessions</div>
      <div className="sessions-list">
        {sessions.length === 0 ? (
          <div className="config-description" style={{ padding: '12px 8px' }}>No conversations yet.</div>
        ) : sessions.map((session) => (
          <div key={session.session_id} className={'session-item ' + (session.session_id === activeSessionId ? 'active' : '')}>
            <button className="session-open" onClick={() => onSelectSession(session.session_id)} disabled={disabled} aria-current={session.session_id === activeSessionId ? 'true' : undefined}>
              <span className="session-info">
                <span className="session-title">{session.title || 'Untitled Chat'}</span>
                <span className="session-meta">{session.message_count || 0} messages</span>
              </span>
            </button>
            <div className="session-actions">
              <button className="btn-icon-del" title="Close session" aria-label={'Close ' + (session.title || 'Untitled Chat')} disabled={disabled} onClick={() => onCloseSession(session.session_id)}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          </div>
        ))}
      </div>
      <div className="sidebar-controls">
        <div className="control-row">
          <label htmlFor="past-memory" className="control-label" title="Include saved conversation memory according to this session's config.json.">
            Past Memory
          </label>
          <label className="switch">
            <input id="past-memory" type="checkbox" checked={pastMemory} onChange={(event) => onTogglePastMemory(event.target.checked)} disabled={disabled || !hasConfig} />
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
        <div className="config-actions">
          <button className="config-button" onClick={onOpenLibrary} disabled={disabled}>Config Library</button>
          <button className="config-button" onClick={onOpenConfig} disabled={disabled}>Session Config</button>
          <button className="config-button" onClick={onOpenTools} disabled={disabled}>Agent Tools</button>
        </div>
      </div>
    </aside>
  );
}
