import React, { useState, useEffect } from 'react';
import { getAgentTools } from '../services/api';

export function AgentToolsModal({ isOpen, onClose }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(false);
  const [testTool, setTestTool] = useState('');
  const [testArgs, setTestArgs] = useState('{}');
  const [testResult, setTestResult] = useState(null);
  const [testRunning, setTestRunning] = useState(false);

  useEffect(() => {
    if (isOpen) {
      loadTools();
    }
  }, [isOpen]);

  const loadTools = async () => {
    try {
      setLoading(true);
      const data = await getAgentTools();
      setTools(data);
      if (data.length > 0 && !testTool) {
        setTestTool(data[0].name);
        if (data[0].name === 'calculator') setTestArgs('{"expression": "125 * 8 - 40"}');
        else if (data[0].name === 'python_interpreter') setTestArgs('{"code": "print(2**16)"}');
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleRunTest = async () => {
    try {
      setTestRunning(true);
      setTestResult(null);
      const parsedArgs = JSON.parse(testArgs);
      const res = await fetch('/api/agent/step', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: testTool, arguments: parsedArgs }),
      });
      const data = await res.json();
      setTestResult(data);
    } catch (err) {
      setTestResult({ success: false, error: err.message });
    } finally {
      setTestRunning(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">⚡ Agentic Tools & Plugin Registry</div>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-faint)', cursor: 'pointer', fontSize: '1.2rem' }}
          >
            ✕
          </button>
        </div>

        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
            These tools are autonomously invoked by The Connector agent or can be called directly by external applications using the <code style={{ color: '#38bdf8' }}>/api/agent/step</code> and <code style={{ color: '#38bdf8' }}>/api/agent/run</code> endpoints.
          </p>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {tools.map((t) => (
              <div
                key={t.name}
                style={{
                  backgroundColor: '#16161d',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: '6px',
                  padding: '10px 12px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, color: '#34d399', fontSize: '0.85rem' }}>
                    {t.name}
                  </span>
                  <button
                    onClick={() => {
                      setTestTool(t.name);
                      if (t.name === 'calculator') setTestArgs('{"expression": "45 * 12"}');
                      else if (t.name === 'python_interpreter') setTestArgs('{"code": "print(\'Hello from Agent\')"}');
                      else if (t.name === 'system_info') setTestArgs('{}');
                      else if (t.name === 'web_search') setTestArgs('{"query": "The Connector architecture"}');
                      else setTestArgs('{}');
                    }}
                    style={{
                      background: 'none',
                      border: '1px solid var(--border-subtle)',
                      color: '#60a5fa',
                      fontSize: '0.72rem',
                      padding: '2px 8px',
                      borderRadius: '4px',
                      cursor: 'pointer',
                    }}
                  >
                    Select for Test
                  </button>
                </div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: '4px' }}>
                  {t.description}
                </div>
              </div>
            ))}
          </div>

          {/* Interactive Test Bench */}
          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
            <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>
              Direct Tool Execution Test Bench:
            </div>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
              <select
                value={testTool}
                onChange={(e) => setTestTool(e.target.value)}
                style={{
                  background: '#121217',
                  border: '1px solid var(--border-subtle)',
                  color: 'var(--text-main)',
                  borderRadius: '6px',
                  padding: '6px 10px',
                  fontSize: '0.82rem',
                }}
              >
                {tools.map((t) => (
                  <option key={t.name} value={t.name}>{t.name}</option>
                ))}
              </select>
              <button
                onClick={handleRunTest}
                disabled={testRunning}
                style={{
                  background: 'var(--accent-blue)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  padding: '6px 14px',
                  fontSize: '0.82rem',
                  fontWeight: 600,
                  cursor: testRunning ? 'not-allowed' : 'pointer',
                }}
              >
                {testRunning ? 'Executing...' : 'Run Tool'}
              </button>
            </div>

            <textarea
              style={{
                width: '100%',
                height: '70px',
                background: '#121217',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
                color: '#e2e8f0',
                fontFamily: 'var(--font-mono)',
                fontSize: '0.8rem',
                padding: '8px',
              }}
              value={testArgs}
              onChange={(e) => setTestArgs(e.target.value)}
              placeholder="Arguments JSON"
            />

            {testResult && (
              <div style={{ marginTop: '8px' }}>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-faint)' }}>Output:</div>
                <pre
                  style={{
                    background: '#121217',
                    border: '1px solid var(--border-subtle)',
                    padding: '8px',
                    borderRadius: '6px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.8rem',
                    color: testResult.success ? '#34d399' : '#f87171',
                    maxHeight: '140px',
                    overflowY: 'auto',
                  }}
                >
                  {JSON.stringify(testResult, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>

        <div className="modal-footer">
          <button
            onClick={onClose}
            style={{
              background: 'var(--bg-card-hover)',
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-main)',
              padding: '6px 14px',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '0.82rem',
            }}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
