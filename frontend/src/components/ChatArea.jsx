import React, { useRef, useEffect, useState } from 'react';
import { MessageItem } from './MessageItem';
import { getExampleConfigUrl } from '../services/api';

export function ChatArea({ messages, loading, disabled, onSendMessage, hasConfig, onOpenConfig, onOpenLibrary, pastMemory, agentMode, draftVersion }) {
  const [inputText, setInputText] = useState('');
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  useEffect(() => {
    setInputText('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  }, [draftVersion]);

  const handleSubmit = async () => {
    const trimmed = inputText.trim();
    if (!trimmed || disabled || !hasConfig) return;
    const sent = await onSendMessage(trimmed);
    if (sent) {
      setInputText('');
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
    }
  };

  return (
    <div className="main-content">
      <div className="messages-container" aria-busy={loading}>
        {messages.length === 0 ? (
          <div className="welcome-screen">
            <div className="welcome-icon" aria-hidden="true">⌁</div>
            <h1 className="welcome-title">The Connector</h1>
            <p className="welcome-subtitle">
              {hasConfig ? 'Your configuration is ready. Start a conversation below.' : 'Choose a config.json to start your conversation.'}
            </p>
            <div className="config-actions">
              <button className="config-button" onClick={onOpenLibrary} disabled={disabled}>Saved configurations</button>
              <button className="config-button primary" onClick={onOpenConfig} disabled={disabled}>
                {hasConfig ? 'Edit configuration' : 'Choose config.json'}
              </button>
              <a className="config-button" href={getExampleConfigUrl()} download="config.json">Download example</a>
            </div>
          </div>
        ) : (
          <div className="messages-inner">
            {messages.map((message, index) => <MessageItem key={message.id || index} message={message} />)}
            {loading && (
              <div className="message-row" role="status">
                <div className="avatar avatar-assistant"><span className="pulsing-dot" /></div>
                <div className="message-body response-status">
                  {agentMode ? 'Agent reasoning and executing tools…' : 'Generating response…'}
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>
      <div className="input-section">
        <div className="input-container">
          <textarea
            ref={textareaRef} className="chat-textarea" aria-label="Message"
            placeholder={!hasConfig ? 'Choose config.json to begin' : agentMode ? 'Ask the agent to solve a task…' : 'Send a prompt… (Shift+Enter for newline)'}
            value={inputText}
            onChange={(event) => {
              setInputText(event.target.value);
              event.target.style.height = 'auto';
              event.target.style.height = Math.min(event.target.scrollHeight, 180) + 'px';
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                handleSubmit();
              }
            }}
            rows={1} disabled={disabled || !hasConfig}
          />
          <div className="input-bottom-bar">
            <div className="input-indicators">
              <span>{hasConfig ? 'Session config.json' : 'No configuration selected'}</span>
              {hasConfig && <><span>·</span><span>{pastMemory ? 'Past memory enabled' : 'Past memory disabled'}</span></>}
            </div>
            <button className="btn-send" onClick={handleSubmit} disabled={!inputText.trim() || disabled || !hasConfig} title="Send message" aria-label="Send message">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden="true">
                <line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
