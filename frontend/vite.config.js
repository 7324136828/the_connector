import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const portNames = ['react_port', 'connector_port', 'kokoro_port'];
const projectPortMapping = new URL('../port_mapping.json', import.meta.url);

function validatePorts(values, path) {
  for (const name of portNames) {
    if (!Number.isInteger(values[name]) || values[name] < 1 || values[name] > 65535) {
      throw new Error(`${name} in ${path} must be an integer between 1 and 65535.`);
    }
  }
  return values;
}

function readPortMapping(path) {
  const values = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  if (!values || typeof values !== 'object' || Array.isArray(values)) {
    throw new Error(`Port mapping at ${path} must be a JSON object.`);
  }
  validatePorts(values, path);
  const ports = Object.fromEntries(portNames.map((name) => [name, values[name]]));
  if (new Set(Object.values(ports)).size !== portNames.length) {
    throw new Error(`Service ports in ${path} must be different.`);
  }
  return ports;
}

function readSavedPorts(defaultPorts) {
  const configHome = process.platform === 'win32'
    ? process.env.APPDATA || join(homedir(), 'AppData', 'Roaming')
    : process.env.XDG_CONFIG_HOME || join(homedir(), '.config');
  const settingsOverride = process.env.CONNECTOR_SETTINGS_PATH || process.env.SETTINGS_PATH;
  const settingsPath = settingsOverride
    ? resolve(settingsOverride.replace(/^~(?=$|[/\\])/, homedir()))
    : join(configHome, 'the_connector', 'settings.json');
  let savedSettings = {};
  try {
    savedSettings = JSON.parse(readFileSync(settingsPath, 'utf8').replace(/^\uFEFF/, ''));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (!savedSettings || typeof savedSettings !== 'object' || Array.isArray(savedSettings)) {
    throw new Error(`Settings at ${settingsPath} must be a JSON object.`);
  }
  return validatePorts({ ...defaultPorts, ...savedSettings }, settingsPath);
}

export function configureConnector({ command }, defaultsPath = projectPortMapping) {
  const defaultPorts = readPortMapping(defaultsPath);
  const savedPorts = command === 'serve' ? readSavedPorts(defaultPorts) : defaultPorts;
  const backendUrl = process.env.CONNECTOR_BACKEND_URL || process.env.VITE_BACKEND_URL
    || `http://127.0.0.1:${savedPorts.connector_port}`;
  return {
    plugins: [react()],
    server: {
      port: savedPorts.react_port,
      host: process.env.BACKEND_HOST || process.env.HOST || '0.0.0.0',
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: true,
        },
        '/v1': {
          target: backendUrl,
          changeOrigin: true,
        },
      },
    },
  };
}

export default defineConfig(configureConnector);
