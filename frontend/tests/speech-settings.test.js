import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';
import { requestSpeech, getSpeechVoices } from '../src/services/api.js';

let server;
let VoiceActorSetting;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ VoiceActorSetting } = await server.ssrLoadModule('/src/components/VoiceActorSetting.jsx'));
});

after(async () => { await server?.close(); });

test('voice settings show the selected actor and configured automatic default', () => {
  const html = renderToStaticMarkup(React.createElement(VoiceActorSetting, {
    actor: 'am_michael', voices: ['af_heart', 'am_michael'], defaultActor: 'af_heart', onChange() {},
  }));
  assert.match(html, /<label for="speech-actor"/);
  assert.match(html, /Automatic \(af_heart\)/);
  assert.match(html, /value="am_michael" selected=""/);
});

test('voice catalog errors expose a retry action', () => {
  const html = renderToStaticMarkup(React.createElement(VoiceActorSetting, {
    error: 'Kokoro unavailable', onRetry() {}, onChange() {},
  }));
  assert.match(html, /role="alert"/);
  assert.match(html, /Kokoro unavailable/);
  assert.match(html, />Retry<\/button>/);
});

test('speech requests send a selected actor and omit it for automatic mode', async (t) => {
  const calls = [];
  const audio = new Blob(['RIFF-test-wave'], { type: 'audio/wav' });
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, ...options });
    return { ok: true, blob: async () => audio };
  });
  const signal = new AbortController().signal;
  assert.equal(await requestSpeech('Hello', { actor: 'am_michael', signal }), audio);
  await requestSpeech('{"text":"Hello","actor":"af_heart"}', { actor: '' });
  assert.equal(calls[0].url, '/api/speech');
  assert.equal(calls[0].signal, signal);
  assert.deepEqual(JSON.parse(calls[0].body), { content: 'Hello', actor: 'am_michael' });
  assert.deepEqual(JSON.parse(calls[1].body), { content: '{"text":"Hello","actor":"af_heart"}' });
});

test('the actor picker uses the Connector voice catalog', async (t) => {
  const catalog = { default: 'am_michael', voices: ['af_heart', 'am_michael'] };
  t.mock.method(globalThis, 'fetch', async (url) => {
    assert.equal(url, '/api/speech/voices');
    return { ok: true, json: async () => catalog };
  });
  assert.deepEqual(await getSpeechVoices(), catalog);
});
