import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';
import { getSettings, updateSettings } from '../src/services/api.js';

let server;
let DataStorageFields;
let SettingsModal;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ DataStorageFields } = await server.ssrLoadModule('/src/components/DataStorageSetting.jsx'));
  ({ SettingsModal } = await server.ssrLoadModule('/src/components/SettingsModal.jsx'));
});

after(async () => { await server?.close(); });

const settings = {
  data_dir: 'C:\\Temp\\the_connector',
  configured_data_dir: 'D:\\Connector Data',
  default_data_dir: 'C:\\Temp\\the_connector',
  settings_path: 'C:\\Users\\User\\AppData\\Roaming\\the_connector\\settings.json',
  restart_required: true,
};

function render(fields = {}) {
  return renderToStaticMarkup(React.createElement(DataStorageFields, {
    settings, folder: settings.configured_data_dir,
    onChange() {}, onSave() {}, onReset() {}, ...fields,
  }));
}

test('storage settings distinguish the saved folder from the running and default folders', () => {
  const html = render();
  assert.match(html, /<label for="data-storage-folder"/);
  assert.match(html, /value="D:\\Connector Data"/);
  assert.match(html, /Running folder: <code>C:\\Temp\\the_connector<\/code>/);
  assert.match(html, /Default folder: <code>C:\\Temp\\the_connector<\/code>/);
  assert.match(html, /Settings file: <code>C:\\Users\\User\\AppData\\Roaming\\the_connector\\settings.json<\/code>/);
  assert.match(html, /computer running The Connector/);
  assert.match(html, /%TEMP% are supported/);
  assert.match(html, /Existing files are not moved/);
  assert.match(html, /copy it to the new folder while the app is stopped/);
  assert.match(html, /Restart The Connector to apply it/);
  assert.match(html, /Environment overrides take precedence/);
});

test('storage controls allow clearing a custom folder to restore the default', () => {
  const unchanged = render();
  assert.match(unchanged, /disabled="">Save folder<\/button>/);
  const reset = render({ folder: '' });
  assert.match(reset, /value=""/);
  assert.match(reset, /placeholder="C:\\Temp\\the_connector"/);
  assert.match(reset, /Leave blank to use the default folder/);
  assert.doesNotMatch(reset, /disabled="">Save folder<\/button>/);
  assert.match(reset, /disabled="">Use default<\/button>/);
});

test('an invalid saved preference can be repaired even when the replacement is the default', () => {
  const html = render({
    settings: { ...settings, configured_data_dir: '', configuration_error: 'Data storage folder must be a string.' },
    folder: '', error: 'Data storage folder must be a string.',
  });
  assert.match(html, /role="alert">Data storage folder must be a string/);
  assert.doesNotMatch(html, /disabled="">Save folder<\/button>/);
});

test('storage settings preserve the draft on a save failure and expose loading failures for retry', () => {
  const failedSave = render({ folder: 'relative-folder', error: 'Enter an absolute folder' });
  assert.match(failedSave, /value="relative-folder"/);
  assert.match(failedSave, /role="alert">Enter an absolute folder/);
  assert.doesNotMatch(failedSave, />Retry<\/button>/);
  const failedLoad = render({ settings: null, folder: '', error: 'Server unavailable', onRetry() {} });
  assert.match(failedLoad, /disabled="" aria-describedby="data-storage-help"/);
  assert.match(failedLoad, /role="alert">Server unavailable/);
  assert.match(failedLoad, />Retry<\/button>/);
  const saving = render({ folder: 'D:\\New Data', saving: true });
  assert.match(saving, /disabled="">Saving…<\/button>/);
});

test('Settings includes folder settings alongside existing voice and session preferences', () => {
  const html = renderToStaticMarkup(React.createElement(SettingsModal, {
    isOpen: true, onClose() {}, pastMemory: true, showSystemSessions: false, agentMode: true,
    onTogglePastMemory() {}, onToggleShowSystemSessions() {}, onToggleAgentMode() {},
    speechVoices: [], onSpeechActorChange() {},
  }));
  assert.match(html, /Data storage folder/);
  assert.match(html, /Voice actor/);
  assert.match(html, /Past Memory/);
  assert.match(html, /Show System Sessions/);
  assert.match(html, /Agentic Mode/);
});

test('storage APIs use the current origin and persist custom and default folder choices', async (t) => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, ...options });
    return { ok: true, json: async () => settings };
  });
  assert.deepEqual(await getSettings(), settings);
  assert.deepEqual(await updateSettings({ data_dir: '%USERPROFILE%\\Connector Data' }), settings);
  await updateSettings({ data_dir: '' });
  assert.deepEqual(calls.map(({ url, method }) => ({ url, method })), [
    { url: '/api/settings', method: 'GET' },
    { url: '/api/settings', method: 'PATCH' },
    { url: '/api/settings', method: 'PATCH' },
  ]);
  assert.deepEqual(JSON.parse(calls[1].body), { data_dir: '%USERPROFILE%\\Connector Data' });
  assert.deepEqual(calls[1].headers, { 'Content-Type': 'application/json' });
  assert.deepEqual(JSON.parse(calls[2].body), { data_dir: '' });
});

test('storage API validation failures expose the server message', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: false, status: 422, statusText: 'Unprocessable Entity',
    json: async () => ({ detail: 'Data storage folder must be an absolute path.' }),
  }));
  await assert.rejects(updateSettings({ data_dir: 'relative-folder' }), /Data storage folder must be an absolute path/);
});
