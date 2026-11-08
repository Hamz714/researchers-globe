import "./style.css";
import { api } from "./api";
import { createGlobe, TIER_COLOR, type Arc, type Located } from "./globe";
import type { AssetDetail, AssetPoint, EvalReport, PathEdge, PathNode, QueryResult, Result, ScreenSeries, Tier } from "./types";

// ----------------------------------------------------------------- constants
const TIER_LABEL: Record<Tier, string> = { high: "High", medium: "Medium", low: "Low" };
const SCORE_NOTE = "Scores rank assets by exposure plus current evidence. A score is a ranking, not a probability.";
const SCREEN_NOTE = "Scores rank places by how strongly the condition holds now. A score is a ranking, not a probability.";
const TOP_LIST = 8;

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const fmt = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const safeUrl = (u: string) => (/^https?:\/\//i.test(u) ? u : "#");

let current: QueryResult | null = null;
let currentQuestion = "";
let selectedId: string | null = null;
// Condition screens ("which US states / mines where <condition>?") answer with one of these channels.
// "filter" = a list question with no event ("show all mines in Brazil"), same simple panel.
const SCREEN_CHANNELS = new Set(["attention", "edits", "weather", "filter"]);
const isScreen = (q: QueryResult | null) => !!q && q.channels.length === 1 && SCREEN_CHANNELS.has(q.channels[0].id);
const assetKind = (q: QueryResult) => (q.criteria.asset_type === "us_state" ? "state" : "mine") as "state" | "mine";
const assetsByKind: Partial<Record<"mine" | "state", AssetPoint[]>> = {};
async function assetsFor(kind: "mine" | "state"): Promise<AssetPoint[]> {
  if (!assetsByKind[kind]) assetsByKind[kind] = await api.assets(kind).catch(() => []);
  return assetsByKind[kind]!;
}

// ----------------------------------------------------------------- globe (Cesium, see globe.ts)
const globe = createGlobe(
  "map",
  (id) => {
    const r = current?.results.find((x) => x.id === id);
    if (r) void select(r);
  },
  (p) => `<b>#${p.rank} ${esc(p.name)}</b><br>${TIER_LABEL[p.tier]} tier · score ${fmt(p.score)}`,
);

/** Fly so the given points are in view, leaving room for the side panel when it is open. */
function fitTo(points: [number, number][], minSpanDeg = 6) {
  globe.fitTo(points, { panelOpen: !$("#panel").hidden, minSpanDeg });
}

// ----------------------------------------------------------------- query flow
async function ask(text: string) {
  status(`Asking: ${text}`);
  ($("#question") as HTMLInputElement).value = text;
  document.querySelectorAll<HTMLButtonElement>("#presets button").forEach((b) =>
    b.classList.toggle("active", (b.dataset.q ?? "").trim().toLowerCase() === text.trim().toLowerCase()),
  );
  try {
    current = await api.query(text);
    currentQuestion = text;
    render(current, true);
    status("");
  } catch (err) {
    status(`Query failed: ${(err as Error).message}`, true);
  }
}

function render(q: QueryResult, fly: boolean) {
  clearPath();
  selectedId = null;
  $("#panel").hidden = true;
  globe.setSelected(null);
  const ranked = [...q.results].sort((a, b) => b.score - a.score);
  const rank = new Map(ranked.map((r, i) => [r.id, i + 1]));
  globe.setResults(q.results, rank);
  const lit = new Set(q.results.map((r) => r.id));
  globe.setDark([]);
  void assetsFor(assetKind(q)).then((all) => {
    if (current === q) globe.setDark(all.filter((a) => !lit.has(a.id)));
  });
  const screen = isScreen(q);

  const counts = { high: 0, medium: 0, low: 0 } as Record<Tier, number>;
  q.results.forEach((r) => counts[r.tier]++);
  $("#legend").innerHTML = `
    <div class="legend-title">${esc(q.criteria.event.name)}</div>
    <div class="legend-sub">${screen
      ? esc(q.channels[0].reason)
      : `${esc(q.criteria.event.date)} · last ${q.criteria.window.recent_days} days vs ${q.criteria.window.baseline_days} before`}</div>
    <div class="channels">${q.channels.map((c) => `<span class="chip small" title="${esc(c.reason)}">${esc(c.id)}</span>`).join("") || "<em>no channels chosen</em>"}</div>
    <div class="tiers">${(["high", "medium", "low"] as Tier[])
      .map((t) => `<span><span class="dot" style="background:${TIER_COLOR[t]}"></span>${TIER_LABEL[t]} <b>${counts[t]}</b></span>`)
      .join("")}<span><span class="dot dark"></span>Dark <b>${q.dark_count}</b></span></div>
    ${ranked.length ? `<div class="top-title">Top ranked</div><ol class="top">${ranked
      .slice(0, TOP_LIST)
      .map((r) => `<li><button data-id="${esc(r.id)}" title="${esc(r.name)}"><span class="dot" style="background:${TIER_COLOR[r.tier]}"></span><span class="nm">${esc(r.name)}</span><span class="sc">${fmt(r.score)}</span></button></li>`)
      .join("")}</ol>` : `<p class="empty">Nothing lights up: ${screen ? "nothing meets this condition right now" : "no asset is exposed to this event"}.</p>`}
    <div class="hint">${screen ? SCREEN_NOTE : SCORE_NOTE}</div>`;
  $("#legend").querySelectorAll<HTMLButtonElement>(".top button").forEach((b) =>
    b.addEventListener("click", () => {
      const r = current?.results.find((x) => x.id === b.dataset.id);
      if (r) void select(r);
    }),
  );
  if (fly) {
    if (q.results.length) fitTo(q.results.map((r) => [r.lon, r.lat]));
    else globe.home();
  }
}

// ----------------------------------------------------------------- side panel
async function select(r: Result) {
  if (isScreen(current)) return selectScreen(r);
  selectedId = r.id;
  const panel = $("#panel");
  panel.hidden = false;
  panel.innerHTML = panelHtml(r, null, true);
  bindPanel();
  globe.setSelected(r);
  clearPath();
  try {
    const detail = await api.asset(r.id, currentQuestion);
    if (selectedId !== r.id) return; // another mine was clicked meanwhile
    panel.innerHTML = panelHtml(r, detail, false);
    bindPanel();
    const pts = drawPath(r, detail);
    fitTo(pts.length > 1 ? pts : [[r.lon, r.lat]], pts.length > 1 ? 4 : 8);
  } catch (err) {
    if (selectedId !== r.id) return;
    panel.innerHTML = panelHtml(r, null, false, (err as Error).message);
    bindPanel();
    fitTo([[r.lon, r.lat]], 8);
  }
}

function bindPanel() {
  $("#panel").querySelector(".close")?.addEventListener("click", () => {
    $("#panel").hidden = true;
    selectedId = null;
    clearPath();
    globe.setSelected(null);
  });
}

const VERB: Record<string, string> = {
  EXPORTS_VIA: "exports through",
  EXPORTS_TO: "ships to",
  LOCATED_IN: "is in",
  OWNED_BY: "is owned by",
  LISTED_IN: "is listed in",
  TOUCHES: "is exposed to",
};

/** "US Steel Keetac Mine exports through Duluth, which is in United States, which is exposed to tariffs." */
export function pathInWords(path: string[], names: Map<string, string>, edges: Map<string, string>): string {
  const name = (id: string) => names.get(id) ?? id.replace(/^[a-z]+_/, "");
  let s = `<b>${esc(name(path[0]))}</b>`;
  for (let i = 1; i < path.length; i++) {
    const verb = VERB[edges.get(`${path[i - 1]}>${path[i]}`) ?? ""] ?? "links to";
    const obj = path[i].startsWith("channel_") ? `<b>${esc(name(path[i]).toLowerCase())}</b>` : esc(name(path[i]));
    s += `${i === 1 ? " " : ", which "}${verb} ${obj}`;
  }
  return s + ".";
}

function panelHtml(r: Result, d: AssetDetail | null, loading: boolean, error?: string): string {
  const c = r.components;
  const names = new Map((d?.path_nodes ?? []).map((n) => [n.id, n.name]));
  const edges = new Map((d?.path_edges ?? []).map((e: PathEdge) => [`${e.src}>${e.dst}`, e.type]));
  const shown = r.paths.slice(0, 3);
  const more = r.paths.length - shown.length;
  const rank = current ? [...current.results].sort((a, b) => b.score - a.score).findIndex((x) => x.id === r.id) + 1 : 0;
  const relevant = r.evidence.filter((e) => e.judge === "relevant");
  const other = r.evidence.filter((e) => e.judge !== "relevant");
  const evidenceLi = (e: (typeof r.evidence)[number]) =>
    `<li><a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title || e.url)}</a>
      <small>${esc(e.date)} · judge: <span class="judge ${e.judge}">${esc(e.judge.replace("_", " "))}</span> (${fmt(e.confidence)})</small></li>`;
  return `
    <button class="close" aria-label="Close">×</button>
    <h2>${esc(r.name)}</h2>
    <div class="badges"><span class="tier" style="background:${TIER_COLOR[r.tier]}">${TIER_LABEL[r.tier]} tier</span>
      <span class="score">score <b>${fmt(r.score)}</b>${rank ? ` · rank ${rank} of ${current!.results.length}` : ""}</span></div>
    <p class="score-note">${SCORE_NOTE}</p>

    <h3>Exposure path</h3>
    ${loading ? `<p class="muted">Loading path…</p>` : `<ul class="paths">${shown.map((p) => `<li>${pathInWords(p, names, edges)}</li>`).join("")}</ul>
    ${more > 0 ? `<p class="muted">+${more} more path${more > 1 ? "s" : ""} (scored with noisy-OR).</p>` : ""}`}

    <h3>News around this mine</h3>
    ${loading ? `<p class="muted">Loading news series…</p>` : newsChart(d)}

    <h3>Score components</h3>
    <table class="components">
      <tr><td>Exposure (path strength, 0–1)</td><td>${fmt(c.exposure)}</td></tr>
      <tr><td>News volume z (last 7 days vs baseline)</td><td>${fmt(c.news_z, 1)}</td></tr>
      <tr><td>Tone shift</td><td>${fmt(c.tone_shift, 1)}</td></tr>
      <tr><td>Wikipedia attention z</td><td>${fmt(c.attention_z, 1)}</td></tr>
      <tr><td>Relevant articles</td><td>${c.relevant_articles}</td></tr>
    </table>

    <h3>Evidence</h3>
    ${relevant.length
      ? `<ul class="evidence">${relevant.map(evidenceLi).join("")}</ul>`
      : `<div class="callout"><b>Exposure only, no current evidence.</b> No news article judged relevant to this mine and event was found in the last ${current?.criteria.window.recent_days ?? 7} days, so its score comes from exposure along the paths above.</div>`}
    ${other.length ? `<details><summary>${other.length} other article${other.length > 1 ? "s" : ""} (judged not relevant or unsure)</summary><ul class="evidence">${other.map(evidenceLi).join("")}</ul></details>` : ""}
    ${error ? `<p class="error">Details unavailable: ${esc(error)}</p>` : ""}
    <h3>Why</h3><p class="muted">${esc(r.reason)}</p>`;
}

/** 60-day baseline + 7 recent days of GDELT article counts, recent days highlighted. */
function newsChart(d: AssetDetail | null): string {
  const s = d?.series;
  if (!s || !s.news_volume.length) {
    return `<p class="muted">${esc(d?.series_note || "No news series for this mine yet.")}</p>`;
  }
  const v = s.news_volume, n = v.length, recent = Math.min(s.recent_days, n);
  const W = 340, H = 92, top = 14, base = 70, max = Math.max(...v, 1);
  const bw = W / n;
  const y = (x: number) => base - (x / max) * (base - top);
  const baseline = v.slice(0, n - recent).sort((a, b) => a - b);
  const median = baseline.length ? baseline[Math.floor(baseline.length / 2)] : 0;
  const recentSum = v.slice(n - recent).reduce((a, b) => a + b, 0);
  const bars = v
    .map((x, i) => `<rect x="${(i * bw + 0.3).toFixed(1)}" y="${y(x).toFixed(1)}" width="${Math.max(bw - 0.6, 0.6).toFixed(1)}" height="${(base - y(x)).toFixed(1)}" class="${i >= n - recent ? "r" : "b"}"><title>${esc(s.dates[i] ?? "")}: ${x}</title></rect>`)
    .join("");
  const rx = (n - recent) * bw;
  return `<svg class="news" viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily news articles, last ${recent} days highlighted">
      <rect x="${rx}" y="${top - 10}" width="${W - rx}" height="${base - top + 10}" class="recent-bg"/>
      ${bars}
      <line x1="0" x2="${rx}" y1="${y(median)}" y2="${y(median)}" class="median"/>
      <text x="${W}" y="${top - 2}" text-anchor="end" class="lbl hot">last ${recent} days</text>
      <text x="0" y="${top - 2}" class="lbl">max ${max}/day</text>
      <text x="0" y="${H - 6}" class="lbl">${esc(s.dates[0] ?? "")}</text>
      <text x="${W}" y="${H - 6}" text-anchor="end" class="lbl">${esc(s.dates[n - 1] ?? "")}</text>
    </svg>
    <p class="muted small">Baseline median ${median}/day (dashed) · ${recentSum} articles in the last ${recent} days. ${esc(d?.series_note ?? "")}</p>`;
}

// ----------------------------------------------------------------- condition-screen panel
async function selectScreen(r: Result) {
  selectedId = r.id;
  const panel = $("#panel");
  const ch = current!.channels[0].id;
  const kind = ch === "attention" ? "views" : ch === "edits" ? "edits" : null;
  panel.hidden = false;
  panel.innerHTML = screenPanelHtml(r, null, kind !== null);
  bindPanel();
  globe.setSelected(r);
  clearPath();
  fitTo([[r.lon, r.lat]], r.id.startsWith("state_") ? 14 : 8);
  if (!kind) return;
  try {
    const s = await api.series(kind, r.id);
    if (selectedId !== r.id) return;
    panel.innerHTML = screenPanelHtml(r, s, false);
  } catch (err) {
    if (selectedId !== r.id) return;
    panel.innerHTML = screenPanelHtml(r, null, false, (err as Error).message);
  }
  bindPanel();
}

function screenPanelHtml(r: Result, s: ScreenSeries | null, loading: boolean, error?: string): string {
  const q = current!;
  const ch = q.channels[0].id;
  const rank = [...q.results].sort((a, b) => b.score - a.score).findIndex((x) => x.id === r.id) + 1;
  const source = ch === "filter"
    ? `<a href="https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker" target="_blank" rel="noopener noreferrer">Global Iron Ore Mines Tracker</a> (Global Energy Monitor, CC BY 4.0)`
    : ch === "weather"
    ? `<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a> current weather`
    : r.evidence.map((e) => `<a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>`).join("<br>");
  return `
    <button class="close" aria-label="Close">×</button>
    <h2>${esc(r.name)}</h2>
    <div class="badges"><span class="tier" style="background:${TIER_COLOR[r.tier]}">${TIER_LABEL[r.tier]} tier</span>
      <span class="score">score <b>${fmt(r.score)}</b> · rank ${rank} of ${q.results.length}</span></div>
    <p class="score-note">${ch === "filter" ? "Ranked by design capacity among the matches: a size ranking, not a probability." : SCREEN_NOTE}</p>
    <h3>${esc(q.criteria.event.name)}</h3>
    <p>${esc(r.reason)}</p>
    ${ch === "weather" || ch === "filter" ? "" : `<h3>Wikipedia ${ch === "attention" ? "views" : "edits"} per day</h3>
      ${loading ? `<p class="muted">Loading series…</p>` : s ? seriesChart(s) : `<p class="muted">No series.</p>`}
      <table class="components"><tr><td>Robust z (last ${q.criteria.window.recent_days} days vs ${q.criteria.window.baseline_days} before)</td><td>${fmt(r.components.attention_z, 1)}</td></tr></table>`}
    <h3>Source</h3><p>${source}</p>
    ${error ? `<p class="error">Series unavailable: ${esc(error)}</p>` : ""}`;
}

/** Daily bars, the last `recent_days` highlighted, baseline median dashed. */
function seriesChart(s: ScreenSeries): string {
  const v = s.values, n = v.length, recent = Math.min(s.recent_days, n);
  const W = 340, H = 92, top = 14, base = 70, max = Math.max(...v, 1);
  const bw = W / n;
  const y = (x: number) => base - (x / max) * (base - top);
  const baseline = v.slice(0, n - recent).sort((a, b) => a - b);
  const median = baseline.length ? baseline[Math.floor(baseline.length / 2)] : 0;
  const recentSum = v.slice(n - recent).reduce((a, b) => a + b, 0);
  const rx = (n - recent) * bw;
  const nf = (x: number) => Math.round(x).toLocaleString("en-GB");
  const bars = v.map((x, i) => `<rect x="${(i * bw + 0.3).toFixed(1)}" y="${y(x).toFixed(1)}" width="${Math.max(bw - 0.6, 0.6).toFixed(1)}" height="${(base - y(x)).toFixed(1)}" class="${i >= n - recent ? "r" : "b"}"><title>${esc(s.dates[i] ?? "")}: ${nf(x)}</title></rect>`).join("");
  return `<svg class="news" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(s.label)}">
      <rect x="${rx}" y="${top - 10}" width="${W - rx}" height="${base - top + 10}" class="recent-bg"/>
      ${bars}
      <line x1="0" x2="${rx}" y1="${y(median)}" y2="${y(median)}" class="median"/>
      <text x="${W}" y="${top - 2}" text-anchor="end" class="lbl hot">last ${recent} days</text>
      <text x="0" y="${top - 2}" class="lbl">max ${nf(max)}/day</text>
      <text x="0" y="${H - 6}" class="lbl">${esc(s.dates[0] ?? "")}</text>
      <text x="${W}" y="${H - 6}" text-anchor="end" class="lbl">${esc(s.dates[n - 1] ?? "")}</text>
    </svg>
    <p class="muted small">Baseline median ${nf(median)} ${esc(s.unit)}/day (dashed) · ${nf(recentSum)} ${esc(s.unit)} in the last ${recent} days.</p>`;
}

// ----------------------------------------------------------------- exposure path on the globe
function clearPath() {
  globe.setPath([], []);
}

/**
 * Draw the selected mine's exposure path as great-circle arcs: mine → port (EXPORTS_VIA), then
 * port (or mine) → destination country (EXPORTS_TO). "Is in" hops (LOCATED_IN) are locations, not
 * shipments, so they are not drawn; a path that goes through ownership (OWNED_BY, LISTED_IN) is a
 * financial link and is not drawn past that hop.
 * Returns the drawn points for framing.
 */
function drawPath(r: Result, d: AssetDetail): [number, number][] {
  const byId = new Map<string, PathNode>(d.path_nodes.map((n) => [n.id, n]));
  byId.set(r.id, { id: r.id, type: "mine", name: r.name, lat: r.lat, lon: r.lon });
  const edgeType = new Map(d.path_edges.map((e) => [`${e.src}>${e.dst}`, e.type]));
  const located = (id: string) => {
    const n = byId.get(id);
    return n && n.lat !== null && n.lon !== null ? (n as PathNode & { lat: number; lon: number }) : null;
  };
  const arcs: Arc[] = [];
  const seen = new Set<string>();
  const used = new Map<string, PathNode & { lat: number; lon: number }>();
  const arc = (a: PathNode & { lat: number; lon: number }, b: PathNode & { lat: number; lon: number }) => {
    if (a.id === b.id || seen.has(`${a.id}>${b.id}`)) return;
    seen.add(`${a.id}>${b.id}`);
    used.set(a.id, a);
    used.set(b.id, b);
    arcs.push({ from: a, to: b });
  };
  const pending: [PathNode & { lat: number; lon: number }, PathNode & { lat: number; lon: number }][] = [];
  for (const p of d.paths) {
    let lastPhysical = located(p[0]); // the mine, then the port it ships through
    for (let i = 1; i < p.length; i++) {
      const type = edgeType.get(`${p[i - 1]}>${p[i]}`) ?? "UNKNOWN";
      if (type === "OWNED_BY" || type === "LISTED_IN") break; // ownership route: a financial link, not a shipment
      const b = located(p[i]);
      if (!b) continue;
      if (type === "EXPORTS_VIA" && lastPhysical) {
        arc(lastPhysical, b);
        lastPhysical = b;
      } else if (type === "EXPORTS_TO") {
        const from = lastPhysical ?? located(p[i - 1]);
        if (from) pending.push([from, b]);
      }
    }
  }
  // A destination reached through a port is drawn from the port only, not also straight from the mine.
  const viaPort = new Set(pending.filter(([a]) => a.type === "port").map(([, b]) => b.id));
  for (const [a, b] of pending) if (a.type === "port" || !viaPort.has(b.id)) arc(a, b);
  globe.setPath(arcs, [...used.values()].filter((n): n is Located & PathNode & { lat: number; lon: number } => n.id !== r.id));
  return [[r.lon, r.lat], ...[...used.values()].filter((n) => n.id !== r.id).map((n) => [n.lon, n.lat] as [number, number])];
}

// ----------------------------------------------------------------- results tab
async function showResults() {
  const el = $("#results");
  el.innerHTML = "<p>Loading…</p>";
  try {
    const r: EvalReport = await api.evalReport();
    el.innerHTML = `
      <h2>Results</h2>
      ${r.note ? `<p class="note">${esc(r.note)}</p>` : ""}
      <h3>Relevance judge vs keyword baseline</h3>
      <table><tr><th>Method</th><th>Precision</th><th>Recall</th><th>n</th></tr>
      ${r.judge.map((j) => `<tr><td>${esc(j.method)}</td><td>${fmt(j.precision)}</td><td>${fmt(j.recall)}</td><td>${j.n}</td></tr>`).join("")}</table>
      <p>Inter-annotator agreement (Cohen's κ): ${fmt(r.agreement_kappa)}</p>
      <h3>Backtests: precision at 10</h3>
      <table><tr><th>Event</th><th>Method</th><th>P@10</th></tr>
      ${r.backtests.map((b) => `<tr><td>${esc(b.event)}</td><td>${esc(b.method)}</td><td>${fmt(b.precision_at_10)}</td></tr>`).join("") || "<tr><td colspan=3><em>none yet</em></td></tr>"}</table>
      <h3>Negative controls</h3>
      <table><tr><th>Query</th><th>Lit</th><th>Dark</th><th>Pass</th></tr>
      ${r.negative_controls.map((n) => `<tr><td>${esc(n.query)}</td><td>${n.lit}</td><td>${n.dark}</td><td>${n.passed ? "✓" : "✗"}</td></tr>`).join("") || "<tr><td colspan=4><em>none yet</em></td></tr>"}</table>
      <p class="note">${SCORE_NOTE}</p>`;
  } catch (err) {
    el.innerHTML = `<p class="error">Could not load /eval: ${esc((err as Error).message)}</p>`;
  }
}

// ----------------------------------------------------------------- chrome
function status(msg: string, error = false) {
  const el = $("#status");
  el.textContent = msg;
  el.classList.toggle("error", error);
  el.hidden = !msg;
}

$("#ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = ($("#question") as HTMLInputElement).value.trim();
  if (text) void ask(text);
});

document.querySelectorAll<HTMLButtonElement>(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    const results = b.dataset.tab === "results";
    $("#results").hidden = !results;
    if (results) void showResults();
  }),
);

// TODO(Person C): live layers toggle (ships, earthquakes, weather). Atmosphere only, never scoring.

// The header grows when preset chips wrap; keep the status line and panels just below it.
const syncTop = () =>
  document.documentElement.style.setProperty("--top-h", `${Math.ceil($("#top").getBoundingClientRect().bottom)}px`);
new ResizeObserver(syncTop).observe($("#top"));
window.addEventListener("resize", syncTop);
syncTop();

async function init() {
  try {
    void assetsFor("mine");
    const presets = await api.presets();
    $("#presets").innerHTML = presets
      .map((p) => `<button class="chip ${p.kind}" data-q="${esc(p.query)}" title="${esc(p.query)}">${esc(p.label)}</button>`)
      .join("");
    $("#presets").querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
      b.addEventListener("click", () => void ask(b.dataset.q!)),
    );
    // No query runs on load: presets are suggestions, any question can be typed.
  } catch (err) {
    status(`Backend not reachable (${(err as Error).message}). Is \`make backend\` running?`, true);
  }
}

// Dev-only hook for headless QA (select a mine by id without clicking a pixel).
if (import.meta.env.DEV) {
  (window as unknown as { __globe: unknown }).__globe = {
    viewer: globe.viewer,
    selectById: (id: string) => {
      const r = current?.results.find((x) => x.id === id);
      if (r) void select(r);
    },
  };
}

void init();
