#!/usr/bin/env node
// GDELT 2.0 Events → US protest/riot activity by state, per day.
//
// Downloads the 15-minute GDELT 2.0 event exports for a date range, keeps
// events located in the US, and writes:
//   output/gdelt-us-unrest/daily_by_state.csv   one row per state per day
//   output/gdelt-us-unrest/periods_by_state.csv before / during / after summary
//   output/gdelt-us-unrest/events.geojson       individual protest/riot events
//   output/gdelt-us-unrest/coverage.json        which 15-min files were missing
//
// Usage:
//   node tools/gdelt-us-unrest.mjs
//   node tools/gdelt-us-unrest.mjs --from 2026-10-06 --to 2026-11-07 \
//        --during 2026-11-03,2026-11-04 --concurrency 8
//
// No dependencies. Zips are cached in .gev-cache/gdelt/ so reruns are fast.
// Data: GDELT Project (https://www.gdeltproject.org/), cite and link per its terms.

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

// ---------- CLI ----------
const args = Object.fromEntries(
  process.argv
    .slice(2)
    .reduce((acc, a, i, all) => {
      if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1]]);
      return acc;
    }, []),
);
const FROM = args.from ?? '2026-10-06';
const TO = args.to ?? '2026-11-07';
const DURING = (args.during ?? '2026-11-03,2026-11-04').split(',');
const CONCURRENCY = Number(args.concurrency ?? 8);
const CACHE_DIR = args.cache ?? '.gev-cache/gdelt';
const OUT_DIR = args.out ?? 'output/gdelt-us-unrest';
const BASE = args.base ?? 'https://data.gdeltproject.org/gdeltv2';

// ---------- GDELT 2.0 export columns (tab-separated, 61 fields) ----------
const C = {
  id: 0,
  eventCode: 26,
  rootCode: 28,
  goldstein: 30,
  numMentions: 31,
  numSources: 32,
  numArticles: 33,
  avgTone: 34,
  actor1: 6,
  actor2: 16,
  geoType: 51, // 1 country, 2 US state, 3 US city, 4 world city, 5 world state
  geoName: 52,
  geoCountry: 53, // FIPS 10-4: "US"
  geoAdm1: 54, // e.g. "USGA"
  lat: 56,
  lon: 57,
  dateAdded: 59, // YYYYMMDDHHMMSS (UTC)
  url: 60,
};

const FIPS_STATE = {
  AL: 'Alabama', AK: 'Alaska', AZ: 'Arizona', AR: 'Arkansas', CA: 'California',
  CO: 'Colorado', CT: 'Connecticut', DE: 'Delaware', DC: 'District of Columbia',
  FL: 'Florida', GA: 'Georgia', HI: 'Hawaii', ID: 'Idaho', IL: 'Illinois',
  IN: 'Indiana', IA: 'Iowa', KS: 'Kansas', KY: 'Kentucky', LA: 'Louisiana',
  ME: 'Maine', MD: 'Maryland', MA: 'Massachusetts', MI: 'Michigan',
  MN: 'Minnesota', MS: 'Mississippi', MO: 'Missouri', MT: 'Montana',
  NE: 'Nebraska', NV: 'Nevada', NH: 'New Hampshire', NJ: 'New Jersey',
  NM: 'New Mexico', NY: 'New York', NC: 'North Carolina', ND: 'North Dakota',
  OH: 'Ohio', OK: 'Oklahoma', OR: 'Oregon', PA: 'Pennsylvania',
  RI: 'Rhode Island', SC: 'South Carolina', SD: 'South Dakota',
  TN: 'Tennessee', TX: 'Texas', UT: 'Utah', VT: 'Vermont', VA: 'Virginia',
  WA: 'Washington', WV: 'West Virginia', WI: 'Wisconsin', WY: 'Wyoming',
};

const ELECTION_URL = /elect|vote|voting|ballot|midterm|poll(s|ing)|recount|certif|canvass/i;

// ---------- helpers ----------
function timestamps(from, to) {
  const out = [];
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T23:45:00Z`);
  for (let t = start; t <= end; t += 15 * 60 * 1000) {
    out.push(new Date(t).toISOString().replace(/[-:T]/g, '').slice(0, 14));
  }
  return out;
}

// Minimal single-entry zip reader via the central directory (GDELT zips hold one CSV).
function unzipFirstEntry(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('not a zip (no EOCD)');
  const cdOffset = buf.readUInt32LE(eocd + 16);
  if (buf.readUInt32LE(cdOffset) !== 0x02014b50) throw new Error('bad central directory');
  const method = buf.readUInt16LE(cdOffset + 10);
  const compSize = buf.readUInt32LE(cdOffset + 20);
  const localOffset = buf.readUInt32LE(cdOffset + 42);
  if (buf.readUInt32LE(localOffset) !== 0x04034b50) throw new Error('bad local header');
  const nameLen = buf.readUInt16LE(localOffset + 26);
  const extraLen = buf.readUInt16LE(localOffset + 28);
  const dataStart = localOffset + 30 + nameLen + extraLen;
  const data = buf.subarray(dataStart, dataStart + compSize);
  if (method === 0) return data;
  if (method === 8) return zlib.inflateRawSync(data);
  throw new Error(`unsupported zip method ${method}`);
}

async function fetchFile(ts) {
  const name = `${ts}.export.CSV.zip`;
  const cached = path.join(CACHE_DIR, name);
  if (fs.existsSync(cached)) return { ts, status: 200, buf: fs.readFileSync(cached) };
  if (fs.existsSync(cached + '.404')) return { ts, status: 404 };
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(`${BASE}/${name}`, { signal: AbortSignal.timeout(30_000) });
      if (res.status === 404) {
        fs.writeFileSync(cached + '.404', '');
        return { ts, status: 404 };
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(cached, buf);
      return { ts, status: 200, buf };
    } catch (err) {
      if (attempt === 4) return { ts, status: 'error', error: String(err.message ?? err) };
      await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
    }
  }
}

async function pool(items, n, fn, onDone) {
  let next = 0;
  const workers = Array.from({ length: n }, async () => {
    while (next < items.length) {
      const item = items[next++];
      onDone(await fn(item));
    }
  });
  await Promise.all(workers);
}

function periodOf(day) {
  if (DURING.includes(day)) return 'during';
  return day < DURING[0] ? 'before' : 'after';
}

const csvCell = (v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
const toCsv = (rows) =>
  [Object.keys(rows[0]).join(','), ...rows.map((r) => Object.values(r).map(csvCell).join(','))].join('\n') + '\n';

// ---------- main ----------
export async function main() {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const stamps = timestamps(FROM, TO).filter((ts) => {
    // Skip windows in the future (files not published yet).
    const t = Date.parse(`${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}T${ts.slice(8, 10)}:${ts.slice(10, 12)}:00Z`);
    return t < Date.now() - 20 * 60 * 1000;
  });
  console.log(`GDELT ${FROM} → ${TO}: ${stamps.length} files, during = ${DURING.join(', ')}`);

  // day -> state -> counters ; day -> totals
  const daily = new Map();
  const dayTotals = new Map();
  const features = [];
  const seen = new Set();
  const coverage = { expected: stamps.length, ok: 0, missing: [], errors: [], perDay: {} };
  let done = 0;

  const bump = (day, st) => {
    if (!daily.has(day)) daily.set(day, new Map());
    const m = daily.get(day);
    if (!m.has(st)) m.set(st, { all: 0, protest: 0, riot: 0, protestElection: 0, protestArticles: 0 });
    return m.get(st);
  };

  await pool(stamps, CONCURRENCY, fetchFile, (r) => {
    done++;
    const day = `${r.ts.slice(0, 4)}-${r.ts.slice(4, 6)}-${r.ts.slice(6, 8)}`;
    coverage.perDay[day] ??= { ok: 0, missing: 0, error: 0 };
    if (r.status === 404) { coverage.missing.push(r.ts); coverage.perDay[day].missing++; }
    else if (r.status !== 200) { coverage.errors.push({ ts: r.ts, error: r.error }); coverage.perDay[day].error++; }
    else {
      coverage.ok++;
      coverage.perDay[day].ok++;
      let text;
      try { text = unzipFirstEntry(r.buf).toString('utf8'); }
      catch (e) { coverage.errors.push({ ts: r.ts, error: e.message }); return; }
      const tot = dayTotals.get(day) ?? { allUS: 0, allWorld: 0 };
      for (const line of text.split('\n')) {
        if (!line) continue;
        const f = line.split('\t');
        if (f.length < 61) continue;
        tot.allWorld++;
        if (f[C.geoCountry] !== 'US') continue;
        tot.allUS++;
        const type = f[C.geoType];
        if (type !== '2' && type !== '3') continue; // need state-level precision
        const st = f[C.geoAdm1].slice(2);
        if (!FIPS_STATE[st]) continue;
        const c = bump(day, st);
        c.all++;
        if (f[C.rootCode] !== '14') continue;
        if (seen.has(f[C.id])) continue;
        seen.add(f[C.id]);
        const riot = f[C.eventCode] === '145';
        const election = ELECTION_URL.test(f[C.url]);
        c.protest++;
        if (riot) c.riot++;
        if (election) c.protestElection++;
        c.protestArticles += Number(f[C.numArticles]) || 0;
        const lat = Number(f[C.lat]);
        const lon = Number(f[C.lon]);
        if (Number.isFinite(lat) && Number.isFinite(lon)) {
          features.push({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [lon, lat] },
            properties: {
              id: f[C.id],
              day,
              added: f[C.dateAdded],
              period: periodOf(day),
              state: st,
              place: f[C.geoName],
              precision: type === '3' ? 'city' : 'state',
              code: f[C.eventCode],
              riot,
              electionUrl: election,
              actor1: f[C.actor1],
              actor2: f[C.actor2],
              mentions: Number(f[C.numMentions]) || 0,
              sources: Number(f[C.numSources]) || 0,
              articles: Number(f[C.numArticles]) || 0,
              goldstein: Number(f[C.goldstein]) || 0,
              tone: Number(Number(f[C.avgTone]).toFixed(2)) || 0,
              url: f[C.url],
            },
          });
        }
      }
      dayTotals.set(day, tot);
    }
    if (done % 100 === 0 || done === stamps.length) {
      process.stdout.write(`\r  ${done}/${stamps.length} files  (${coverage.missing.length} missing, ${coverage.errors.length} errors)`);
    }
  });
  process.stdout.write('\n');

  // ---------- daily_by_state.csv ----------
  const days = [...new Set(stamps.map((ts) => `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}`))].sort();
  const dailyRows = [];
  for (const day of days) {
    for (const st of Object.keys(FIPS_STATE)) {
      const c = daily.get(day)?.get(st) ?? { all: 0, protest: 0, riot: 0, protestElection: 0, protestArticles: 0 };
      const cov = coverage.perDay[day] ?? { ok: 0 };
      dailyRows.push({
        day,
        period: periodOf(day),
        state: st,
        state_name: FIPS_STATE[st],
        files_ok: cov.ok,
        all_events: c.all,
        protest_events: c.protest,
        riot_events: c.riot,
        protest_election_url: c.protestElection,
        protest_articles: c.protestArticles,
        protest_share: c.all ? +(c.protest / c.all).toFixed(5) : '',
      });
    }
  }
  fs.writeFileSync(path.join(OUT_DIR, 'daily_by_state.csv'), toCsv(dailyRows));

  // ---------- periods_by_state.csv ----------
  // Share = protest events / all state-located events in that period (controls for news volume).
  // lift = period share / before share.
  const agg = {};
  for (const r of dailyRows) {
    const k = `${r.state}|${r.period}`;
    agg[k] ??= { all: 0, protest: 0, riot: 0, election: 0, fileDays: 0 };
    agg[k].all += r.all_events;
    agg[k].protest += r.protest_events;
    agg[k].riot += r.riot_events;
    agg[k].election += r.protest_election_url;
    agg[k].fileDays += r.files_ok / 96; // a day with half its files counts as half a day
  }
  const periodRows = [];
  for (const st of Object.keys(FIPS_STATE)) {
    const row = { state: st, state_name: FIPS_STATE[st] };
    const share = {};
    for (const p of ['before', 'during', 'after']) {
      const a = agg[`${st}|${p}`] ?? { all: 0, protest: 0, riot: 0, election: 0, fileDays: 0 };
      share[p] = a.all ? a.protest / a.all : null;
      row[`${p}_protest_per_day`] = a.fileDays ? +(a.protest / a.fileDays).toFixed(2) : '';
      row[`${p}_riot`] = a.riot;
      row[`${p}_election_url`] = a.election;
      row[`${p}_share`] = share[p] == null ? '' : +share[p].toFixed(5);
    }
    row.lift_during = share.before && share.during != null ? +(share.during / share.before).toFixed(2) : '';
    row.lift_after = share.before && share.after != null ? +(share.after / share.before).toFixed(2) : '';
    periodRows.push(row);
  }
  periodRows.sort((a, b) => (Number(b.lift_after) || 0) - (Number(a.lift_after) || 0));
  fs.writeFileSync(path.join(OUT_DIR, 'periods_by_state.csv'), toCsv(periodRows));

  // ---------- events.geojson + coverage.json ----------
  fs.writeFileSync(
    path.join(OUT_DIR, 'events.geojson'),
    JSON.stringify({
      type: 'FeatureCollection',
      metadata: {
        source: 'GDELT Project 2.0 Events (https://www.gdeltproject.org/)',
        from: FROM, to: TO, during: DURING,
        note: 'CAMEO root 14 (protest); code 145 = violent protest/riot. Machine-coded from news; counts are reporting volume, not verified incidents.',
      },
      features,
    }),
  );
  coverage.totalsPerDay = Object.fromEntries([...dayTotals].sort());
  fs.writeFileSync(path.join(OUT_DIR, 'coverage.json'), JSON.stringify(coverage, null, 2));

  // ---------- console summary ----------
  const thinDays = Object.entries(coverage.perDay).filter(([, v]) => v.ok < 90).map(([d, v]) => `${d} (${v.ok}/96)`);
  console.log(`Files ok: ${coverage.ok}/${coverage.expected}. Days with <90 files: ${thinDays.length ? thinDays.join(', ') : 'none'}`);
  console.log(`US protest/riot events with state precision: ${features.length}`);
  console.log('\nTop states by protest-share lift after vs before (states with >= 5 protest events/day before):');
  console.table(
    periodRows.filter((r) => Number(r.before_protest_per_day) >= 5).slice(0, 12).map((r) => ({
      state: r.state_name,
      before_per_day: r.before_protest_per_day,
      during_per_day: r.during_protest_per_day,
      after_per_day: r.after_protest_per_day,
      lift_during: r.lift_during,
      lift_after: r.lift_after,
    })),
  );
  console.log(`\nWrote ${OUT_DIR}/{daily_by_state.csv, periods_by_state.csv, events.geojson, coverage.json}`);
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('gdelt-us-unrest.mjs')) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
