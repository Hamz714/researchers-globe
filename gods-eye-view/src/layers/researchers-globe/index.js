import './researchers-globe.css';
import { rgApi } from './api.js';
import { createRgGlobe, TIER_COLOR } from './globe.js';

/**
 * Researcher's Globe HUD for God's Eye View: ask which iron ore mines are exposed
 * to an event; matching mines light up with a score, the exposure path and linked
 * news evidence. Ported from frontend/src/main.ts. Data: /api/rg (our FastAPI).
 * Wording: a score is a ranking, never a probability.
 */

const TIER_LABEL = { high: 'High', medium: 'Medium', low: 'Low' };
const SCORE_NOTE = 'Scores rank assets by exposure plus current evidence. A score is a ranking, not a probability.';
const TOP_LIST = 8;
// GEV layers behind the "Live layers" toggle: display only, never used in scoring.
const LIVE_LAYERS = [
  { id: 'ais-live-vessels', label: 'ships (AIS)' },
  { id: 'earthquakes', label: 'earthquakes' },
  { id: 'weather-cyclones', label: 'cyclones' },
];
const CREDITS =
  'Built on <a href="https://github.com/bilawalsidhu/gods-eye-view" target="_blank" rel="noopener noreferrer">God’s Eye View</a> (MIT) · ' +
  'Mines: <a href="https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker" target="_blank" rel="noopener noreferrer">Global Energy Monitor</a> (CC BY 4.0) · ' +
  'News: <a href="https://www.gdeltproject.org/" target="_blank" rel="noopener noreferrer">GDELT</a> · ' +
  'Attention: <a href="https://wikimedia.org/api/rest_v1/" target="_blank" rel="noopener noreferrer">Wikimedia</a> · ' +
  'Trade: <a href="https://comtradeplus.un.org/" target="_blank" rel="noopener noreferrer">UN Comtrade</a> (via WITS)';

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (x, d = 2) => (x === null || x === undefined ? '—' : Number(x).toFixed(d));
const safeUrl = (u) => (/^https?:\/\//i.test(u) ? u : '#');

const VERB = {
  EXPORTS_VIA: 'exports through',
  EXPORTS_TO: 'ships to',
  LOCATED_IN: 'is in',
  OWNED_BY: 'is owned by',
  LISTED_IN: 'is listed in',
  TOUCHES: 'is exposed to',
};

/** "US Steel Keetac Mine exports through Duluth, which is in United States, which is exposed to tariffs." */
function pathInWords(path, names, edges) {
  const name = (id) => names.get(id) ?? id.replace(/^[a-z]+_/, '');
  let s = `<b>${esc(name(path[0]))}</b>`;
  for (let i = 1; i < path.length; i++) {
    const verb = VERB[edges.get(`${path[i - 1]}>${path[i]}`) ?? ''] ?? 'links to';
    const obj = path[i].startsWith('channel_') ? `<b>${esc(name(path[i]).toLowerCase())}</b>` : esc(name(path[i]));
    s += `${i === 1 ? ' ' : ', which '}${verb} ${obj}`;
  }
  return s + '.';
}

/** Daily bars, the last `recent` days highlighted, baseline median dashed. */
function barChart(values, dates, recent, { label, unit, hot }) {
  const v = values;
  const n = v.length;
  recent = Math.min(recent, n);
  const W = 340, H = 92, top = 14, base = 70, max = Math.max(...v, 1); // prettier-ignore
  const bw = W / n;
  const y = (x) => base - (x / max) * (base - top);
  const baseline = v.slice(0, n - recent).sort((a, b) => a - b);
  const median = baseline.length ? baseline[Math.floor(baseline.length / 2)] : 0;
  const recentSum = v.slice(n - recent).reduce((a, b) => a + b, 0);
  const bars = v
    .map(
      (x, i) =>
        `<rect x="${(i * bw + 0.3).toFixed(1)}" y="${y(x).toFixed(1)}" width="${Math.max(bw - 0.6, 0.6).toFixed(1)}" height="${(base - y(x)).toFixed(1)}" class="${i >= n - recent ? 'r' : 'b'}"><title>${esc(dates[i] ?? '')}: ${x}</title></rect>`,
    )
    .join('');
  const rx = (n - recent) * bw;
  const fmtN = (x) => Math.round(x).toLocaleString('en-GB');
  return `<svg class="rg-news ${hot ? 'is-' + hot : ''}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}, last ${recent} days highlighted">
      <rect x="${rx}" y="${top - 10}" width="${W - rx}" height="${base - top + 10}" class="recent-bg"/>
      ${bars}
      <line x1="0" x2="${rx}" y1="${y(median)}" y2="${y(median)}" class="median"/>
      <text x="${W}" y="${top - 2}" text-anchor="end" class="lbl hot">last ${recent} days</text>
      <text x="0" y="${top - 2}" class="lbl">max ${fmtN(max)}/day</text>
      <text x="0" y="${H - 6}" class="lbl">${esc(dates[0] ?? '')}</text>
      <text x="${W}" y="${H - 6}" text-anchor="end" class="lbl">${esc(dates[n - 1] ?? '')}</text>
    </svg>
    <p class="rg-muted rg-small">Baseline median ${fmtN(median)} ${unit}/day (dashed) · ${fmtN(recentSum)} ${unit} in the last ${recent} days.</p>`;
}

/** GDELT news volume, then Wikipedia attention (it lags 1-2 days, so it can end earlier). */
function seriesCharts(d) {
  const s = d?.series;
  const news = s?.news_volume?.length
    ? barChart(s.news_volume, s.dates, s.recent_days, { label: 'Daily news articles', unit: 'articles' })
    : '<p class="rg-muted">No cached news series for this mine yet.</p>';
  const attention = s?.pageviews?.length
    ? barChart(s.pageviews, s.dates.slice(0, s.pageviews.length), s.recent_days, {
        label: 'Daily Wikipedia views',
        unit: 'views',
        hot: 'attention',
      })
    : '<p class="rg-muted">No Wikipedia article for this mine or its owner.</p>';
  return `<h4>News volume (GDELT)</h4>${news}<h4>Attention (Wikipedia views)</h4>${attention}
    ${d?.series_note ? `<p class="rg-muted rg-small">${esc(d.series_note)}</p>` : ''}`;
}

/**
 * Exposure path as arcs: mine -> port (EXPORTS_VIA), then port (or mine) -> destination
 * country (EXPORTS_TO). LOCATED_IN hops are locations, not shipments; an ownership
 * route (OWNED_BY, LISTED_IN) is a financial link and is not drawn past that hop.
 */
function pathArcs(r, d) {
  const byId = new Map(d.path_nodes.map((n) => [n.id, n]));
  byId.set(r.id, { id: r.id, type: 'mine', name: r.name, lat: r.lat, lon: r.lon });
  const edgeType = new Map(d.path_edges.map((e) => [`${e.src}>${e.dst}`, e.type]));
  const located = (id) => {
    const n = byId.get(id);
    return n && n.lat !== null && n.lon !== null ? n : null;
  };
  const arcs = [];
  const seen = new Set();
  const used = new Map();
  const arc = (a, b) => {
    if (a.id === b.id || seen.has(`${a.id}>${b.id}`)) return;
    seen.add(`${a.id}>${b.id}`);
    used.set(a.id, a);
    used.set(b.id, b);
    arcs.push({ from: a, to: b });
  };
  const pending = [];
  for (const p of d.paths) {
    let lastPhysical = located(p[0]);
    for (let i = 1; i < p.length; i++) {
      const type = edgeType.get(`${p[i - 1]}>${p[i]}`) ?? 'UNKNOWN';
      if (type === 'OWNED_BY' || type === 'LISTED_IN') break;
      const b = located(p[i]);
      if (!b) continue;
      if (type === 'EXPORTS_VIA' && lastPhysical) {
        arc(lastPhysical, b);
        lastPhysical = b;
      } else if (type === 'EXPORTS_TO') {
        const from = lastPhysical ?? located(p[i - 1]);
        if (from) pending.push([from, b]);
      }
    }
  }
  // A destination reached through a port is drawn from the port only, not also straight from the mine.
  const viaPort = new Set(pending.filter(([a]) => a.type === 'port').map(([, b]) => b.id));
  for (const [a, b] of pending) if (a.type === 'port' || !viaPort.has(b.id)) arc(a, b);
  const nodes = [...used.values()].filter((n) => n.id !== r.id);
  return { arcs, nodes, points: [[r.lon, r.lat], ...nodes.map((n) => [n.lon, n.lat])] };
}

/** Resolves once GEV's loading screen is gone (its startup camera restore is done). */
function whenGlobeReady(timeoutMs = 20_000) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const tick = () => {
      const ls = document.querySelector('#loading-screen');
      if (!ls || ls.classList.contains('hidden') || performance.now() - t0 > timeoutMs) setTimeout(resolve, 800);
      else setTimeout(tick, 200);
    };
    tick();
  });
}

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/**
 * Mount the HUD. `dataManager` (GEV's layer manager) is optional: without it the
 * Live layers toggle is hidden. `autoRun` (off by default): run the first preset on load.
 */
/** Answers that are not exposure rankings: a list ("filter") or a live condition screen. */
const SIMPLE_CHANNELS = new Set(['filter', 'attention', 'edits', 'weather', 'conditions']);
const isSimple = (q) => !!q && q.channels.length === 1 && SIMPLE_CHANNELS.has(q.channels[0].id);

export function mountResearchersGlobe({ viewer, dataManager = null, autoRun = false }) {
  let current = null;
  let currentQuestion = '';
  let selectedId = null;
  let allAssets = [];

  const panel = el(`
    <section class="rg-panel" aria-label="Researcher's Globe">
      <header class="rg-head">
        <div><strong>RESEARCHER'S GLOBE</strong><span>which assets does an event touch?</span></div>
        <button class="rg-collapse" type="button" aria-label="Collapse panel" title="Collapse">–</button>
      </header>
      <nav class="rg-tabs"><button type="button" data-tab="query" class="is-active">Query</button><button type="button" data-tab="results">Results</button></nav>
      <div class="rg-body" data-pane="query">
        <form class="rg-ask"><input name="q" type="text" placeholder="Which iron ore mines are exposed to …?" autocomplete="off" aria-label="Question"><button type="submit">Ask</button></form>
        <div class="rg-presets"></div>
        <div class="rg-status" hidden></div>
        <div class="rg-legend"></div>
        <label class="rg-live" hidden><input type="checkbox"> Live layers <span></span></label>
      </div>
      <div class="rg-body" data-pane="results" hidden></div>
      <footer class="rg-credits">${CREDITS}</footer>
    </section>`);
  const detail = el(`<aside class="rg-detail" aria-label="Mine details" hidden></aside>`);
  const reopen = el(`<button class="rg-reopen" type="button" hidden>RESEARCHER'S GLOBE</button>`);
  document.body.append(panel, detail, reopen);
  const $ = (sel) => panel.querySelector(sel);

  const globe = createRgGlobe(viewer, {
    onSelect: (id) => {
      const r = current?.results.find((x) => x.id === id);
      if (r) void select(r);
    },
    describe: (p) => `<b>#${p.rank} ${esc(p.name)}</b><br>${TIER_LABEL[p.tier]} tier · score ${fmt(p.score)}`,
  });
  const fitTo = (points, minSpanDeg = 6) => globe.fitTo(points, { panelOpen: !detail.hidden, minSpanDeg });

  // ---------------------------------------------------------------- chrome
  function status(msg, error = false) {
    const s = $('.rg-status');
    s.textContent = msg;
    s.classList.toggle('is-error', error);
    s.hidden = !msg;
  }
  $('.rg-collapse').addEventListener('click', () => {
    panel.hidden = true;
    reopen.hidden = false;
  });
  reopen.addEventListener('click', () => {
    panel.hidden = false;
    reopen.hidden = true;
  });
  panel.querySelectorAll('.rg-tabs button').forEach((b) =>
    b.addEventListener('click', () => {
      panel.querySelectorAll('.rg-tabs button').forEach((x) => x.classList.toggle('is-active', x === b));
      panel.querySelectorAll('.rg-body').forEach((p) => (p.hidden = p.dataset.pane !== b.dataset.tab));
      if (b.dataset.tab === 'results') void showResults();
    }),
  );
  $('.rg-ask').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = $('.rg-ask input').value.trim();
    if (text) void ask(text);
  });

  // ---------------------------------------------------------------- live layers (display only)
  if (dataManager?.setEnabled) {
    const live = $('.rg-live');
    live.hidden = false;
    live.querySelector('span').textContent = `(${LIVE_LAYERS.map((l) => l.label).join(', ')}; display only, not scored)`;
    live.querySelector('input').addEventListener('change', async (e) => {
      const on = e.target.checked;
      const failed = [];
      for (const l of LIVE_LAYERS) {
        try {
          const ok = await dataManager.setEnabled(l.id, on, { origin: 'user' });
          if (ok === false) failed.push(l.label);
        } catch (err) {
          console.warn('[Researcher’s Globe] live layer', l.id, err);
          failed.push(l.label);
        }
      }
      status(failed.length && on ? `Live layers unavailable: ${failed.join(', ')}` : '', failed.length > 0 && on);
    });
  }

  // ---------------------------------------------------------------- query flow
  async function ask(text) {
    status(`Asking: ${text}`);
    $('.rg-ask input').value = text;
    panel.querySelectorAll('.rg-presets button').forEach((b) =>
      b.classList.toggle('is-active', (b.dataset.q ?? '').trim().toLowerCase() === text.trim().toLowerCase()),
    );
    try {
      current = await rgApi.query(text);
      currentQuestion = text;
      render(current, true);
      status('');
    } catch (err) {
      status(`Query failed: ${err.message}`, true);
    }
  }

  function closeDetail() {
    detail.hidden = true;
    selectedId = null;
    globe.setPath([], []);
    globe.setSelected(null);
  }

  function render(q, fly) {
    closeDetail();
    const ranked = [...q.results].sort((a, b) => b.score - a.score);
    const rank = new Map(ranked.map((r, i) => [r.id, i + 1]));
    globe.setResults(q.results, rank);
    const lit = new Set(q.results.map((r) => r.id));
    globe.setDark(allAssets.filter((a) => !lit.has(a.id)));
    const counts = { high: 0, medium: 0, low: 0 };
    q.results.forEach((r) => counts[r.tier]++);
    const legend = $('.rg-legend');
    legend.innerHTML = `
      <div class="rg-event">${esc(q.criteria.event.name)}</div>
      <div class="rg-sub">${isSimple(q) ? esc(q.channels[0].reason) : `${esc(q.criteria.event.date)} · last ${q.criteria.window.recent_days} days vs ${q.criteria.window.baseline_days} before`}</div>
      <div class="rg-channels">${q.channels.map((c) => `<span class="rg-chip small" title="${esc(c.reason)}">${esc(c.id)}</span>`).join('') || '<em>no channels chosen</em>'}</div>
      <div class="rg-tiers">${['high', 'medium', 'low']
        .map((t) => `<span><i class="rg-dot" style="background:${TIER_COLOR[t]}"></i>${TIER_LABEL[t]} <b>${counts[t]}</b></span>`)
        .join('')}<span><i class="rg-dot dark"></i>Dark <b>${q.dark_count}</b></span></div>
      ${
        ranked.length
          ? `<div class="rg-top-title">Top ranked</div><ol class="rg-top">${ranked
              .slice(0, TOP_LIST)
              .map(
                (r) =>
                  `<li><button type="button" data-id="${esc(r.id)}" title="${esc(r.name)}"><i class="rg-dot" style="background:${TIER_COLOR[r.tier]}"></i><span class="nm">${esc(r.name)}</span><span class="sc">${fmt(r.score)}</span></button></li>`,
              )
              .join('')}</ol>`
          : '<p class="rg-empty">Nothing lights up: no asset is exposed to this event.</p>'
      }
      <p class="rg-hint">${SCORE_NOTE}</p>`;
    legend.querySelectorAll('.rg-top button').forEach((b) =>
      b.addEventListener('click', () => {
        const r = current?.results.find((x) => x.id === b.dataset.id);
        if (r) void select(r);
      }),
    );
    if (fly) {
      if (q.results.length) fitTo(q.results.map((r) => [r.lon, r.lat]));
      else globe.home();
    }
  }

  // ---------------------------------------------------------------- detail panel
  async function select(r) {
    if (isSimple(current)) return selectSimple(r);
    selectedId = r.id;
    detail.hidden = false;
    detail.innerHTML = detailHtml(r, null, true);
    bindDetail();
    globe.setSelected(r);
    globe.setPath([], []);
    try {
      const d = await rgApi.asset(r.id, currentQuestion);
      if (selectedId !== r.id) return; // another mine was clicked meanwhile
      detail.innerHTML = detailHtml(r, d, false);
      bindDetail();
      const { arcs, nodes, points } = pathArcs(r, d);
      globe.setPath(arcs, nodes);
      fitTo(points.length > 1 ? points : [[r.lon, r.lat]], points.length > 1 ? 4 : 8);
    } catch (err) {
      if (selectedId !== r.id) return;
      detail.innerHTML = detailHtml(r, null, false, err.message);
      bindDetail();
      fitTo([[r.lon, r.lat]], 8);
    }
  }
  function bindDetail() {
    detail.querySelector('.rg-close')?.addEventListener('click', closeDetail);
  }

  /** List and live-condition answers have no exposure path or news: name, tier, reason, source. */
  function selectSimple(r) {
    selectedId = r.id;
    const ch = current.channels[0].id;
    const rank = [...current.results].sort((a, b) => b.score - a.score).findIndex((x) => x.id === r.id) + 1;
    const source = ch === 'filter'
      ? '<a href="https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker" target="_blank" rel="noopener noreferrer">Global Iron Ore Mines Tracker</a> (Global Energy Monitor, CC BY 4.0)'
      : ch === 'weather' || ch === 'conditions'
        ? '<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a> current weather' +
          r.evidence.map((e) => `<br><a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>`).join('')
        : r.evidence.map((e) => `<a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>`).join('<br>');
    detail.hidden = false;
    detail.innerHTML = `
      <button class="rg-close" type="button" aria-label="Close">×</button>
      <h2>${esc(r.name)}</h2>
      <div class="rg-badges"><span class="rg-tier" style="background:${TIER_COLOR[r.tier]}">${TIER_LABEL[r.tier]} tier</span>
        <span>score <b>${fmt(r.score)}</b> · rank ${rank} of ${current.results.length}</span></div>
      <p class="rg-hint">${ch === 'filter' ? 'Ranked by design capacity among the matches: a size ranking, not a probability.' : 'Ranked by how strongly the condition holds now: a ranking, not a probability.'}</p>
      <h3>${esc(current.criteria.event.name)}</h3>
      <p>${esc(r.reason)}</p>
      <h3>Source</h3><p>${source}</p>`;
    bindDetail();
    globe.setSelected(r);
    globe.setPath([], []);
    fitTo([[r.lon, r.lat]], 8);
  }

  function detailHtml(r, d, loading, error) {
    const c = r.components;
    const names = new Map((d?.path_nodes ?? []).map((n) => [n.id, n.name]));
    const edges = new Map((d?.path_edges ?? []).map((e) => [`${e.src}>${e.dst}`, e.type]));
    const shown = r.paths.slice(0, 3);
    const more = r.paths.length - shown.length;
    const rank = current ? [...current.results].sort((a, b) => b.score - a.score).findIndex((x) => x.id === r.id) + 1 : 0;
    const relevant = r.evidence.filter((e) => e.judge === 'relevant');
    const other = r.evidence.filter((e) => e.judge !== 'relevant');
    const li = (e) =>
      `<li><a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title || e.url)}</a>
        <small>${esc(e.date)} · judge: <span class="rg-judge ${esc(e.judge)}">${esc(e.judge.replace('_', ' '))}</span> (${fmt(e.confidence)})</small></li>`;
    return `
      <button class="rg-close" type="button" aria-label="Close">×</button>
      <h2>${esc(r.name)}</h2>
      <div class="rg-badges"><span class="rg-tier" style="background:${TIER_COLOR[r.tier]}">${TIER_LABEL[r.tier]} tier</span>
        <span>score <b>${fmt(r.score)}</b>${rank ? ` · rank ${rank} of ${current.results.length}` : ''}</span></div>
      <p class="rg-hint">${SCORE_NOTE}</p>
      <h3>Exposure path</h3>
      ${
        loading
          ? '<p class="rg-muted">Loading path…</p>'
          : `<ul class="rg-paths">${shown.map((p) => `<li>${pathInWords(p, names, edges)}</li>`).join('')}</ul>
             ${more > 0 ? `<p class="rg-muted">+${more} more path${more > 1 ? 's' : ''} (combined with noisy-OR).</p>` : ''}`
      }
      <h3>News and attention around this mine</h3>
      ${loading ? '<p class="rg-muted">Loading series…</p>' : seriesCharts(d)}
      <h3>Score components</h3>
      <table class="rg-components">
        <tr><td>Exposure (path strength, 0–1)</td><td>${fmt(c.exposure)}</td></tr>
        <tr><td>News volume z (last 7 days vs baseline)</td><td>${fmt(c.news_z, 1)}</td></tr>
        <tr><td>Tone shift</td><td>${fmt(c.tone_shift, 1)}</td></tr>
        <tr><td>Wikipedia attention z</td><td>${fmt(c.attention_z, 1)}</td></tr>
        <tr><td>Relevant articles</td><td>${c.relevant_articles}</td></tr>
      </table>
      <h3>Evidence</h3>
      ${
        relevant.length
          ? `<ul class="rg-evidence">${relevant.map(li).join('')}</ul>`
          : `<div class="rg-callout"><b>Exposure only, no current evidence.</b> No news article judged relevant to this mine and event was found in the last ${current?.criteria.window.recent_days ?? 7} days, so its score comes from exposure along the paths above.</div>`
      }
      ${other.length ? `<details><summary>${other.length} other article${other.length > 1 ? 's' : ''} (judged not relevant or unsure)</summary><ul class="rg-evidence">${other.map(li).join('')}</ul></details>` : ''}
      ${error ? `<p class="rg-error">Details unavailable: ${esc(error)}</p>` : ''}
      <h3>Why</h3><p class="rg-muted">${esc(r.reason)}</p>`;
  }

  // ---------------------------------------------------------------- results tab
  async function showResults() {
    const box = panel.querySelector('[data-pane="results"]');
    box.innerHTML = '<p class="rg-muted">Loading…</p>';
    try {
      const r = await rgApi.evalReport();
      box.innerHTML = `
        ${r.note ? `<p class="rg-muted">${esc(r.note)}</p>` : ''}
        <h3>Relevance judge vs keyword baseline</h3>
        <table><tr><th>Method</th><th>Precision</th><th>Recall</th><th>n</th></tr>
        ${r.judge.map((j) => `<tr><td>${esc(j.method)}</td><td>${fmt(j.precision)}</td><td>${fmt(j.recall)}</td><td>${j.n}</td></tr>`).join('') || '<tr><td colspan=4><em>none yet</em></td></tr>'}</table>
        <p>Inter-annotator agreement (Cohen's κ): ${fmt(r.agreement_kappa)}</p>
        <h3>Backtests: precision at 10</h3>
        <table><tr><th>Event</th><th>Method</th><th>P@10</th></tr>
        ${r.backtests.map((b) => `<tr><td>${esc(b.event)}</td><td>${esc(b.method)}</td><td>${fmt(b.precision_at_10)}</td></tr>`).join('') || '<tr><td colspan=3><em>none yet</em></td></tr>'}</table>
        <h3>Negative controls</h3>
        <table><tr><th>Query</th><th>Lit</th><th>Dark</th><th>Pass</th></tr>
        ${r.negative_controls.map((n) => `<tr><td>${esc(n.query)}</td><td>${n.lit}</td><td>${n.dark}</td><td>${n.passed ? '✓' : '✗'}</td></tr>`).join('') || '<tr><td colspan=4><em>none yet</em></td></tr>'}</table>
        <p class="rg-hint">${SCORE_NOTE}</p>`;
    } catch (err) {
      box.innerHTML = `<p class="rg-error">Could not load results: ${esc(err.message)}</p>`;
    }
  }

  // ---------------------------------------------------------------- start
  async function init() {
    try {
      allAssets = await rgApi.assets().catch(() => []);
      const presets = await rgApi.presets();
      const box = $('.rg-presets');
      box.innerHTML = presets
        .map((p) => `<button type="button" class="rg-chip ${esc(p.kind)}" data-q="${esc(p.query)}" title="${esc(p.query)}">${esc(p.label)}</button>`)
        .join('');
      box.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => void ask(b.dataset.q)));
      if (!current) globe.setDark(allAssets);
      const hero = presets.find((p) => p.kind === 'hero');
      if (autoRun && hero) {
        await whenGlobeReady();
        if (!current) await ask(hero.query);
      }
    } catch (err) {
      status(`Backend not reachable (${err.message}). Is \`make backend\` running?`, true);
    }
  }
  void init();

  const api = {
    ask,
    selectById: (id) => {
      const r = current?.results.find((x) => x.id === id);
      if (r) void select(r);
    },
    destroy() {
      globe.destroy();
      panel.remove();
      detail.remove();
      reopen.remove();
    },
  };
  if (import.meta.env?.DEV) globalThis.__rg = { ...api, viewer }; // headless QA hook
  return api;
}
