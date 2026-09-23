import React from 'react';
import { getExportZipUrl } from '../services/api';

export function ModelSelector({ config, onOpenConfig, activeSessionId, pastMemory, agentMode, disabled, sidebarOpen, onToggleSidebar }) {
  const steps = config?.sequences || [];
  const summary = steps.length === 1 && steps[0].model
    ? [steps[0].provider + ' / ' + steps[0].model, steps[0].effort].filter(Boolean).join(' · ')
    : steps.length + ' routing step' + (steps.length === 1 ? '' : 's');

  return (
    <header className="chat-header">
      <div className="header-left">
        <button
          className="mobile-sidebar-toggle" type="button" onClick={onToggleSidebar}
          aria-label={sidebarOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={sidebarOpen}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="18" x2="20" y2="18" />
          </svg>
        </button>
        <button className="model-dropdown-trigger" onClick={onOpenConfig} disabled={disabled} title="Choose or edit the session configuration">
          <span className="badge-tag badge-sequence">config.json</span>
          <span className="config-summary">{config ? summary : 'Choose configuration'}</span>
        </button>
        {config && (
          <span className={'badge-tag ' + (pastMemory ? 'badge-openai' : 'badge-mock')} title={pastMemory ? 'Include saved memories and conversation context' : 'Past memory is disabled in this configuration'}>
            Memory {pastMemory ? 'ON' : 'OFF'}
          </span>
        )}
        {agentMode && <span className="badge-tag badge-ollama">Agent Mode</span>}
      </div>
      <div className="header-right">
        {activeSessionId && (
          <a href={getExportZipUrl(activeSessionId)} download className="model-dropdown-trigger export-link" title="Export this session as a ZIP archive">
            Export ZIP
          </a>
        )}
      </div>
    </header>
  );
}
