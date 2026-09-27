import React from 'react';

export function VoiceActorSetting({ actor = '', onChange, voices = [], defaultActor = 'af_heart', loading, error, onRetry, disabled }) {
  return (
    <div className="speech-setting">
      <label htmlFor="speech-actor" className="control-label">Voice actor</label>
      <select
        id="speech-actor" value={actor} onChange={(event) => onChange(event.target.value)}
        disabled={disabled || loading} aria-describedby="speech-actor-description"
      >
        <option value="">Automatic ({defaultActor})</option>
        {actor && !voices.includes(actor) && <option value={actor}>{actor}</option>}
        {voices.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
      </select>
      <span id="speech-actor-description" className="speech-setting-description">
        {loading ? 'Loading voice actors…' : actor ? 'Used when you select Speak.' : 'Uses the response actor or default.'}
      </span>
      {error && (
        <div className="speech-setting-error" role="alert">
          {error}
          <span>Retries automatically after 30 seconds.</span>
          <button type="button" className="config-button" onClick={onRetry} disabled={disabled || loading}>Retry</button>
        </div>
      )}
    </div>
  );
}
