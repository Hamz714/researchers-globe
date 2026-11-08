import path from 'node:path';
import zlib from 'node:zlib';
import { promises as fsp } from 'node:fs';

import {
  accumulateExport,
  buildRegionIndex,
  createAccumulator,
  dayTimestamps,
  finalizeAccumulator,
  listCountries,
  regionGeometry,
  topicMatcher,
} from '../../src/data/gdeltSentiment.js';
import { defaultSourceRoot } from './common/source-root.js';

/**
 * `/api/sentiment` — news sentiment by admin-1 region and day from GDELT 2.0.
 *
 *   GET /api/sentiment/countries
 *   GET /api/sentiment/search?country=US&topic=election&center=2026-11-03&days=3
 *
 * A search downloads the 15-minute GDELT event exports for the window (cached
 * on disk in .gev-cache/gdelt, shared with tools/gdelt-us-unrest.mjs), keeps
 * events located inside the country's regions whose article URL matches the
 * topic, and aggregates article-weighted tone. The first request starts a job
 * and returns `{status:'running', done, total}`; poll the same URL until it
 * returns `{status:'ready', ...}` or `{status:'error'}`.
 */

const GDELT_BASE = 'https://data.gdeltproject.org/gdeltv2';
const GDELT_START = '2015-02-19';
const MAX_DAYS_EACH_SIDE = 7;
const MAX_JOBS = 24;
const DAY_MS = 86_400_000;

/** Read the single entry of a GDELT zip through its central directory. */
export function unzipSingleEntry(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65_557); i -= 1) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error('not a zip archive');
  const cd = buf.readUInt32LE(eocd + 16);
  if (buf.readUInt32LE(cd) !== 0x02014b50) throw new Error('bad zip directory');
  const method = buf.readUInt16LE(cd + 10);
  const size = buf.readUInt32LE(cd + 20);
  const local = buf.readUInt32LE(cd + 42);
  if (buf.readUInt32LE(local) !== 0x04034b50) throw new Error('bad zip entry');
  const start =
    local + 30 + buf.readUInt16LE(local + 26) + buf.readUInt16LE(local + 28);
  const data = buf.subarray(start, start + size);
  if (method === 0) return data;
  if (method === 8) return zlib.inflateRawSync(data);
  throw new Error(`unsupported zip method ${method}`);
}

export function parseSearch(searchParams, { countries, today }) {
  const country = String(searchParams.get('country') || '').toUpperCase();
  const topic = String(searchParams.get('topic') || '')
    .trim()
    .slice(0, 120);
  const center = String(searchParams.get('center') || '');
  const days = Number(searchParams.get('days') ?? 3);
  if (!/^[A-Z]{2}$/.test(country) || !countries.has(country))
    return { error: 'unknown_country' };
  if (
    !/^\d{4}-\d{2}-\d{2}$/.test(center) ||
    Number.isNaN(Date.parse(`${center}T00:00:00Z`))
  )
    return { error: 'invalid_date' };
  if (center < GDELT_START || center > today)
    return { error: 'date_out_of_range' };
  if (!Number.isInteger(days) || days < 0 || days > MAX_DAYS_EACH_SIDE)
    return { error: 'invalid_days' };
  const base = Date.parse(`${center}T00:00:00Z`);
  const dayKeys = [];
  for (let k = -days; k <= days; k += 1) {
    const key = new Date(base + k * DAY_MS).toISOString().slice(0, 10);
    if (key >= GDELT_START && key <= today) dayKeys.push(key);
  }
  return { country, topic, center, days, dayKeys };
}

export function sentimentProxy({
  cacheDir = path.join(process.cwd(), '.gev-cache', 'gdelt'),
  boundariesPath = path.join(
    defaultSourceRoot,
    'src/data/local_data/natural_earth/states_provinces.json',
  ),
  fetchImpl = globalThis.fetch,
  concurrency = 8,
  now = Date.now,
} = {}) {
  let boundariesPromise;
  const indexes = new Map();
  const jobs = new Map();

  function boundaries() {
    boundariesPromise ||= fsp.readFile(boundariesPath, 'utf8').then((text) => {
      const data = JSON.parse(text);
      const countries = listCountries(data);
      return {
        data,
        countries,
        byIso: new Map(countries.map((c) => [c.iso2, c])),
      };
    });
    boundariesPromise.catch(() => {
      boundariesPromise = null;
    });
    return boundariesPromise;
  }

  function regionIndex(b, iso2) {
    if (!indexes.has(iso2)) {
      const index = buildRegionIndex(b.data, iso2);
      indexes.set(iso2, { index, geometry: regionGeometry(index) });
    }
    return indexes.get(iso2);
  }

  async function exportFile(ts, signal) {
    const name = `${ts}.export.CSV.zip`;
    const file = path.join(cacheDir, name);
    try {
      return await fsp.readFile(file);
    } catch {
      /* not cached */
    }
    try {
      await fsp.access(`${file}.404`);
      return null;
    } catch {
      /* not known missing */
    }
    for (let attempt = 1; ; attempt += 1) {
      signal.throwIfAborted();
      try {
        const res = await fetchImpl(`${GDELT_BASE}/${name}`, {
          signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
        });
        if (res.status === 404) {
          // Only remember a 404 for windows comfortably in the past.
          if (
            now() -
              Date.parse(
                `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}T00:00:00Z`,
              ) >
            2 * DAY_MS
          )
            await fsp.writeFile(`${file}.404`, '').catch(() => {});
          return null;
        }
        if (!res.ok) throw new Error(`GDELT HTTP ${res.status}`);
        const buf = Buffer.from(await res.arrayBuffer());
        await fsp.writeFile(file, buf).catch(() => {});
        return buf;
      } catch (error) {
        if (signal.aborted || attempt >= 3) throw error;
        await new Promise((r) => setTimeout(r, 800 * 2 ** attempt));
      }
    }
  }

  async function runJob(job, b) {
    const { index, geometry } = regionIndex(b, job.query.country);
    const matches = topicMatcher(job.query.topic);
    const state = createAccumulator(job.query.dayKeys, index.regions.length);
    const cutoff = now() - 20 * 60_000;
    const files = [];
    job.query.dayKeys.forEach((key, d) => {
      for (const ts of dayTimestamps(key)) {
        const t = Date.UTC(
          +ts.slice(0, 4),
          +ts.slice(4, 6) - 1,
          +ts.slice(6, 8),
          +ts.slice(8, 10),
          +ts.slice(10, 12),
        );
        if (t <= cutoff) files.push({ ts, d });
      }
    });
    job.total = files.length;
    await fsp.mkdir(cacheDir, { recursive: true });
    let next = 0;
    const worker = async () => {
      while (next < files.length) {
        const { ts, d } = files[next++];
        try {
          const buf = await exportFile(ts, job.controller.signal);
          if (buf) {
            accumulateExport(
              state,
              unzipSingleEntry(buf).toString('utf8'),
              d,
              index,
              matches,
            );
            state.days[d].files += 1;
          } else job.missing += 1;
        } catch (error) {
          if (job.controller.signal.aborted) throw error;
          job.failed += 1;
        }
        job.done += 1;
      }
    };
    await Promise.all(Array.from({ length: concurrency }, worker));
    const result = finalizeAccumulator(state, index);
    return {
      status: 'ready',
      query: {
        ...job.query,
        countryName: b.byIso.get(job.query.country)?.name ?? job.query.country,
      },
      ...result,
      geometry,
      coverage: {
        expectedFiles: job.total,
        files: job.total - job.missing - job.failed,
        missing: job.missing,
        failed: job.failed,
      },
      source: {
        name: 'GDELT Project 2.0 Events',
        url: 'https://www.gdeltproject.org/',
        note: 'Tone is GDELT’s dictionary score of article wording about events located in each region (negative = negative coverage). It measures coverage, not public opinion. Topic matching uses article web addresses.',
      },
    };
  }

  function startJob(key, query, b) {
    while (jobs.size >= MAX_JOBS) {
      const oldest = [...jobs.entries()].find(
        ([, j]) => j.status !== 'running',
      );
      if (!oldest) break;
      jobs.delete(oldest[0]);
    }
    const job = {
      status: 'running',
      query,
      done: 0,
      total: 0,
      missing: 0,
      failed: 0,
      controller: new AbortController(),
      startedAt: now(),
    };
    jobs.set(key, job);
    runJob(job, b).then(
      (result) => {
        job.status = 'ready';
        job.result = result;
        job.finishedAt = now();
      },
      (error) => {
        job.status = 'error';
        job.error = error?.message || String(error);
        job.finishedAt = now();
      },
    );
    return job;
  }

  async function handler(req, res) {
    const json = (status, value) => {
      res.writeHead(status, {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify(value));
    };
    try {
      if (req.method !== 'GET')
        return json(405, { error: 'method_not_allowed' });
      const url = new URL(req.url || '/', 'http://local');
      const b = await boundaries();
      if (url.pathname === '/countries')
        return json(200, { countries: b.countries });
      if (url.pathname !== '/search') return json(404, { error: 'not_found' });
      const today = new Date(now()).toISOString().slice(0, 10);
      const query = parseSearch(url.searchParams, {
        countries: b.byIso,
        today,
      });
      if (query.error) return json(400, { error: query.error });
      const key = JSON.stringify([
        query.country,
        query.topic.toLowerCase(),
        query.dayKeys,
      ]);
      let job = jobs.get(key);
      const includesToday = query.dayKeys.at(-1) === today;
      if (
        job &&
        job.status !== 'running' &&
        (job.status === 'error' ||
          (includesToday && now() - job.finishedAt > 15 * 60_000))
      ) {
        jobs.delete(key);
        job = null;
      }
      job ||= startJob(key, query, b);
      if (job.status === 'ready') return json(200, job.result);
      if (job.status === 'error')
        return json(502, { status: 'error', error: job.error });
      return json(200, { status: 'running', done: job.done, total: job.total });
    } catch (error) {
      json(500, {
        status: 'error',
        error: error?.message || 'sentiment_failed',
      });
    }
  }

  return {
    name: 'gdelt-sentiment',
    configureServer({ middlewares, httpServer }) {
      middlewares.use('/api/sentiment', handler);
      httpServer?.once?.('close', () => {
        for (const job of jobs.values()) job.controller.abort();
      });
    },
    configurePreviewServer({ middlewares }) {
      middlewares.use('/api/sentiment', handler);
    },
  };
}
