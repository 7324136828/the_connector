import React, { useState, useEffect, useRef } from 'react';
import { Sidebar } from './components/Sidebar';
import { ModelSelector } from './components/ModelSelector';
import { ChatArea } from './components/ChatArea';
import { ConfigModal } from './components/ConfigModal';
import { ConfigLibrary } from './components/ConfigLibrary';
import { AgentToolsModal } from './components/AgentToolsModal';
import {
  createNewSession, sendMessage, closeSession, getSessions, getSession,
  getModels, runAgent, updateSessionConfig, recordConfigLoad,
} from './services/api';
import './config.css';

export function App() {
  const [sessions, setSessions] = useState([]);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [draftVersion, setDraftVersion] = useState(0);
  const [models, setModels] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [selectedConfigId, setSelectedConfigId] = useState(null);
  const [selectedHistoryId, setSelectedHistoryId] = useState(null);
  const [agentMode, setAgentMode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [error, setError] = useState('');
  const [configModalOpen, setConfigModalOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [libraryDraft, setLibraryDraft] = useState(null);
  const [toolsModalOpen, setToolsModalOpen] = useState(false);
  const busyRef = useRef(false);
  const pastMemory = selectedConfig?.past_memory ?? true;
  const busy = loading || savingConfig;

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([getSessions(), getModels()]).then(([sessionResult, modelResult]) => {
      if (cancelled) return;
      if (sessionResult.status === 'fulfilled') setSessions(sessionResult.value);
      else setError(sessionResult.reason.message);
      if (modelResult.status === 'fulfilled') setModels(modelResult.value);
      else setError((current) => [current, 'Model catalog: ' + modelResult.reason.message].filter(Boolean).join(' '));
    });
    return () => { cancelled = true; };
  }, []);

  const handleNewSession = () => {
    if (busyRef.current || busy) return;
    setActiveSessionId(null);
    setMessages([]);
    setDraftVersion((version) => version + 1);
    setError('');
    if (!selectedConfig) setConfigModalOpen(true);
  };

  const selectSession = async (sessionId) => {
    if (busyRef.current || busy) return;
    busyRef.current = true;
    setLoading(true);
    setError('');
    try {
      const detail = await getSession(sessionId);
      setActiveSessionId(sessionId);
      setMessages(detail.messages || []);
      setDraftVersion((version) => version + 1);
      setSelectedConfig(detail.config || detail.session?.config || null);
      setSelectedConfigId(null);
      setSelectedHistoryId(null);
    } catch (err) {
      setError('Could not load session: ' + err.message);
    } finally {
      busyRef.current = false;
      setLoading(false);
    }
  };

  const handleCloseSession = async (sessionId) => {
    if (busyRef.current || busy) return;
    busyRef.current = true;
    setLoading(true);
    setError('');
    try {
      await closeSession(sessionId);
      setSessions((previous) => previous.filter((session) => session.session_id !== sessionId));
      if (activeSessionId === sessionId) {
        setActiveSessionId(null);
        setMessages([]);
        setDraftVersion((version) => version + 1);
      }
    } catch (err) {
      setError('Could not close session: ' + err.message);
    } finally {
      busyRef.current = false;
      setLoading(false);
    }
  };

  const saveConfig = async (config, historyMetadata = {}) => {
    setSavingConfig(true);
    try {
      if (activeSessionId) {
        const result = await updateSessionConfig(activeSessionId, config);
        setSelectedConfig(result.config || config);
        getSessions().then(setSessions).catch(() => {});
      } else {
        const record = await recordConfigLoad({ config, name: historyMetadata.name || 'Session configuration', source: 'editor' });
        setSelectedConfig(record.config);
      }
      setError('');
      setSelectedConfigId(null);
      setSelectedHistoryId(null);
    } finally {
      setSavingConfig(false);
    }
  };

  const handleTogglePastMemory = async (enabled) => {
    if (!selectedConfig || busyRef.current || busy) return;
    try {
      await saveConfig({ ...selectedConfig, past_memory: enabled });
    } catch (err) {
      setError('Could not update memory setting: ' + err.message);
    }
  };

  const handleSendMessage = async (text) => {
    if (busyRef.current || busy) return false;
    if (!selectedConfig) {
      setConfigModalOpen(true);
      return false;
    }
    busyRef.current = true;
    setLoading(true);
    setError('');
    let sessionId = activeSessionId;
    try {
      if (!sessionId) {
        const newSession = await createNewSession({ config: selectedConfig, configId: selectedConfigId, historyId: selectedHistoryId });
        sessionId = newSession.session_id;
        setSessions((previous) => [newSession, ...previous]);
        setActiveSessionId(sessionId);
        if (selectedConfigId || selectedHistoryId) {
          const detail = await getSession(sessionId);
          setSelectedConfig(detail.config);
          setSelectedConfigId(null);
          setSelectedHistoryId(null);
        }
      }
      setMessages((previous) => [...previous, {
        id: 'temp-' + Date.now(), role: 'user', content: text, created_at: new Date().toISOString(),
      }]);
      if (agentMode) {
        const result = await runAgent({ prompt: text, sessionId });
        setMessages((previous) => [...previous, {
          id: result.message_id || 'agent-' + Date.now(),
          role: 'assistant', content: result.final_answer,
          provider: result.provider, model: result.model,
          latency_ms: result.latency_ms, tokens: { total_tokens: result.total_tokens },
          isAgent: true, agentSteps: result.steps, created_at: new Date().toISOString(),
        }]);
      } else {
        const result = await sendMessage({ sessionId, message: text });
        setMessages((previous) => [...previous, {
          ...result, id: result.message_id, role: 'assistant',
        }]);
      }
      getSessions().then(setSessions).catch(() => {});
      return true;
    } catch (err) {
      setError(err.message);
      // Refresh any prompt saved before a provider failed.
      if (sessionId) {
        try {
          const detail = await getSession(sessionId);
          setMessages(detail.messages || []);
        } catch { /* Keep the visible prompt when the server is unavailable. */ }
      }
      return false;
    } finally {
      busyRef.current = false;
      setLoading(false);
    }
  };

  return (
    <div className="app-container">
      <Sidebar
        sessions={sessions} activeSessionId={activeSessionId}
        onSelectSession={selectSession} onNewSession={handleNewSession}
        onCloseSession={handleCloseSession} pastMemory={pastMemory}
        onTogglePastMemory={handleTogglePastMemory} agentMode={agentMode}
        onToggleAgentMode={setAgentMode} onOpenConfig={() => setConfigModalOpen(true)}
        onOpenTools={() => setToolsModalOpen(true)} disabled={busy}
        hasConfig={Boolean(selectedConfig)}
        onOpenLibrary={() => { setLibraryDraft(null); setLibraryOpen(true); }}
      />
      <div className="chat-frame">
        <ModelSelector
          config={selectedConfig} activeSessionId={activeSessionId}
          pastMemory={pastMemory} agentMode={agentMode}
          onOpenConfig={() => setConfigModalOpen(true)} disabled={busy}
        />
        {error && (
          <div className="app-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => setError('')} aria-label="Dismiss error">×</button>
          </div>
        )}
        <ChatArea
          messages={messages} loading={loading} disabled={busy}
          onSendMessage={handleSendMessage} hasConfig={Boolean(selectedConfig)}
          onOpenConfig={() => setConfigModalOpen(true)} pastMemory={pastMemory}
          agentMode={agentMode} draftVersion={draftVersion}
          onOpenLibrary={() => { setLibraryDraft(null); setLibraryOpen(true); }}
        />
      </div>
      <ConfigModal
        isOpen={configModalOpen} onClose={() => setConfigModalOpen(false)}
        config={selectedConfig} models={models} activeSessionId={activeSessionId}
        onConfigSaved={saveConfig}
        onSaveCopy={(config) => { setLibraryDraft(config); setConfigModalOpen(false); setLibraryOpen(true); }}
      />
      <ConfigLibrary
        isOpen={libraryOpen} onClose={() => setLibraryOpen(false)} models={models} initialConfig={libraryDraft}
        onSelect={async (entry, source = 'library') => {
          const record = await recordConfigLoad({ config: entry.config, name: entry.name, source });
          setSelectedConfig(record.config);
          setSelectedConfigId(source === 'library' ? entry.id : null);
          setSelectedHistoryId(source === 'history' ? record.id : null);
          setActiveSessionId(null);
          setMessages([]);
          setDraftVersion((version) => version + 1);
          setError('');
        }}
      />
      <AgentToolsModal isOpen={toolsModalOpen} onClose={() => setToolsModalOpen(false)} />
    </div>
  );
}

export default App;
