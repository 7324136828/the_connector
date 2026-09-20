import React, { useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

function CopyButton({ text, label = 'Copy', className = '' }) {
  const [status, setStatus] = useState(label);
  const timeoutRef = useRef(null);

  useEffect(() => () => clearTimeout(timeoutRef.current), []);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setStatus('Copied!');
    } catch {
      setStatus('Copy failed');
    }
    clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setStatus(label), 2000);
  };

  return (
    <button type="button" className={`message-copy ${className}`} onClick={handleCopy} aria-live="polite">
      {status}
    </button>
  );
}

function CodeBlock({ children }) {
  const codeElement = React.Children.toArray(children).find(React.isValidElement);
  const code = String(codeElement?.props.children ?? '');
  const language = /language-([^\s]+)/.exec(codeElement?.props.className ?? '')?.[1];

  return (
    <div className="markdown-code-block">
      <div className="markdown-code-header">
        <span>{language || 'code'}</span>
        <CopyButton text={code} label="Copy code" />
      </div>
      <pre>{children}</pre>
    </div>
  );
}

const markdownComponents = {
  pre: CodeBlock,
  a: ({ node, children, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>
  ),
  table: ({ node, children, ...props }) => (
    <div className="markdown-table-wrapper"><table {...props}>{children}</table></div>
  ),
};

export function MessageItem({ message }) {
  const isUser = message.role === 'user';
  const [showSteps, setShowSteps] = useState(true);

  return (
    <div className="message-row">
      <div className={`avatar ${isUser ? 'avatar-user' : message.isAgent ? 'avatar-agent' : 'avatar-assistant'}`}>
        {isUser ? 'U' : message.isAgent ? 'AG' : 'AI'}
      </div>

      <div className="message-body">
        {/* Metadata Banner */}
        <div className="message-meta-row">
          <span style={{ fontWeight: 600, color: 'var(--text-main)' }}>
            {isUser ? 'You' : message.isAgent ? 'Agent Orchestrator' : 'Assistant'}
          </span>

          {!isUser && message.provider && (
            <span>
              via <strong>{message.provider}</strong> ({message.model})
            </span>
          )}

          {!isUser && message.latency_ms && (
            <span>• {Math.round(message.latency_ms)}ms</span>
          )}

          {!isUser && message.tokens?.total_tokens && (
            <span>• {message.tokens.total_tokens} tokens</span>
          )}

          {!isUser && (
            <CopyButton text={message.content ?? ''} className="message-copy-all" />
          )}
        </div>

        {/* Agent Step Traces if present */}
        {message.agentSteps && message.agentSteps.length > 0 && (
          <div className="agent-steps-wrapper">
            <div className="agent-steps-header" onClick={() => setShowSteps(!showSteps)}>
              <span>⚡ Reasoning & Tool Trace ({message.agentSteps.length} step{message.agentSteps.length > 1 ? 's' : ''})</span>
              <span>{showSteps ? '▲ Hide' : '▼ Expand'}</span>
            </div>

            {showSteps && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '6px' }}>
                {message.agentSteps.map((s, idx) => (
                  <div key={idx} className="step-card">
                    <div className="step-header">
                      <span>Step {s.step || idx + 1}:</span>
                      {s.thought && <span style={{ fontStyle: 'italic' }}>{s.thought}</span>}
                    </div>

                    {s.tool && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span>Action:</span>
                        <span className="tool-name-badge">{s.tool}</span>
                        {s.arguments && (
                          <code style={{ fontSize: '0.75rem' }}>{JSON.stringify(s.arguments)}</code>
                        )}
                      </div>
                    )}

                    {s.observation && (
                      <div>
                        <div style={{ fontSize: '0.7rem', color: 'var(--text-faint)' }}>Observation:</div>
                        <pre className="step-obs">{s.observation}</pre>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Message Content */}
        <div className="message-text">
          <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents} skipHtml>
            {message.content ?? ''}
          </Markdown>
        </div>
      </div>
    </div>
  );
}
