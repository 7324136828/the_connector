import React, { useEffect, useState } from 'react';
import {
  createCodingSkillFromConversation, deleteSkill, getAgentTools, getSkills,
  getSkillsExportUrl, importSkillsFile, updateSkill,
} from '../services/api';

const fieldStyle = {
  width: '100%', background: '#121217', border: '1px solid var(--border-subtle)',
  borderRadius: '6px', color: 'var(--text-main)', padding: '8px',
};

export function AgentToolsModal({ isOpen, onClose, activeSessionId }) {
  const [tools, setTools] = useState([]);
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(false);
  const [testTool, setTestTool] = useState('');
  const [testArgs, setTestArgs] = useState('{}');
  const [testResult, setTestResult] = useState(null);
  const [testRunning, setTestRunning] = useState(false);
  const [skillName, setSkillName] = useState('');
  const [skillType, setSkillType] = useState('');
  const [conversation, setConversation] = useState('');
  const [skillError, setSkillError] = useState('');
  const [skillNotice, setSkillNotice] = useState('');
  const [skillCreating, setSkillCreating] = useState(false);
  const [skillImporting, setSkillImporting] = useState(false);
  const [importConflict, setImportConflict] = useState('error');
  const [editDraft, setEditDraft] = useState(null);
  const [skillSaving, setSkillSaving] = useState(false);

  const loadTools = async () => {
    try {
      setLoading(true);
      const [toolData, skillData] = await Promise.all([getAgentTools(), getSkills()]);
      setTools(toolData);
      setSkills(skillData);
      if (toolData.length > 0 && !testTool) {
        setTestTool(toolData[0].name);
        setTestArgs(toolData[0].name === 'run_python_script' ? '{"code": "print(2**16)"}' : '{}');
      }
    } catch (err) {
      setSkillError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) loadTools();
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCreateSkill = async () => {
    if (!activeSessionId || !conversation.trim()) return;
    try {
      setSkillCreating(true);
      setSkillError('');
      setSkillNotice('');
      await createCodingSkillFromConversation({
        sessionId: activeSessionId, conversation: conversation.trim(), name: skillName,
        type: skillType || undefined,
      });
      setConversation('');
      setSkillName('');
      setSkillType('');
      await loadTools();
    } catch (err) {
      setSkillError(err.message);
    } finally {
      setSkillCreating(false);
    }
  };

  const handleDeleteSkill = async (id) => {
    if (!window.confirm('Delete this persisted skill?')) return;
    try {
      setSkillError('');
      await deleteSkill(id);
      if (editDraft?.id === id) setEditDraft(null);
      await loadTools();
    } catch (err) {
      setSkillError(err.message);
    }
  };

  const beginEdit = (skill) => {
    setSkillError('');
    setEditDraft({
      id: skill.id,
      name: skill.name,
      description: skill.description,
      parameters: JSON.stringify(skill.parameters, null, 2),
      type: skill.type,
      python_code: skill.python_code,
      source_conversation: skill.source_conversation,
    });
  };

  const handleSaveSkill = async () => {
    if (!editDraft) return;
    try {
      setSkillSaving(true);
      setSkillError('');
      const parameters = JSON.parse(editDraft.parameters);
      await updateSkill(editDraft.id, {
        name: editDraft.name,
        description: editDraft.description,
        parameters,
        type: editDraft.type,
        python_code: editDraft.python_code,
        source_conversation: editDraft.source_conversation,
      });
      setEditDraft(null);
      await loadTools();
    } catch (err) {
      setSkillError(err instanceof SyntaxError ? 'Parameters must be valid JSON.' : err.message);
    } finally {
      setSkillSaving(false);
    }
  };

  const handleImportSkills = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    try {
      setSkillImporting(true);
      setSkillError('');
      setSkillNotice('');
      const result = await importSkillsFile(file, importConflict);
      setSkillNotice(`Imported ${result.created}; replaced ${result.replaced}; skipped ${result.skipped}.`);
      setEditDraft(null);
      await loadTools();
    } catch (err) {
      setSkillError(err.message);
    } finally {
      setSkillImporting(false);
    }
  };

  const handleRunTest = async () => {
    try {
      setTestRunning(true);
      setTestResult(null);
      const response = await fetch('/api/agent/step', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tool: testTool, arguments: JSON.parse(testArgs),
          ...(testTool === 'fetch_memory' && activeSessionId ? { session_id: activeSessionId } : {}),
        }),
      });
      setTestResult(await response.json());
    } catch (err) {
      setTestResult({ success: false, error: err.message });
    } finally {
      setTestRunning(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">Agent Skills</div>
          <button type="button" onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--text-faint)', cursor: 'pointer', fontSize: '1.2rem' }}>×</button>
        </div>

        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
            The Connector can create reusable Python, Windows CMD, and C++ coding skills. Python uses the selected managed environment; CMD can call that environment through PATH; C++ uses a discovered system compiler. Created skills are stored in SQLite and reloaded after restart.
          </p>
          {skillError && <div role="alert" style={{ color: '#f87171', fontSize: '0.78rem' }}>{skillError}</div>}
          {skillNotice && <div role="status" style={{ color: '#34d399', fontSize: '0.78rem' }}>{skillNotice}</div>}

          <section style={{ backgroundColor: '#16161d', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '12px' }}>
            <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>Backup and restore skills</div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
              <a href={getSkillsExportUrl()} download="skills.json" style={{ color: '#60a5fa', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '6px 10px', textDecoration: 'none', fontSize: '0.78rem' }}>Export skills.json</a>
              <select value={importConflict} onChange={(event) => setImportConflict(event.target.value)} style={{ background: '#121217', border: '1px solid var(--border-subtle)', color: 'var(--text-main)', borderRadius: '6px', padding: '6px 10px' }}>
                <option value="error">Stop on duplicate</option>
                <option value="skip">Skip duplicates</option>
                <option value="replace">Replace duplicates</option>
              </select>
              <label style={{ color: '#60a5fa', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '6px 10px', cursor: skillImporting ? 'default' : 'pointer', fontSize: '0.78rem' }}>
                {skillImporting ? 'Importing...' : 'Import skills.json'}
                <input type="file" accept="application/json,.json" disabled={skillImporting} onChange={handleImportSkills} style={{ display: 'none' }} />
              </label>
            </div>
          </section>

          <section style={{ backgroundColor: '#16161d', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '12px' }}>
            <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>Create skill from conversation</div>
            <input value={skillName} onChange={(event) => setSkillName(event.target.value)} placeholder="optional_skill_name" style={{ ...fieldStyle, marginBottom: '8px' }} />
            <select value={skillType} onChange={(event) => setSkillType(event.target.value)} style={{ ...fieldStyle, marginBottom: '8px' }}>
              <option value="">Auto-detect language</option>
              <option value="python">Python</option>
              <option value="cmd">Windows CMD</option>
              <option value="c++">C++</option>
            </select>
            <textarea value={conversation} onChange={(event) => setConversation(event.target.value)} placeholder="Paste the conversation that describes the reusable behavior…" style={{ ...fieldStyle, minHeight: '110px' }} />
            <button type="button" onClick={handleCreateSkill} disabled={!activeSessionId || !conversation.trim() || skillCreating} style={{ marginTop: '8px', background: 'var(--accent-blue)', color: 'white', border: 'none', borderRadius: '6px', padding: '7px 12px', cursor: 'pointer' }}>
              {skillCreating ? 'Creating…' : 'Create and persist skill'}
            </button>
            {!activeSessionId && <div style={{ color: 'var(--text-faint)', fontSize: '0.75rem', marginTop: '6px' }}>Open a chat session, or send its first message, so the configured model can generate the code.</div>}
          </section>

          {skills.length > 0 && (
            <section>
              <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>Persisted skills</div>
              {skills.map((skill) => (
                <div key={skill.id} style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', padding: '8px 0', borderBottom: '1px solid var(--border-subtle)' }}>
                  <div>
                    <div style={{ color: '#34d399', fontFamily: 'var(--font-mono)', fontSize: '0.82rem' }}>{skill.name} <small style={{ color: 'var(--text-faint)' }}>({skill.type})</small></div>
                    <div style={{ color: 'var(--text-muted)', fontSize: '0.76rem' }}>{skill.description}</div>
                  </div>
                  <div style={{ display: 'flex', gap: '6px' }}>
                    <button type="button" onClick={() => beginEdit(skill)} style={{ background: 'none', color: '#60a5fa', border: '1px solid var(--border-subtle)', borderRadius: '4px', height: '28px', cursor: 'pointer' }}>Edit</button>
                    <button type="button" onClick={() => handleDeleteSkill(skill.id)} style={{ background: 'none', color: '#f87171', border: '1px solid var(--border-subtle)', borderRadius: '4px', height: '28px', cursor: 'pointer' }}>Delete</button>
                  </div>
                </div>
              ))}
            </section>
          )}

          {editDraft && (
            <section style={{ backgroundColor: '#16161d', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '12px' }}>
              <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>Edit persisted skill</div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Name
                <input value={editDraft.name} onChange={(event) => setEditDraft({ ...editDraft, name: event.target.value })} style={{ ...fieldStyle, marginTop: '4px' }} />
              </label>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Description
                <textarea value={editDraft.description} onChange={(event) => setEditDraft({ ...editDraft, description: event.target.value })} style={{ ...fieldStyle, minHeight: '55px', marginTop: '4px' }} />
              </label>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Execution type
                <select value={editDraft.type} onChange={(event) => setEditDraft({ ...editDraft, type: event.target.value })} style={{ ...fieldStyle, marginTop: '4px' }}>
                  <option value="python">Python</option>
                  <option value="cmd">Windows CMD</option>
                  <option value="c++">C++</option>
                </select>
              </label>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Parameters (JSON Schema)
                <textarea value={editDraft.parameters} onChange={(event) => setEditDraft({ ...editDraft, parameters: event.target.value })} style={{ ...fieldStyle, minHeight: '100px', marginTop: '4px', fontFamily: 'var(--font-mono)' }} />
              </label>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Code ({editDraft.type})
                <textarea value={editDraft.python_code} onChange={(event) => setEditDraft({ ...editDraft, python_code: event.target.value })} style={{ ...fieldStyle, minHeight: '180px', marginTop: '4px', fontFamily: 'var(--font-mono)' }} />
              </label>
              <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Source conversation
                <textarea value={editDraft.source_conversation} onChange={(event) => setEditDraft({ ...editDraft, source_conversation: event.target.value })} style={{ ...fieldStyle, minHeight: '80px', marginTop: '4px' }} />
              </label>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button type="button" onClick={handleSaveSkill} disabled={skillSaving} style={{ background: 'var(--accent-blue)', color: 'white', border: 'none', borderRadius: '6px', padding: '7px 12px', cursor: 'pointer' }}>{skillSaving ? 'Saving...' : 'Save changes'}</button>
                <button type="button" onClick={() => setEditDraft(null)} disabled={skillSaving} style={{ background: 'none', color: 'var(--text-main)', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '7px 12px', cursor: 'pointer' }}>Cancel</button>
              </div>
            </section>
          )}

          <section>
            <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '8px' }}>Available tools {loading ? '(loading…)' : ''}</div>
            {tools.map((tool) => (
              <div key={tool.name} style={{ backgroundColor: '#16161d', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '10px 12px', marginBottom: '8px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, color: '#34d399', fontSize: '0.85rem' }}>{tool.name} <small style={{ color: 'var(--text-faint)' }}>({tool.kind})</small></span>
                  <button type="button" onClick={() => { setTestTool(tool.name); setTestArgs(tool.name === 'run_python_script' ? '{"code": "print(\'Hello from Agent\')"}' : '{}'); }} style={{ background: 'none', border: '1px solid var(--border-subtle)', color: '#60a5fa', fontSize: '0.72rem', padding: '2px 8px', borderRadius: '4px', cursor: 'pointer' }}>Test</button>
                </div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: '4px' }}>{tool.description}</div>
              </div>
            ))}
          </section>

          <section style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
              <select value={testTool} onChange={(event) => setTestTool(event.target.value)} style={{ background: '#121217', border: '1px solid var(--border-subtle)', color: 'var(--text-main)', borderRadius: '6px', padding: '6px 10px' }}>
                {tools.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>)}
              </select>
              <button type="button" onClick={handleRunTest} disabled={testRunning || !testTool} style={{ background: 'var(--accent-blue)', color: 'white', border: 'none', borderRadius: '6px', padding: '6px 14px', cursor: 'pointer' }}>{testRunning ? 'Executing…' : 'Run tool'}</button>
            </div>
            <textarea style={{ ...fieldStyle, height: '70px', fontFamily: 'var(--font-mono)' }} value={testArgs} onChange={(event) => setTestArgs(event.target.value)} placeholder="Arguments JSON" />
            {testResult && <pre style={{ background: '#121217', border: '1px solid var(--border-subtle)', padding: '8px', borderRadius: '6px', color: testResult.success ? '#34d399' : '#f87171', maxHeight: '140px', overflowY: 'auto' }}>{JSON.stringify(testResult, null, 2)}</pre>}
          </section>
        </div>

        <div className="modal-footer"><button type="button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
