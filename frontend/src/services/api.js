/** API calls use the configuration saved with each session. */
const BASE_URL = '/api';

function describeError(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const location = item.loc?.filter((part) => part !== 'body').join('.');
      return [location, item.msg || JSON.stringify(item)].filter(Boolean).join(': ');
    }).join('; ');
  }
  return detail ? JSON.stringify(detail) : '';
}

async function request(path, { method = 'GET', body } = {}) {
  const response = await fetch(BASE_URL + path, {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(describeError(data?.detail || data?.error) || 'Request failed (' + response.status + ' ' + response.statusText + ')');
  }
  return data;
}

export function createNewSession({ title = 'New Chat', config, configId, historyId }) {
  const selection = historyId ? { history_id: historyId } : configId ? { config_id: configId } : { config };
  return request('/sessions', { method: 'POST', body: { title, ...selection, user_session: true } });
}

export function updateSessionConfig(sessionId, config) {
  return request('/sessions/' + encodeURIComponent(sessionId), { method: 'PATCH', body: { config } });
}

export function sendMessage({ sessionId, message }) {
  return request('/chat', { method: 'POST', body: { session_id: sessionId, message } });
}

export function closeSession(sessionId) {
  return request('/close?session_id=' + encodeURIComponent(sessionId), { method: 'DELETE' });
}

export function getSessions() { return request('/sessions'); }
export function getSession(sessionId) { return request('/sessions/' + encodeURIComponent(sessionId)); }
export function getModels() { return request('/providers/models'); }
export function getExampleConfig() { return request('/config/example'); }
export function getExampleConfigUrl() { return BASE_URL + '/config/example'; }
export function validateConfig(config) { return request('/config/validate', { method: 'POST', body: config }); }
export function getModelCapabilities(provider, model) {
  return request('/models/capabilities', { method: 'POST', body: { provider, model } });
}

export function getLibraryConfigs() { return request('/configs'); }
export function createLibraryConfig(record) { return request('/configs', { method: 'POST', body: record }); }
export function updateLibraryConfig(id, changes) {
  return request('/configs/' + encodeURIComponent(id), { method: 'PATCH', body: changes });
}
export function deleteLibraryConfig(id) {
  return request('/configs/' + encodeURIComponent(id), { method: 'DELETE' });
}
export function getLibraryConfigDownloadUrl(id) { return BASE_URL + '/configs/' + encodeURIComponent(id) + '/download'; }

export function getConfigHistory() { return request('/config-history'); }
export function recordConfigLoad({ config, name, source = 'editor' }) {
  return request('/config-history', { method: 'POST', body: { config, ...(name ? { name } : {}), source } });
}
export function getConfigHistoryDownloadUrl(id) { return BASE_URL + '/config-history/' + encodeURIComponent(id) + '/download'; }

export async function loadConfigFile(file) {
  const text = (await file.text()).replace(/^\uFEFF/, '');
  let config;
  try { config = JSON.parse(text); }
  catch (err) { throw new Error(file.name + ' contains invalid JSON: ' + err.message); }
  // Recording also validates and normalizes the upload before it enters the editor.
  return recordConfigLoad({ config, name: file.name, source: 'upload' });
}

export function runAgent({ prompt, sessionId }) {
  return request('/agent/run', { method: 'POST', body: { prompt, session_id: sessionId } });
}

export function getAgentTools() { return request('/agent/tools'); }
export function getHealth() { return request('/health'); }
export function getExportZipUrl(sessionId) {
  return BASE_URL + '/sessions/' + encodeURIComponent(sessionId) + '/export-zip';
}
