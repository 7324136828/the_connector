import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { getConfigRoutes, setAgentFinalRetries, setGrossTokenLimit, setMemorySource, setRouteEffort, setRouteTokenLimit, suggestedModelId } from '../src/components/configHelpers.js';
import { createNewSession, sendMessage, runAgent, updateSessionConfig, validateConfig, createLibraryConfig, updateLibraryConfig, deleteLibraryConfig, getLibraryConfigDownloadUrl, getModels, getConfigHistory, getConfigHistoryDownloadUrl, loadConfigFile, recordConfigLoad, getPythonEnvironments, createPythonEnvironment, selectPythonEnvironment, getExportZipUrl, getAllHistoryExportUrl, clearAllHistory } from '../src/services/api.js';
import { visibleSessions } from '../src/components/sessionHelpers.js';

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

test('model token limits update direct routes and probability choices independently', () => {
  const config = {
    context_window: 12,
    sequences: [
      { provider: 'mock', model: 'mock-assistant', effort: 'low', max_input_tokens: 10000 },
      { type: 'probability', retries: 2, choices: [
        { provider: 'openai', model: 'gpt-5', probability: 70, max_output_tokens: 4000 },
        { provider: 'gemini', model: 'gemini-2.5-flash', probability: 30 },
      ] },
    ],
  };
  const routes = getConfigRoutes(config);
  const updatedInput = setRouteTokenLimit(config, routes[0].path, 'max_input_tokens', 24000);
  const updated = setRouteTokenLimit(updatedInput, routes[1].path, 'max_output_tokens', 2000);
  assert.equal(updated.sequences[0].max_input_tokens, 24000);
  assert.equal(updated.sequences[0].effort, 'low');
  assert.equal(updated.sequences[1].choices[0].max_output_tokens, 2000);
  assert.equal(updated.sequences[1].choices[0].probability, 70);
  assert.deepEqual(updated.sequences[1].choices[1], config.sequences[1].choices[1]);
  assert.equal(updated.sequences[1].retries, 2);
  assert.equal(updated.context_window, 12);
  assert.equal(config.sequences[0].max_input_tokens, 10000);
  assert.equal(config.sequences[1].choices[0].max_output_tokens, 4000);
  const cleared = setRouteTokenLimit(updated, routes[1].path, 'max_output_tokens', '');
  assert.equal(Object.hasOwn(cleared.sequences[1].choices[0], 'max_output_tokens'), false);
  assert.equal(cleared.sequences[0].max_input_tokens, 24000);
});

test('model token limit controls reject invalid numbers and unrelated fields', () => {
  const config = { sequences: [{ provider: 'mock', model: 'mock-assistant' }] };
  const path = getConfigRoutes(config)[0].path;
  for (const value of [0, -1, 1.5, Infinity, NaN, 1000000001, Number.MAX_SAFE_INTEGER + 1, '1000', null, true]) {
    assert.throws(() => setRouteTokenLimit(config, path, 'max_input_tokens', value), /positive whole numbers/);
  }
  assert.equal(setRouteTokenLimit(config, path, 'max_output_tokens', 1).sequences[0].max_output_tokens, 1);
  assert.equal(setRouteTokenLimit(config, path, 'max_input_tokens', 1000000000).sequences[0].max_input_tokens, 1000000000);
  assert.throws(() => setRouteTokenLimit(config, path, 'context_window', 10), /Unknown model token limit/);
  assert.deepEqual(config.sequences[0], { provider: 'mock', model: 'mock-assistant' });
});

test('configuration token limits are top-level and clearing removes only the chosen cap', () => {
  const config = {
    gross_max_input_token: 100000,
    context_window: 10,
    sequences: [{ provider: 'mock', model: 'mock-assistant', max_input_tokens: 40000 }],
  };
  const updated = setGrossTokenLimit(config, 'gross_max_output_token', 10000);
  assert.equal(updated.gross_max_input_token, 100000);
  assert.equal(updated.gross_max_output_token, 10000);
  assert.equal(config.gross_max_output_token, undefined);
  assert.deepEqual(updated.sequences, config.sequences);
  assert.equal(updated.context_window, 10);
  const cleared = setGrossTokenLimit(updated, 'gross_max_input_token', '');
  assert.equal(Object.hasOwn(cleared, 'gross_max_input_token'), false);
  assert.equal(cleared.gross_max_output_token, 10000);
  assert.deepEqual(cleared.sequences, config.sequences);
  assert.equal(updated.gross_max_input_token, 100000);
});

test('configuration token limit controls validate positive bounded integers', () => {
  const config = { sequences: [{ provider: 'mock', model: 'mock-assistant' }] };
  for (const value of [0, -1, 1.5, Infinity, NaN, 1000000001, Number.MAX_SAFE_INTEGER + 1, '1000', null, true]) {
    assert.throws(() => setGrossTokenLimit(config, 'gross_max_input_token', value), /positive whole numbers/);
  }
  assert.equal(setGrossTokenLimit(config, 'gross_max_input_token', 1).gross_max_input_token, 1);
  assert.equal(setGrossTokenLimit(config, 'gross_max_output_token', 1000000000).gross_max_output_token, 1000000000);
  assert.throws(() => setGrossTokenLimit(config, 'max_input_tokens', 10), /Unknown configuration token limit/);
  assert.deepEqual(config, { sequences: [{ provider: 'mock', model: 'mock-assistant' }] });
});

test('singular model token aliases can be read, changed, and cleared without restoring a hidden cap', () => {
  const config = { sequences: [{ choices: [
    { provider: 'openai', model: 'gpt-5', max_input_token: 20000, max_output_token: 1000 },
    { provider: 'mock', model: 'mock-assistant', max_input_token: 30000 },
  ] }] };
  const route = getConfigRoutes(config)[0];
  assert.equal(route.max_input_tokens, 20000);
  assert.equal(route.max_output_tokens, 1000);
  const changed = setRouteTokenLimit(config, route.path, 'max_input_tokens', 12000);
  assert.equal(changed.sequences[0].choices[0].max_input_tokens, 12000);
  assert.equal(Object.hasOwn(changed.sequences[0].choices[0], 'max_input_token'), false);
  assert.equal(changed.sequences[0].choices[0].max_output_token, 1000);
  assert.deepEqual(changed.sequences[0].choices[1], config.sequences[0].choices[1]);
  const cleared = setRouteTokenLimit(changed, route.path, 'max_output_tokens', '');
  assert.equal(Object.hasOwn(cleared.sequences[0].choices[0], 'max_output_token'), false);
  assert.equal(Object.hasOwn(cleared.sequences[0].choices[0], 'max_output_tokens'), false);
  assert.equal(getConfigRoutes(cleared)[0].max_output_tokens, undefined);
  const conflicting = { sequences: [{ provider: 'mock', model: 'mock-assistant', max_input_tokens: 2000, max_input_token: 1000 }] };
  assert.equal(getConfigRoutes(conflicting)[0].max_input_tokens, 2000);
  const clearedConflict = setRouteTokenLimit(conflicting, getConfigRoutes(conflicting)[0].path, 'max_input_tokens', '');
  assert.equal(getConfigRoutes(clearedConflict)[0].max_input_tokens, undefined);
  assert.equal(config.sequences[0].choices[0].max_input_token, 20000);
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
  assert.deepEqual(requests[0].body, { title: 'New Chat', config, user_session: true });
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
  assert.deepEqual(requests[0].body, { title: 'New Chat', config_id: 'saved-config', user_session: true });
  assert.equal(requests[1].url, '/api/providers/models');
});

test('Python environment APIs list, create, and select managed environments', async () => {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, ...options, body: options.body ? JSON.parse(options.body) : undefined });
    return { ok: true, json: async () => [] };
  };
  await getPythonEnvironments();
  await createPythonEnvironment({ name: 'Data Science' });
  await selectPythonEnvironment('environment/id');
  assert.equal(requests[0].url, '/api/python-environments');
  assert.deepEqual(requests[1].body, { name: 'Data Science', select: true });
  assert.equal(requests[1].method, 'POST');
  assert.equal(requests[2].url, '/api/python-environments/environment%2Fid/select');
  assert.equal(requests[2].method, 'POST');
});

test('history export URLs are encoded and clearing sends explicit confirmation', async () => {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, ...options, body: options.body ? JSON.parse(options.body) : undefined });
    return { ok: true, json: async () => ({ deleted_sessions: 2, status: 'cleared' }) };
  };
  assert.equal(getExportZipUrl('session/id'), '/api/sessions/session%2Fid/export-zip');
  assert.equal(getAllHistoryExportUrl(), '/api/history/export-zip');
  assert.deepEqual(await clearAllHistory(), { deleted_sessions: 2, status: 'cleared' });
  assert.equal(requests[0].url, '/api/history');
  assert.equal(requests[0].method, 'DELETE');
  assert.deepEqual(requests[0].body, { confirmation: 'DELETE' });
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
  assert.deepEqual(requests[0].body, { title: 'New Chat', history_id: 'historical-config', user_session: true });
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

test('memory source controls preserve defaults and update only the selected source', () => {
  const config = { sequences: [{ provider: 'mock', model: 'mock-assistant' }] };
  const withoutSystem = setMemorySource(config, 'system_sessions', false);
  assert.deepEqual(withoutSystem.memory_sources, {
    user_sessions: true,
    system_sessions: false,
    completion_events: true,
  });
  assert.equal(config.memory_sources, undefined);
  const withoutCompletions = setMemorySource(withoutSystem, 'completion_events', false);
  assert.equal(withoutCompletions.memory_sources.system_sessions, false);
  assert.equal(withoutCompletions.memory_sources.completion_events, false);
});

test('system sessions are hidden by default and shown when the setting is enabled', () => {
  const sessions = [
    { session_id: 'user', user_session: true },
    { session_id: 'system', user_session: false },
    { session_id: 'legacy' },
  ];
  assert.deepEqual(visibleSessions(sessions, false).map((session) => session.session_id), ['user', 'legacy']);
  assert.deepEqual(visibleSessions(sessions, true), sessions);
});

test('agent final-answer retry setting accepts only the configured range', () => {
  const config = { sequences: [{ provider: 'mock', model: 'mock-assistant' }] };
  assert.equal(setAgentFinalRetries(config, 5).agent_final_retries, 5);
  assert.throws(() => setAgentFinalRetries(config, -1), /between 0 and 15/);
  assert.throws(() => setAgentFinalRetries(config, 16), /between 0 and 15/);
  assert.throws(() => setAgentFinalRetries(config, 1.5), /whole number/);
});
