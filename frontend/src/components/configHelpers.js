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
        routes.push({ ...step, path: stepPath, label: stepLabel, key: JSON.stringify([step.provider, step.model]) });
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

export function suggestedModelId(name) {
  return name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 100).replace(/-+$/, '');
}
