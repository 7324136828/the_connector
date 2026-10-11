import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer as createHttpServer } from 'node:http';
import { createServer } from 'vite';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('the dev server proxies API and compatibility requests to the selected backend', async () => {
  const backend = createHttpServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ path: request.url }));
  });
  await new Promise((resolve) => backend.listen(0, '127.0.0.1', resolve));
  const previousBackendUrl = process.env.CONNECTOR_BACKEND_URL;
  process.env.CONNECTOR_BACKEND_URL = `http://127.0.0.1:${backend.address().port}`;
  let frontend;
  try {
    frontend = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: true } });
    await frontend.listen();
    const frontendUrl = `http://127.0.0.1:${frontend.httpServer.address().port}`;
    for (const path of ['/api/health', '/v1/models']) {
      const response = await fetch(`${frontendUrl}${path}`);
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), { path });
    }
  } finally {
    if (previousBackendUrl === undefined) delete process.env.CONNECTOR_BACKEND_URL;
    else process.env.CONNECTOR_BACKEND_URL = previousBackendUrl;
    await frontend?.close();
    await new Promise((resolve) => backend.close(resolve));
  }
});

test('direct Vite startup reads saved ports before environment defaults and preserves preferences', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'connector-vite-'));
  const settingsPath = join(directory, 'settings.json');
  const preferences = { react_port: 9230, connector_port: 9231, kokoro_port: 9232, data_dir: 'D:\\Saved Data' };
  await writeFile(settingsPath, JSON.stringify(preferences));
  const keys = ['CONNECTOR_SETTINGS_PATH', 'SETTINGS_PATH', 'CONNECTOR_BACKEND_URL', 'VITE_BACKEND_URL', 'CONNECTOR_BACKEND_PORT', 'BACKEND_PORT', 'FRONTEND_PORT'];
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  process.env.CONNECTOR_SETTINGS_PATH = settingsPath;
  process.env.SETTINGS_PATH = join(directory, 'unused-settings.json');
  delete process.env.CONNECTOR_BACKEND_URL;
  delete process.env.VITE_BACKEND_URL;
  process.env.CONNECTOR_BACKEND_PORT = '8301';
  process.env.BACKEND_PORT = '8301';
  process.env.FRONTEND_PORT = '5130';
  try {
    const { default: configure } = await import(`../vite.config.js?saved-ports=${Date.now()}`);
    const config = configure({ command: 'serve' });
    assert.equal(config.server.port, 9230);
    assert.equal(config.server.proxy['/api'].target, 'http://127.0.0.1:9231');
    assert.equal(config.server.proxy['/v1'].target, 'http://127.0.0.1:9231');
    assert.deepEqual(JSON.parse(await readFile(settingsPath, 'utf8')), preferences);
    delete process.env.CONNECTOR_SETTINGS_PATH;
    process.env.SETTINGS_PATH = settingsPath;
    const legacyConfig = configure({ command: 'serve' });
    assert.equal(legacyConfig.server.port, 9230);
    assert.equal(legacyConfig.server.proxy['/api'].target, 'http://127.0.0.1:9231');
  } finally {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
    await rm(directory, { recursive: true, force: true });
  }
});

test('a custom project port mapping supplies missing defaults without writing preferences', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'connector-port-mapping-'));
  const mappingPath = join(directory, 'port_mapping.json');
  const settingsPath = join(directory, 'settings.json');
  const defaults = { react_port: 9430, connector_port: 9431, kokoro_port: 9432 };
  await writeFile(mappingPath, JSON.stringify(defaults));
  const keys = ['CONNECTOR_SETTINGS_PATH', 'SETTINGS_PATH', 'CONNECTOR_BACKEND_URL', 'VITE_BACKEND_URL'];
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  process.env.CONNECTOR_SETTINGS_PATH = settingsPath;
  delete process.env.SETTINGS_PATH;
  delete process.env.CONNECTOR_BACKEND_URL;
  delete process.env.VITE_BACKEND_URL;
  try {
    const { configureConnector } = await import('../vite.config.js');
    const initial = configureConnector({ command: 'serve' }, mappingPath);
    assert.equal(initial.server.port, 9430);
    assert.equal(initial.server.proxy['/api'].target, 'http://127.0.0.1:9431');
    await assert.rejects(readFile(settingsPath), { code: 'ENOENT' });

    const preferences = { react_port: 9530, data_dir: 'D:\\Preserved Data', other: { keep: true } };
    await writeFile(settingsPath, JSON.stringify(preferences));
    const customized = configureConnector({ command: 'serve' }, mappingPath);
    assert.equal(customized.server.port, 9530);
    assert.equal(customized.server.proxy['/v1'].target, 'http://127.0.0.1:9431');
    assert.deepEqual(JSON.parse(await readFile(settingsPath, 'utf8')), preferences);

    process.env.CONNECTOR_BACKEND_URL = 'http://127.0.0.1:9631';
    assert.equal(configureConnector({ command: 'serve' }, mappingPath).server.proxy['/api'].target, 'http://127.0.0.1:9631');
    delete process.env.CONNECTOR_BACKEND_URL;
    await writeFile(settingsPath, '{invalid user preferences');
    const buildConfig = configureConnector({ command: 'build' }, mappingPath);
    assert.equal(buildConfig.server.port, 9430);
    assert.equal(buildConfig.server.proxy['/api'].target, 'http://127.0.0.1:9431');

    await writeFile(mappingPath, JSON.stringify({ ...defaults, connector_port: true }));
    assert.throws(() => configureConnector({ command: 'build' }, mappingPath), /connector_port.*integer/);
    await writeFile(mappingPath, JSON.stringify({ ...defaults, connector_port: defaults.react_port }));
    assert.throws(() => configureConnector({ command: 'build' }, mappingPath), /must be different/);
  } finally {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
    await rm(directory, { recursive: true, force: true });
  }
});
