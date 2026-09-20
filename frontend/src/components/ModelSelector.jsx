import React from 'react';
import { getExportZipUrl } from '../services/api';

export function ModelSelector({ config, onOpenConfig, activeSessionId, pastMemory, agentMode, disabled }) {
  const steps = config?.sequences || [];
  const summary = steps.length === 1 && steps[0].model
    ? [steps[0].provider + ' / ' + steps[0].model, steps[0].effort].filter(Boolean).join(' · ')
    : steps.length + ' routing step' + (steps.length === 1 ? '' : 's');

  return (
    <header className="chat-header">
      <div className="header-left">
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
