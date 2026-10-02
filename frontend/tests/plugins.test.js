import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let PluginsModal;
let Sidebar;
let pluginFrameUrl;
before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ PluginsModal, pluginFrameUrl } = await server.ssrLoadModule('/src/components/PluginsModal.jsx'));
  ({ Sidebar } = await server.ssrLoadModule('/src/components/Sidebar.jsx'));
});
after(async () => { await server?.close(); });

test('plugin window exposes ZIP installation and an empty state', () => {
  const html = renderToStaticMarkup(React.createElement(PluginsModal, { isOpen: true, onClose() {} }));
  assert.match(html, /Install plugin ZIP/);
  assert.match(html, /application\/zip/);
  assert.match(html, /No plugins installed/);
  assert.match(html, /role="dialog"/);
  assert.equal(renderToStaticMarkup(React.createElement(PluginsModal, { isOpen: false })), '');
});
test('plugin frame URLs pass the selected session safely', () => {
  assert.equal(pluginFrameUrl('count_message'), '/api/plugins/count_message/frame');
  const url = new URL(pluginFrameUrl('count_message', 'id &?#'), 'http://localhost');
  assert.equal(url.searchParams.get('session_id'), 'id &?#');
});
test('sidebar exposes plugin management', () => {
  const html = renderToStaticMarkup(React.createElement(Sidebar, { sessions: [], onOpenPlugins() {} }));
  assert.match(html, />Plugins<\/button>/);
});
