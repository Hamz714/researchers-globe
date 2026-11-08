/** Client for /api/rg (server/providers/researchersGlobe.js -> our FastAPI). Shapes: backend/schemas.py. */
const BASE = '/api/rg';

async function call(path, init) {
  const r = await fetch(BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    const body = await r.text();
    let msg = body.slice(0, 200);
    try {
      const j = JSON.parse(body);
      msg = j.hint ? `${j.error}: ${j.hint}` : j.detail || j.error || msg;
    } catch {
      /* not JSON */
    }
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}

export const rgApi = {
  query: (text) => call('/query', { method: 'POST', body: JSON.stringify({ text }) }),
  presets: () => call('/presets'),
  assets: (kind = 'mine') => call(`/assets?kind=${encodeURIComponent(kind)}`),
  liveFeed: (limit = 6) => call(`/live/feed?limit=${limit}`),
  liveStatus: () => call('/live/status'),
  asset: (id, question) =>
    call(`/asset/${encodeURIComponent(id)}${question ? `?q=${encodeURIComponent(question)}` : ''}`),
  evalReport: () => call('/eval'),
};
