import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let ConfigHistoryList;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ ConfigHistoryList } = await server.ssrLoadModule('/src/components/ConfigHistoryList.jsx'));
});

after(async () => { await server?.close(); });

const records = [
  { id: 'old', name: 'Old session', source: 'session', first_loaded_at: '2026-01-01T10:00:00Z', last_loaded_at: '2026-01-01T10:00:00Z', load_count: 1, config: { sequences: [{ provider: 'mock', model: 'mock-assistant' }] } },
  { id: 'new', name: 'research.json', source: 'upload', first_loaded_at: '2026-02-01T10:00:00Z', last_loaded_at: '2026-03-01T10:00:00Z', load_count: 3, config: { sequences: [{ choices: [{ provider: 'openai', model: 'gpt-5', effort: 'high' }, { provider: 'gemini', model: 'gemini-2.5-flash', effort: 'low' }] }] } },
];

function render(props = {}) {
  return renderToStaticMarkup(React.createElement(ConfigHistoryList, { records, loading: false, onSelect() {}, onSave() {}, ...props }));
}

test('history shows newest snapshots first with source, timestamps, routes, efforts, and reusable actions', () => {
  const html = render();
  assert.ok(html.indexOf('research.json') < html.indexOf('Old session'));
  assert.match(html, /Uploaded file/);
  assert.match(html, /dateTime="2026-03-01T10:00:00Z"/);
  assert.match(html, /3 loads/);
  assert.match(html, /openai \/ gpt-5/);
  assert.match(html, /Effort: high/);
  assert.match(html, /gemini \/ gemini-2.5-flash/);
  assert.match(html, /Use in new chat/);
  assert.match(html, /Save to library/);
  assert.match(html, /href="\/api\/config-history\/new\/download"/);
  assert.match(html, /<details class="history-json"><summary>View config.json<\/summary><pre/);
  assert.doesNotMatch(html, /<textarea|contenteditable|disabled=""/);
});

test('history search finds nested model and effort settings and explains empty results', () => {
  assert.match(render({ filter: ' HIGH ' }), /research.json/);
  assert.doesNotMatch(render({ filter: 'gpt-5' }), /Old session/);
  assert.match(render({ filter: 'missing' }), /No matching configuration history/);
  assert.match(render({ records: [] }), /No configurations loaded yet/);
});

test('JSON inspection escapes uploaded content and history reuse does not depend on library activation', () => {
  const html = render({ records: [{ ...records[0], active: false, config: { system_prompt: '<script>unsafe()</script>', sequences: [{ provider: 'mock', model: 'mock-assistant' }] } }] });
  assert.match(html, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|disabled=""/);
});
