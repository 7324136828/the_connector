import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { fetchCounts } from '../src/index.js';

test('fetchCounts encodes filters and returns backend counts', async () => {
  let observed;
  const server = http.createServer((req, res) => {
    observed = new URL(req.url, 'http://localhost');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ user_messages: 2, assistant_messages: 1, total_messages: 3 }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const result = await fetchCounts({ apiBaseUrl: `http://127.0.0.1:${server.address().port}/`, sessionId: 'id &?#', includeSystemSessions: true });
    assert.equal(result.total_messages, 3);
    assert.equal(observed.pathname, '/api/count_message');
    assert.equal(observed.searchParams.get('session_id'), 'id &?#');
    assert.equal(observed.searchParams.get('include_system_sessions'), 'true');
  } finally { await new Promise((resolve) => server.close(resolve)); }
});

test('unavailable backend surfaces an error and omits the all-chats session filter', async () => {
  let query;
  const server = http.createServer((req, res) => {
    query = req.url;
    res.writeHead(503, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ detail: 'Connector database unavailable' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(fetchCounts({ apiBaseUrl: `http://127.0.0.1:${server.address().port}` }), /Connector database unavailable/);
    assert.equal(query, '/api/count_message?include_system_sessions=false');
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(fetchCounts({ apiBaseUrl: `http://127.0.0.1:${server.address().port}`, signal: controller.signal }), { name: 'AbortError' });
  } finally { await new Promise((resolve) => server.close(resolve)); }
});
