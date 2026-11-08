/**
 * Client for `/api/sentiment` (server/providers/sentiment.js). Portable: uses
 * only the supplied fetch implementation.
 */

export async function fetchSentimentCountries({
  fetchImpl = fetch,
  signal,
} = {}) {
  const res = await fetchImpl('/api/sentiment/countries', { signal });
  if (!res.ok) throw new Error(`Countries unavailable (HTTP ${res.status})`);
  const body = await res.json();
  return body.countries ?? [];
}

const ERRORS = {
  unknown_country: 'Pick a country from the list.',
  invalid_date: 'Pick a valid centre date.',
  date_out_of_range: 'GDELT 2.0 covers 19 Feb 2015 up to today.',
  invalid_days: 'Choose between 0 and 7 days each side.',
};

/**
 * Start (or join) a search and poll until it is ready.
 * `onProgress({done,total})` fires while GDELT files download.
 */
export async function runSentimentSearch(
  { country, topic, center, days },
  { fetchImpl = fetch, signal, onProgress = () => {}, intervalMs = 900 } = {},
) {
  const params = new URLSearchParams({
    country,
    topic: topic ?? '',
    center,
    days: String(days),
  });
  const url = `/api/sentiment/search?${params}`;
  for (;;) {
    signal?.throwIfAborted();
    const res = await fetchImpl(url, { signal });
    let body = null;
    try {
      body = await res.json();
    } catch {
      /* handled below */
    }
    if (!res.ok || !body) {
      const code = body?.error;
      throw new Error(
        ERRORS[code] ??
          (code
            ? `Search failed: ${code}`
            : `Search failed (HTTP ${res.status})`),
      );
    }
    if (body.status === 'ready') return body;
    if (body.status === 'error')
      throw new Error(`Search failed: ${body.error}`);
    onProgress({ done: body.done ?? 0, total: body.total ?? 0 });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(resolve, intervalMs);
      signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(timer);
          reject(signal.reason);
        },
        { once: true },
      );
    });
  }
}

/**
 * Per-region colour value for a day. `mode` is 'time' (vs the region's own
 * window average) or 'place' (vs that day's national tone). Returns null when
 * the region has no or too few articles.
 */
export function regionValue(result, regionIndex, dayIndex, mode) {
  const v = result.regions[regionIndex]?.values?.[dayIndex];
  if (!v || v.thin) return null;
  if (mode === 'time') return v.change;
  const national = result.days[dayIndex]?.tone;
  if (national == null || v.tone == null) return null;
  const shrink = v.articles / (v.articles + 40);
  return Math.round((v.tone - national) * shrink * 100) / 100;
}

/** Client-side point-in-region lookup for hover read-outs. */
export function locateRegion(geometry, lat, lon) {
  const inRing = (ring) => {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (
        yi > lat !== yj > lat &&
        lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi
      )
        inside = !inside;
    }
    return inside;
  };
  for (let r = 0; r < geometry.length; r += 1) {
    for (const [outer, ...holes] of geometry[r].polygons) {
      if (inRing(outer) && !holes.some(inRing)) return r;
    }
  }
  return -1;
}
