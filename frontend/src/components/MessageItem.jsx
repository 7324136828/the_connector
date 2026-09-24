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

function safeMediaUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const url = value.trim();
  if (url.startsWith('/') && !url.startsWith('//')) return url;
  try {
    const parsed = new URL(url);
    return ['http:', 'https:'].includes(parsed.protocol) ? url : null;
  } catch {
    return null;
  }
}

const INVALID_JSON_ESCAPE = /\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})/g;

function videoPayload(value) {
  if (value && typeof value === 'object') return JSON.stringify(value);
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  const fenced = /^(`{1,3})video\s*\r?\n([\s\S]*?)\r?\n\1$/.exec(trimmed);
  return fenced ? fenced[2].trim() : trimmed;
}

export function parseVideoBlock(code) {
  try {
    const decoded = JSON.parse(code);
    const records = Array.isArray(decoded) ? decoded : [decoded];
    const videos = records.slice(0, 20).map((record) => {
      if (!record || typeof record !== 'object' || Array.isArray(record)) return null;
      const src = safeMediaUrl(record.src || record.url || record.video_url || record.play_url);
      if (!src) return null;
      return {
        src,
        poster: safeMediaUrl(record.poster || record.thumbnail || record.thumbnail_url),
        title: typeof record.title === 'string' ? record.title.slice(0, 500) : '',
        description: typeof record.description === 'string' ? record.description.slice(0, 4000) : '',
        uploader: typeof (record.uploader || record.upload_user) === 'string'
          ? (record.uploader || record.upload_user).slice(0, 500) : '',
        downloadedAt: typeof (record.downloaded_at || record.downloaded_date) === 'string'
          ? (record.downloaded_at || record.downloaded_date).slice(0, 200) : '',
      };
    }).filter(Boolean);
    return videos.length ? videos : null;
  } catch {
    return null;
  }
}

export function parseVideoMessage(content) {
  if (typeof content !== 'string') return null;
  try {
    const decoded = JSON.parse(content.replace(INVALID_JSON_ESCAPE, ''));
    if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) return null;
    const wrapped = decoded.video_block ?? decoded.final_answer?.video_block;
    const payload = videoPayload(wrapped);
    return payload ? parseVideoBlock(payload) : null;
  } catch {
    return null;
  }
}

function VideoPlayer({ video }) {
  return (
    <figure className="message-video-card">
      {video.title && <figcaption>{video.title}</figcaption>}
      <video controls preload="metadata" poster={video.poster || undefined}>
        <source src={video.src} />
        Your browser cannot play this video. <a href={video.src} target="_blank" rel="noopener noreferrer">Open the video</a>.
      </video>
      {(video.uploader || video.downloadedAt) && (
        <div className="message-video-meta">
          {video.uploader && <span>Uploaded by {video.uploader}</span>}
          {video.downloadedAt && <span>Downloaded {video.downloadedAt}</span>}
        </div>
      )}
      {video.description && <p>{video.description}</p>}
      <a className="message-video-open" href={video.src} target="_blank" rel="noopener noreferrer">Open video</a>
    </figure>
  );
}

function VideoList({ videos }) {
  return (
    <div className="message-video-list">
      {videos.map((video, index) => <VideoPlayer key={`${video.src}-${index}`} video={video} />)}
    </div>
  );
}

function CodeBlock({ children }) {
  const codeElement = React.Children.toArray(children).find(React.isValidElement);
  const code = String(codeElement?.props.children ?? '');
  const language = /language-([^\s]+)/.exec(codeElement?.props.className ?? '')?.[1];
  const videos = language?.toLowerCase() === 'video' ? parseVideoBlock(code) : null;

  if (videos) {
    return <VideoList videos={videos} />;
  }

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
  const [showSteps, setShowSteps] = useState(false);
  const wrappedVideos = isUser ? null : parseVideoMessage(message.content);

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
            <button
              type="button" className="agent-steps-header"
              aria-expanded={showSteps}
              onClick={() => setShowSteps((visible) => !visible)}
            >
              <span>⚡ Reasoning & Tool Trace ({message.agentSteps.length} step{message.agentSteps.length > 1 ? 's' : ''})</span>
              <span>{showSteps ? '▲ Hide' : '▼ Expand'}</span>
            </button>

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
          {wrappedVideos ? <VideoList videos={wrappedVideos} /> : (
            <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents} skipHtml>
              {message.content ?? ''}
            </Markdown>
          )}
        </div>
      </div>
    </div>
  );
}
