/** Return every model route, including choices nested in probability groups. */
export function getConfigRoutes(config) {
  const routes = [];
  const visit = (steps, path, label) => {
    if (!Array.isArray(steps)) return;
    steps.forEach((step, index) => {
      if (!step || typeof step !== 'object') return;
      const stepPath = [...path, index];
      const stepLabel = label + (index + 1);
      if (Array.isArray(step.choices)) visit(step.choices, [...stepPath, 'choices'], stepLabel + '.');
      else if (typeof step.provider === 'string' && typeof step.model === 'string') {
        routes.push({
          ...step,
          max_input_tokens: step.max_input_tokens ?? step.max_input_token,
          max_output_tokens: step.max_output_tokens ?? step.max_output_token,
          path: stepPath, label: stepLabel, key: JSON.stringify([step.provider, step.model]),
        });
      }
    });
  };
  visit(config?.sequences, ['sequences'], '');
  return routes;
}

export function setRouteEffort(config, path, effort) {
  const updated = JSON.parse(JSON.stringify(config));
  let route = updated;
  for (const part of path) route = route[part];
  if (effort) route.effort = effort;
  else delete route.effort;
  return updated;
}

export function setRouteTokenLimit(config, path, field, value) {
  if (!['max_input_tokens', 'max_output_tokens'].includes(field)) {
    throw new Error('Unknown model token limit: ' + field);
  }
  if (value !== '' && (!Number.isSafeInteger(value) || value < 1 || value > 1000000000)) {
    throw new Error('Model token limits must be positive whole numbers up to 1,000,000,000, or blank for no model cap.');
  }
  const updated = JSON.parse(JSON.stringify(config));
  let route = updated;
  for (const part of path) route = route[part];
  delete route[field.replace(/s$/, '')];
  if (value === '') delete route[field];
  else route[field] = value;
  return updated;
}

export function setGrossTokenLimit(config, field, value) {
  if (!['gross_max_input_token', 'gross_max_output_token'].includes(field)) {
    throw new Error('Unknown configuration token limit: ' + field);
  }
  if (value !== '' && (!Number.isSafeInteger(value) || value < 1 || value > 1000000000)) {
    throw new Error('Configuration token limits must be positive whole numbers up to 1,000,000,000, or blank for no configuration cap.');
  }
  const updated = { ...config };
  if (value === '') delete updated[field];
  else updated[field] = value;
  return updated;
}

export function suggestedModelId(name) {
  return name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 100).replace(/-+$/, '');
}

export const DEFAULT_MEMORY_SOURCES = {
  user_sessions: true,
  system_sessions: true,
  completion_events: true,
};

export function setMemorySource(config, source, enabled) {
  if (!(source in DEFAULT_MEMORY_SOURCES)) throw new Error('Unknown memory source: ' + source);
  return {
    ...config,
    memory_sources: {
      ...DEFAULT_MEMORY_SOURCES,
      ...(config.memory_sources || {}),
      [source]: enabled,
    },
  };
}

export function setAgentFinalRetries(config, retries) {
  if (!Number.isInteger(retries) || retries < 0 || retries > 15) {
    throw new Error('Agent final-answer retries must be a whole number between 0 and 15.');
  }
  return { ...config, agent_final_retries: retries };
}
