/**
 * `/api/rg/*` — proxy to the Researcher's Globe FastAPI backend (`make backend`).
 *
 *   GET  /api/rg/presets          -> GET  {target}/presets
 *   GET  /api/rg/assets           -> GET  {target}/assets
 *   GET  /api/rg/asset/{id}?q=... -> GET  {target}/asset/{id}?q=...
 *   GET  /api/rg/eval             -> GET  {target}/eval
 *   POST /api/rg/query {"text"}   -> POST {target}/query
 *
 * The browser only ever talks to the GEV dev server (same origin), and the
 * backend stays on loopback. target = RG_BACKEND_URL or http://127.0.0.1:8000.
 */

const MAX_BODY_BYTES = 64 * 1024;
const ALLOWED = new Set(['GET', 'POST']);

function readBody(req, cap) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > cap) {
        reject(Object.assign(new Error('body_too_large'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

export function researchersGlobeProxy({
  target = process.env.RG_BACKEND_URL || 'http://127.0.0.1:8000',
  fetchImpl = globalThis.fetch,
  timeoutMs = 180_000, // a live (non-preset) query judges articles with the decision model
} = {}) {
  const base = String(target).replace(/\/+$/, '');

  async function handler(req, res) {
    const json = (status, value) => {
      res.writeHead(status, {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify(value));
    };
    if (!ALLOWED.has(req.method)) return json(405, { error: 'method_not_allowed' });
    const url = new URL(req.url || '/', 'http://local');
    if (url.pathname.includes('..')) return json(400, { error: 'bad_path' });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    // The client went away before we answered (req 'close' fires as soon as the body is read).
    res.on('close', () => {
      if (!res.writableEnded) controller.abort();
    });
    try {
      const body = req.method === 'POST' ? await readBody(req, MAX_BODY_BYTES) : undefined;
      const upstream = await fetchImpl(`${base}${url.pathname}${url.search}`, {
        method: req.method,
        headers: {
          Accept: 'application/json',
          ...(body ? { 'Content-Type': req.headers['content-type'] || 'application/json' } : {}),
        },
        body,
        signal: controller.signal,
        redirect: 'error',
      });
      const text = await upstream.text();
      res.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') || 'application/json',
        'Cache-Control': 'no-store',
      });
      res.end(text);
    } catch (error) {
      if (res.headersSent) return res.end();
      if (error?.status === 413) return json(413, { error: 'body_too_large' });
      json(502, {
        error: 'rg_backend_unreachable',
        detail: controller.signal.aborted ? 'timeout' : error?.cause?.code || error?.message || 'failed',
        hint: 'Is `make backend` running on ' + base + '?',
      });
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    name: 'researchers-globe',
    configureServer({ middlewares }) {
      middlewares.use('/api/rg', handler);
    },
    configurePreviewServer({ middlewares }) {
      middlewares.use('/api/rg', handler);
    },
  };
}
