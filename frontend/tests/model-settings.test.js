import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let ConfigModal;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ ConfigModal } = await server.ssrLoadModule('/src/components/ConfigModal.jsx'));
});

after(async () => { await server?.close(); });

function render(config) {
  return renderToStaticMarkup(React.createElement(ConfigModal, {
    isOpen: true, config, onClose() {}, onConfigSaved() {},
    models: [{ provider: 'openai', id: 'gpt-5', effort_levels: ['low', 'high'], default_effort: 'low' }],
  }));
}

test('model settings expose separate input and output token limits for every route', () => {
  const html = render({ context_window: 12, sequences: [
    { provider: 'mock', model: 'mock-assistant', max_input_tokens: 20000, max_output_tokens: 1000 },
    { choices: [
      { provider: 'openai', model: 'gpt-5', effort: 'high', max_output_tokens: 4000 },
      { provider: 'gemini', model: 'gemini-2.5-flash' },
    ] },
  ] });
  const tokenInputs = [...html.matchAll(/<input\b[^>]*>/g)].map(([input]) => input).filter((input) => input.includes('max_input_tokens-') || input.includes('max_output_tokens-'));
  assert.equal(tokenInputs.length, 6);
  for (const input of tokenInputs) {
    assert.match(input, /type="number"/);
    assert.match(input, /min="1" max="1000000000" step="1"/);
    assert.match(input, /placeholder="No model cap"/);
    assert.match(input, /aria-describedby="model-limits-help"/);
  }
  assert.match(tokenInputs.find((input) => input.includes('id="max_input_tokens-sequences-0"')), /value="20000"/);
  assert.match(tokenInputs.find((input) => input.includes('id="max_output_tokens-sequences-0"')), /value="1000"/);
  assert.match(tokenInputs.find((input) => input.includes('id="max_output_tokens-sequences-1-choices-0"')), /value="4000"/);
  assert.match(tokenInputs.find((input) => input.includes('id="max_input_tokens-sequences-1-choices-1"')), /value=""/);
  assert.match(html, /Input limit \(tokens\) for 2.1: openai \/ gpt-5/);
  assert.match(html, /Effort for 2.1: openai \/ gpt-5/);
  assert.match(html, /<option value="high" selected="">high<\/option>/);
  assert.match(html, /estimated prompt, including instructions, memory, messages, and tools/);
  assert.match(html, /oversized route is skipped or returns an error without trimming/);
  assert.match(html, /context_window<\/code> controls the number of recent messages/);
});

test('configuration-wide token controls expose top-level caps and describe missing-limit behavior', () => {
  const html = render({
    gross_max_input_token: 100000, gross_max_output_token: 10000,
    sequences: [{ provider: 'mock', model: 'mock-assistant' }],
  });
  const inputs = [...html.matchAll(/<input\b[^>]*>/g)].map(([input]) => input).filter((input) => input.includes('id="gross_max_'));
  assert.equal(inputs.length, 2);
  for (const input of inputs) {
    assert.match(input, /type="number" min="1" max="1000000000" step="1"/);
    assert.match(input, /aria-describedby="config-token-limits-help"/);
    assert.match(input, /placeholder="No configuration cap"/);
  }
  assert.match(inputs.find((input) => input.includes('id="gross_max_input_token"')), /value="100000"/);
  assert.match(inputs.find((input) => input.includes('id="gross_max_output_token"')), /value="10000"/);
  assert.match(html, /Configuration input limit \(tokens\)/);
  assert.match(html, /Configuration output limit \(tokens\)/);
  assert.match(html, /smaller of its model limit and the configuration limit when both are set/);
  assert.match(html, /Blank adds no Connector cap; provider capacity and lower client output limits still apply/);
  const unsetHtml = render({ sequences: [{ provider: 'mock', model: 'mock-assistant' }] });
  const unsetInputs = [...unsetHtml.matchAll(/<input\b[^>]*>/g)].map(([input]) => input).filter((input) => input.includes('id="gross_max_'));
  assert.equal(unsetInputs.length, 2);
  unsetInputs.forEach((input) => assert.match(input, /value=""/));
});

test('model controls display singular token aliases supplied in JSON', () => {
  const html = render({ sequences: [{ provider: 'mock', model: 'mock-assistant', max_input_token: 24000, max_output_token: 2000 }] });
  const inputs = [...html.matchAll(/<input\b[^>]*>/g)].map(([input]) => input);
  assert.match(inputs.find((input) => input.includes('id="max_input_tokens-sequences-0"')), /value="24000"/);
  assert.match(inputs.find((input) => input.includes('id="max_output_tokens-sequences-0"')), /value="2000"/);
});

test('malformed route structures keep the JSON editor available without showing model controls', () => {
  const html = render({ sequences: [null, {}, { choices: null }] });
  assert.match(html, /id="config-json-editor"/);
  assert.doesNotMatch(html, /Model limits and effort settings/);
});
