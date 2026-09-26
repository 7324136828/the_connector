import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let ModelSelector;
let PythonEnvironmentModal;
let ExportHistoryModal;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ ModelSelector } = await server.ssrLoadModule('/src/components/ModelSelector.jsx'));
  ({ PythonEnvironmentModal } = await server.ssrLoadModule('/src/components/PythonEnvironmentModal.jsx'));
  ({ ExportHistoryModal } = await server.ssrLoadModule('/src/components/ExportHistoryModal.jsx'));
});

after(async () => { await server?.close(); });

test('top bar exposes the Select venv control and active environment', () => {
  const html = renderToStaticMarkup(React.createElement(ModelSelector, {
    config: null,
    activeSessionId: null,
    pastMemory: true,
    agentMode: true,
    disabled: false,
    sidebarOpen: false,
    onToggleSidebar() {},
    onOpenConfig() {},
    onOpenPythonEnvironments() {},
    pythonEnvironmentName: 'Data Science',
  }));
  assert.match(html, /Select venv/);
  assert.match(html, /Data Science/);
  assert.match(html, /Python execution/);
  assert.match(html, />Export</);
});

test('environment window includes managed creation controls', () => {
  const html = renderToStaticMarkup(React.createElement(PythonEnvironmentModal, {
    isOpen: true,
    onClose() {},
    onSelectionChange() {},
  }));
  assert.match(html, /Python execution environments/);
  assert.match(html, /Create a virtual environment/);
  assert.match(html, /Create environment/);
  assert.match(html, /system temporary directory/);
});

test('export modal offers current, all, and confirmed clear-history actions', () => {
  const html = renderToStaticMarkup(React.createElement(ExportHistoryModal, {
    isOpen: true,
    onClose() {},
    onClearAll() {},
    activeSessionId: 'session/id',
  }));
  assert.match(html, /Export Current History/);
  assert.match(html, /href="\/api\/sessions\/session%2Fid\/export-zip"/);
  assert.match(html, /Export All History/);
  assert.match(html, /href="\/api\/history\/export-zip"/);
  assert.match(html, /Clear All History/);
  assert.match(html, /Type <strong>DELETE<\/strong> to confirm/);
  assert.match(html, /Clear all history<\/button>/);
});
