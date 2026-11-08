import maplibregl, { type GeoJSONSource, type Map as MLMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { api } from "./api";
import type { AssetDetail, AssetPoint, EvalReport, QueryResult, Result, Tier } from "./types";

// ----------------------------------------------------------------- constants
// TODO(Person C): demotiles is a free, low-detail basemap with no key. Swap for a nicer style if time allows.
const STYLE_URL = "https://demotiles.maplibre.org/style.json";
const TIER_COLOR: Record<Tier, string> = { high: "#ff4d4f", medium: "#ffa940", low: "#ffe7a3" };
const TIER_LABEL: Record<Tier, string> = { high: "High", medium: "Medium", low: "Low" };

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const fmt = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));

let current: QueryResult | null = null;
let allAssets: AssetPoint[] = [];

// ----------------------------------------------------------------- map
const map: MLMap = new maplibregl.Map({
  container: "map",
  style: STYLE_URL,
  center: [10, 20],
  zoom: 1.4,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

const emptyFC = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });

map.on("style.load", () => {
  map.setProjection({ type: "globe" });

  map.addSource("dark", { type: "geojson", data: emptyFC() });
  map.addSource("assets", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "dark",
    type: "circle",
    source: "dark",
    paint: { "circle-radius": 2.2, "circle-color": "#6b7280", "circle-opacity": 0.6 },
  });
  map.addSource("path", { type: "geojson", data: emptyFC() });

  map.addLayer({
    id: "path-line",
    type: "line",
    source: "path",
    paint: { "line-color": "#69b1ff", "line-width": 2, "line-dasharray": [2, 1] },
  });
  map.addLayer({
    id: "path-nodes",
    type: "circle",
    source: "path",
    filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": 4, "circle-color": "#69b1ff", "circle-stroke-color": "#fff", "circle-stroke-width": 1 },
  });
  map.addLayer({
    id: "assets-pulse",
    type: "circle",
    source: "assets",
    filter: ["==", ["get", "tier"], "high"],
    paint: { "circle-radius": 10, "circle-color": TIER_COLOR.high, "circle-opacity": 0.35 },
  });
  map.addLayer({
    id: "assets",
    type: "circle",
    source: "assets",
    paint: {
      "circle-radius": ["match", ["get", "tier"], "high", 7, "medium", 6, 4],
      "circle-color": ["match", ["get", "tier"], "high", TIER_COLOR.high, "medium", TIER_COLOR.medium, TIER_COLOR.low],
      "circle-opacity": ["match", ["get", "tier"], "low", 0.55, 1],
      "circle-stroke-color": "#111",
      "circle-stroke-width": 1,
    },
  });

  map.on("click", "assets", (e) => {
    const id = e.features?.[0]?.properties?.id as string | undefined;
    const r = current?.results.find((x) => x.id === id);
    if (r) void select(r);
  });
  map.on("mouseenter", "assets", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "assets", () => (map.getCanvas().style.cursor = ""));

  pulse();
  if (current) render(current);
});

function pulse() {
  const t = performance.now() / 1000;
  if (map.getLayer("assets-pulse")) {
    map.setPaintProperty("assets-pulse", "circle-radius", 9 + 6 * (0.5 + 0.5 * Math.sin(t * 3)));
  }
  requestAnimationFrame(pulse);
}

// ----------------------------------------------------------------- query flow
async function ask(text: string) {
  status(`Asking: ${text}`);
  ($("#question") as HTMLInputElement).value = text;
  try {
    current = await api.query(text);
    render(current);
    status("");
  } catch (err) {
    status(`Query failed: ${(err as Error).message}`, true);
  }
}

function render(q: QueryResult) {
  const src = map.getSource("assets") as GeoJSONSource | undefined;
  src?.setData({
    type: "FeatureCollection",
    features: q.results.map((r) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [r.lon, r.lat] },
      properties: { id: r.id, name: r.name, tier: r.tier, score: r.score },
    })),
  });
  const lit = new Set(q.results.map((r) => r.id));
  (map.getSource("dark") as GeoJSONSource | undefined)?.setData({
    type: "FeatureCollection",
    features: allAssets
      .filter((a) => !lit.has(a.id))
      .map((a) => ({ type: "Feature", geometry: { type: "Point", coordinates: [a.lon, a.lat] }, properties: { id: a.id } })),
  });
  (map.getSource("path") as GeoJSONSource | undefined)?.setData(emptyFC());
  $("#panel").hidden = true;

  const counts = { high: 0, medium: 0, low: 0 } as Record<Tier, number>;
  q.results.forEach((r) => counts[r.tier]++);
  $("#legend").innerHTML = `
    <div class="legend-title">${esc(q.criteria.event.name)}</div>
    <div class="channels">${q.channels.map((c) => `<span class="chip small" title="${esc(c.reason)}">${esc(c.id)}</span>`).join("") || "<em>no channels chosen</em>"}</div>
    ${(["high", "medium", "low"] as Tier[])
      .map((t) => `<div><span class="dot" style="background:${TIER_COLOR[t]}"></span>${TIER_LABEL[t]} <b>${counts[t]}</b></div>`)
      .join("")}
    <div><span class="dot dark"></span>Dark <b>${q.dark_count}</b></div>
    <div class="hint">Scores rank exposure plus current evidence. They are not probabilities.</div>`;
}

// ----------------------------------------------------------------- side panel
async function select(r: Result) {
  const panel = $("#panel");
  panel.hidden = false;
  panel.innerHTML = panelHtml(r, null);
  map.flyTo({ center: [r.lon, r.lat], zoom: Math.max(map.getZoom(), 2.5) });
  try {
    const detail = await api.asset(r.id);
    panel.innerHTML = panelHtml(r, detail);
    drawPath(detail);
  } catch {
    /* detail is optional: the contract fields are already shown */
  }
}

function panelHtml(r: Result, d: AssetDetail | null): string {
  const c = r.components;
  const names = new Map((d?.path_nodes ?? []).map((n) => [n.id, n.name]));
  const pathWords = r.paths
    .map((p) => `<li>${p.map((id) => esc(names.get(id) ?? id)).join(" → ")}</li>`)
    .join("");
  return `
    <button class="close" aria-label="Close" onclick="this.parentElement.hidden=true">×</button>
    <h2>${esc(r.name)}</h2>
    <div class="tier" style="background:${TIER_COLOR[r.tier]}">${TIER_LABEL[r.tier]} · score ${fmt(r.score)}</div>
    <table class="components">
      <tr><td>Exposure</td><td>${fmt(c.exposure)}</td></tr>
      <tr><td>News volume z</td><td>${fmt(c.news_z, 1)}</td></tr>
      <tr><td>Tone shift</td><td>${fmt(c.tone_shift, 1)}</td></tr>
      <tr><td>Attention z</td><td>${fmt(c.attention_z, 1)}</td></tr>
      <tr><td>Relevant articles</td><td>${c.relevant_articles}</td></tr>
    </table>
    <h3>Why</h3><p>${esc(r.reason)}</p>
    <h3>Exposure paths</h3><ol class="paths">${pathWords}</ol>
    ${d?.series ? `<h3>News volume, last ${d.series.dates.length} days</h3>${sparkline(d.series.news_volume, d.series.recent_days)}` : ""}
    <h3>Evidence</h3>
    ${r.evidence.length ? `<ul class="evidence">${r.evidence
      .map((e) => `<li><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a><br><small>${esc(e.date)} · ${esc(e.judge)} (${fmt(e.confidence)})</small></li>`)
      .join("")}</ul>` : "<p><em>No relevant articles.</em></p>"}`;
}

function sparkline(values: number[], recentDays: number): string {
  if (!values.length) return "";
  const w = 300, h = 60, max = Math.max(...values, 1);
  const x = (i: number) => (i / Math.max(values.length - 1, 1)) * w;
  const y = (v: number) => h - (v / max) * (h - 4) - 2;
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const recentX = x(values.length - recentDays);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <rect x="${recentX}" y="0" width="${w - recentX}" height="${h}" class="recent"/>
    <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}

function drawPath(d: AssetDetail) {
  const byId = new Map(d.path_nodes.map((n) => [n.id, n]));
  const features: GeoJSON.Feature[] = [];
  for (const p of d.paths) {
    const coords = p
      .map((id) => byId.get(id))
      .filter((n): n is NonNullable<typeof n> => !!n && n.lat !== null && n.lon !== null)
      .map((n) => [n.lon!, n.lat!]);
    if (coords.length >= 2) features.push({ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} });
  }
  for (const n of d.path_nodes) {
    if (n.lat !== null && n.lon !== null && n.type !== "mine") {
      features.push({ type: "Feature", geometry: { type: "Point", coordinates: [n.lon, n.lat] }, properties: { name: n.name } });
    }
  }
  (map.getSource("path") as GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features });
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
      ${r.negative_controls.map((n) => `<tr><td>${esc(n.query)}</td><td>${n.lit}</td><td>${n.dark}</td><td>${n.passed ? "✓" : "✗"}</td></tr>`).join("") || "<tr><td colspan=4><em>none yet</em></td></tr>"}</table>`;
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

async function init() {
  try {
    allAssets = await api.assets().catch(() => []);
    const presets = await api.presets();
    $("#presets").innerHTML = presets
      .map((p) => `<button class="chip ${p.kind}" data-q="${esc(p.query)}">${esc(p.label)}</button>`)
      .join("");
    $("#presets").querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
      b.addEventListener("click", () => void ask(b.dataset.q!)),
    );
    const hero = presets.find((p) => p.kind === "hero");
    if (hero) await ask(hero.query);
  } catch (err) {
    status(`Backend not reachable (${(err as Error).message}). Is \`make backend\` running?`, true);
  }
}

void init();
