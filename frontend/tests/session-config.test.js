import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { getConfigRoutes, setRouteEffort, suggestedModelId } from '../src/components/configHelpers.js';
import { createNewSession, sendMessage, runAgent, updateSessionConfig, validateConfig, createLibraryConfig, updateLibraryConfig, deleteLibraryConfig, getLibraryConfigDownloadUrl, getModels, getConfigHistory, getConfigHistoryDownloadUrl, loadConfigFile, recordConfigLoad } from '../src/services/api.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('effort changes target the chosen probability route without altering weights or other routes', () => {
  const config = {
    past_memory: true,
    sequences: [
      { type: 'probability', retries: 2, choices: [
        { provider: 'openai', model: 'gpt-5-nano', probability: 70, effort: 'low' },
        { provider: 'gemini', model: 'gemini-2.5-flash', probability: 30 },
      ] },
      { provider: 'mock', model: 'mock-assistant', retries: 0 },
    ],
  };
  const routes = getConfigRoutes(config);
  assert.equal(routes.length, 3);
  const updated = setRouteEffort(config, routes[0].path, 'high');
  assert.equal(updated.sequences[0].choices[0].effort, 'high');
  assert.equal(updated.sequences[0].choices[0].probability, 70);
  assert.deepEqual(updated.sequences[0].choices[1], config.sequences[0].choices[1]);
  assert.deepEqual(updated.sequences[1], config.sequences[1]);
  assert.equal(config.sequences[0].choices[0].effort, 'low');
  assert.equal(updated.past_memory, true);
  assert.equal(setRouteEffort(updated, routes[0].path, '').sequences[0].choices[0].effort, undefined);
});

test('incomplete or invalid JSON structures do not crash route controls', () => {
  for (const config of [null, [], {}, { sequences: null }, { sequences: [null, 3, {}, { choices: [null] }] }]) {
    assert.deepEqual(getConfigRoutes(config), []);
  }
});

test('sessions carry configuration and messages cannot inject model or memory overrides', async () => {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ session_id: 'created-session' }) };
  };
  const config = { sequences: [{ provider: 'mock', model: 'mock-assistant' }], past_memory: true };
  const session = await createNewSession({ config, provider: 'ignored', model: 'ignored' });
  await sendMessage({ sessionId: session.session_id, message: 'Hello', provider: 'ignored', pastMemory: false });
  await runAgent({ sessionId: session.session_id, prompt: 'Remember this', provider: 'ignored' });
  await updateSessionConfig(session.session_id, { ...config, past_memory: false });
  assert.deepEqual(requests[0].body, { title: 'New Chat', config });
  assert.equal(requests[0].url, '/api/sessions');
  assert.deepEqual(requests[1].body, { session_id: 'created-session', message: 'Hello' });
  assert.deepEqual(requests[2].body, { session_id: 'created-session', prompt: 'Remember this' });
  assert.equal(requests[3].method, 'PATCH');
  assert.equal(requests[3].body.config.past_memory, false);
});

test('validation errors explain the invalid field instead of displaying object Object', async () => {
  globalThis.fetch = async () => ({
    ok: false, status: 422, statusText: 'Unprocessable Entity',
    json: async () => ({ detail: [{ loc: ['body', 'sequences', 0, 'effort'], msg: 'Unsupported effort' }] }),
  });
  await assert.rejects(validateConfig({}), /sequences.0.effort: Unsupported effort/);
});

test('saved configuration selection sends only config_id and provider catalog stays separate', async () => {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options, body: options.body ? JSON.parse(options.body) : undefined });
    return { ok: true, json: async () => ({ session_id: 'saved-selection' }) };
  };
  await createNewSession({ configId: 'saved-config', config: { sequences: [] } });
  await getModels();
  assert.deepEqual(requests[0].body, { title: 'New Chat', config_id: 'saved-config' });
  assert.equal(requests[1].url, '/api/providers/models');
});

test('library create and activation use registry APIs and deletion accepts empty responses', async () => {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options, body: options.body ? JSON.parse(options.body) : undefined });
    return { ok: true, json: async () => { if (options.method === 'DELETE') throw new SyntaxError('Empty response'); return {}; } };
  };
  const record = { name: 'My assistant', model_id: 'my-assistant', active: true, context_length: 128000, config: { sequences: [{ provider: 'mock', model: 'mock-assistant' }] } };
  await createLibraryConfig(record);
  await updateLibraryConfig('record-id', { active: false });
  assert.equal(await deleteLibraryConfig('record-id'), null);
  assert.deepEqual(requests[0].body, record);
  assert.equal(requests[1].method, 'PATCH');
  assert.deepEqual(requests[1].body, { active: false });
  assert.equal(requests[2].method, 'DELETE');
  assert.equal(getLibraryConfigDownloadUrl('record/id'), '/api/configs/record%2Fid/download');
});

test('suggested external model IDs fit the allowed stable slug format', () => {
  assert.equal(suggestedModelId(' My Research Assistant! '), 'my-research-assistant');
  assert.equal(suggestedModelId('_Demo.v2'), 'demo.v2');
  assert.equal(suggestedModelId('x'.repeat(150)).length, 100);
});

test('history selection sends only its immutable snapshot ID, separate from library selection', async () => {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options, body: options.body ? JSON.parse(options.body) : undefined });
    return { ok: true, json: async () => ({ session_id: 'historical-session' }) };
  };
  await createNewSession({ historyId: 'historical-config', configId: 'changed-library-entry', config: { sequences: [] } });
  await getConfigHistory();
  assert.deepEqual(requests[0].body, { title: 'New Chat', history_id: 'historical-config' });
  assert.equal(requests[1].url, '/api/config-history');
  assert.equal(getConfigHistoryDownloadUrl('history/id'), '/api/config-history/history%2Fid/download');
});

test('loading a JSON file records its filename and returns validated normalized configuration', async () => {
  const originalConfig = { sequences: [{ provider: 'mock', model: 'mock-assistant' }] };
  const normalized = { ...originalConfig, past_memory: true };
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ id: 'history-1', config: normalized, name: 'research.json' }) };
  };
  const record = await loadConfigFile({ name: 'research.json', text: async () => '\uFEFF' + JSON.stringify(originalConfig) });
  assert.deepEqual(record.config, normalized);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/config-history');
  assert.deepEqual(requests[0].body, { config: originalConfig, name: 'research.json', source: 'upload' });
});

test('malformed uploads are not recorded and server validation failures prevent a loaded result', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return { ok: false, status: 422, statusText: 'Unprocessable Entity', json: async () => ({ detail: 'Configuration requires at least one sequence.' }) };
  };
  await assert.rejects(loadConfigFile({ name: 'broken.json', text: async () => '{' }), /broken.json contains invalid JSON/);
  assert.equal(calls, 0);
  await assert.rejects(loadConfigFile({ name: 'empty.json', text: async () => '{"sequences":[]}' }), /at least one sequence/);
  assert.equal(calls, 1);
});

test('history persistence failures are propagated before a configuration can be selected', async () => {
  globalThis.fetch = async () => ({ ok: false, status: 500, statusText: 'Server Error', json: async () => ({ detail: 'Could not save configuration history.' }) });
  await assert.rejects(recordConfigLoad({ config: {}, source: 'history', name: 'My configuration' }), /Could not save configuration history/);
});
