import React from 'react';
import { getConfigHistoryDownloadUrl } from '../services/api';
import { getConfigRoutes } from './configHelpers';

const sourceLabels = { upload: 'Uploaded file', editor: 'Configuration editor', session: 'Saved session', library: 'Configuration library', history: 'History' };

function formatLoadedTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ConfigHistoryList({ records, filter = '', loading, onSelect, onSave }) {
  const query = filter.trim().toLowerCase();
  const entries = records.filter((entry) => [entry.name, entry.source, ...getConfigRoutes(entry.config).flatMap((route) => [route.provider, route.model, route.effort])]
    .some((value) => value?.toLowerCase().includes(query)))
    .sort((left, right) => Date.parse(right.last_loaded_at) - Date.parse(left.last_loaded_at));

  return (
    <>
      {!loading && records.length === 0 && <p className="library-empty">No configurations loaded yet. Choose a config.json file in the configuration editor to keep it here for reuse.</p>}
      {!loading && records.length > 0 && entries.length === 0 && <p className="library-empty">No matching configuration history.</p>}
      <div className="library-list">
        {entries.map((entry) => (
          <article className="library-card" key={entry.id}>
            <div className="library-card-heading">
              <h3>{entry.name || 'Untitled configuration'}</h3>
              <span className="badge-tag badge-mock">{sourceLabels[entry.source] || entry.source}</span>
            </div>
            <p className="config-description history-times">
              Last loaded <time dateTime={entry.last_loaded_at}>{formatLoadedTime(entry.last_loaded_at)}</time>
              {' · '}{entry.load_count} {entry.load_count === 1 ? 'load' : 'loads'}
              <br />First loaded <time dateTime={entry.first_loaded_at}>{formatLoadedTime(entry.first_loaded_at)}</time>
            </p>
            <ul className="history-routes" aria-label="Configured models and efforts">
              {getConfigRoutes(entry.config).map((route) => (
                <li key={route.path.join('-')}>
                  <span>{route.provider} / {route.model}</span>
                  <span className="history-effort">Effort: {route.effort || 'provider default'}</span>
                </li>
              ))}
            </ul>
            <details className="history-json">
              <summary>View config.json</summary>
              <pre tabIndex={0} aria-label={'Configuration JSON for ' + (entry.name || 'untitled configuration')}><code>{JSON.stringify(entry.config, null, 2)}</code></pre>
            </details>
            <div className="config-actions">
              <button className="config-button primary" disabled={loading} onClick={() => onSelect(entry)}>Use in new chat</button>
              <button className="config-button" disabled={loading} onClick={() => onSave(entry)}>Save to library</button>
              <a className="config-button" href={getConfigHistoryDownloadUrl(entry.id)} download="config.json">Download</a>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}
