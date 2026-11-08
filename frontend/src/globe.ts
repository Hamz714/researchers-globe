// CesiumJS globe (replaces the MapLibre view). Owner: Person C.
// Imagery: keyless Esri World Imagery. If VITE_GOOGLE_MAPS_API_KEY is set, Google Photorealistic
// 3D Tiles are streamed on top (heavier: keep it off on weak venue Wi-Fi).
import * as Cesium from "cesium";
import type { AssetPoint, Result, Tier } from "./types";

(window as unknown as { CESIUM_BASE_URL: string }).CESIUM_BASE_URL = "/cesium/";

export const TIER_COLOR: Record<Tier, string> = { high: "#ff4d4f", medium: "#ffa940", low: "#ffe7a3" };
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const POINT_HEIGHT = 5000; // m above the ellipsoid, above the terrain under every mine
const css = (c: string, a = 1) => Cesium.Color.fromCssColorString(c).withAlpha(a);

export type Located = { id: string; name: string; type: string; lat: number; lon: number };
export type Arc = { from: Located; to: Located };
type AssetPick = { kind: "asset"; id: string; name: string; tier: Tier; score: number; rank: number };

export interface Globe {
  setDark(points: AssetPoint[]): void;
  setResults(results: Result[], rank: Map<string, number>): void;
  setSelected(r: Result | null): void;
  setPath(arcs: Arc[], labels: Located[]): void;
  fitTo(points: [number, number][], opts: { panelOpen: boolean; minSpanDeg?: number }): void;
  home(): void;
  viewer: Cesium.Viewer;
}

export function createGlobe(container: string, onSelect: (id: string) => void, describe: (p: AssetPick) => string): Globe {
  const viewer = new Cesium.Viewer(container, {
    baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({ url: ESRI, maximumLevel: 18, credit: "Imagery: Esri, Maxar, Earthstar Geographics" }), { brightness: 0.8 }),
    animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false,
    sceneModePicker: false, navigationHelpButton: false, fullscreenButton: false, infoBox: false,
    selectionIndicator: false, requestRenderMode: true, maximumRenderTimeChange: Infinity,
  });
  const scene = viewer.scene;
  scene.backgroundColor = Cesium.Color.BLACK;
  scene.globe.showGroundAtmosphere = true;
  if (scene.skyAtmosphere) scene.skyAtmosphere.show = true;

  const googleKey = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;
  if (googleKey) {
    Cesium.createGooglePhotorealistic3DTileset({ key: googleKey })
      .then((tiles) => { scene.primitives.add(tiles); scene.globe.show = false; scene.requestRender(); })
      .catch((e) => console.warn("Google 3D tiles unavailable, staying on Esri imagery", e));
  }

  const dark = scene.primitives.add(new Cesium.PointPrimitiveCollection());
  const lit = scene.primitives.add(new Cesium.PointPrimitiveCollection());
  const ring = scene.primitives.add(new Cesium.PointPrimitiveCollection());
  const arcs = scene.primitives.add(new Cesium.PolylineCollection());
  const labels = scene.primitives.add(new Cesium.LabelCollection());
  const at = (lon: number, lat: number, h = POINT_HEIGHT) => Cesium.Cartesian3.fromDegrees(lon, lat, h);
  let high: Cesium.PointPrimitive[] = [];

  // Points and labels ignore depth (so the globe never clips them) and are hidden behind the
  // horizon with an ellipsoid occluder instead. High-tier points pulse.
  const occluder = new Cesium.EllipsoidalOccluder(Cesium.Ellipsoid.WGS84, Cesium.Cartesian3.ZERO);
  scene.preRender.addEventListener(() => {
    occluder.cameraPosition = viewer.camera.positionWC;
    for (const c of [dark, lit, ring]) for (let i = 0; i < c.length; i++) { const p = c.get(i); p.show = occluder.isPointVisible(p.position); }
    for (let i = 0; i < labels.length; i++) { const l = labels.get(i); l.show = occluder.isPointVisible(l.position); }
    const t = performance.now() / 1000;
    for (const p of high) p.pixelSize = 12 + 6 * (0.5 + 0.5 * Math.sin(t * 3));
  });
  const NO_DEPTH = Number.POSITIVE_INFINITY;

  // Hover tooltip + click to select.
  const tip = document.createElement("div");
  tip.className = "globe-tip";
  tip.hidden = true;
  viewer.container.appendChild(tip);
  const pickAsset = (pos: Cesium.Cartesian2): AssetPick | null => {
    const picked = scene.pick(pos) as { id?: AssetPick } | undefined;
    return picked?.id?.kind === "asset" ? picked.id : null;
  };
  const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction((m: { endPosition: Cesium.Cartesian2 }) => {
    const a = pickAsset(m.endPosition);
    scene.canvas.style.cursor = a ? "pointer" : "";
    tip.hidden = !a;
    if (a) {
      tip.innerHTML = describe(a);
      tip.style.transform = `translate(${m.endPosition.x + 14}px, ${m.endPosition.y + 10}px)`;
    }
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
  handler.setInputAction((c: { position: Cesium.Cartesian2 }) => {
    const a = pickAsset(c.position);
    if (a) onSelect(a.id);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  /** Points along the geodesic from a to b, lifted into an arc so long hops read as routes. */
  function arcPositions(a: Located, b: Located): Cesium.Cartesian3[] {
    const geo = new Cesium.EllipsoidGeodesic(Cesium.Cartographic.fromDegrees(a.lon, a.lat), Cesium.Cartographic.fromDegrees(b.lon, b.lat));
    const lift = Math.min(1_200_000, geo.surfaceDistance * 0.12);
    const out: Cesium.Cartesian3[] = [];
    for (let i = 0; i <= 64; i++) {
      const c = geo.interpolateUsingFraction(i / 64);
      out.push(Cesium.Cartesian3.fromRadians(c.longitude, c.latitude, POINT_HEIGHT + lift * Math.sin((Math.PI * i) / 64)));
    }
    return out;
  }

  const globe: Globe = {
    viewer,
    setDark(points) {
      dark.removeAll();
      for (const a of points) dark.add({ position: at(a.lon, a.lat), pixelSize: 4, color: css("#9aa3b2", 0.55), disableDepthTestDistance: NO_DEPTH });
      scene.requestRender();
    },
    setResults(results, rank) {
      lit.removeAll();
      high = [];
      for (const r of results) {
        const p = lit.add({
          position: at(r.lon, r.lat),
          pixelSize: r.tier === "high" ? 12 : r.tier === "medium" ? 10 : 8,
          color: css(TIER_COLOR[r.tier]),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 1,
          disableDepthTestDistance: NO_DEPTH,
          id: { kind: "asset", id: r.id, name: r.name, tier: r.tier, score: r.score, rank: rank.get(r.id) ?? 0 } satisfies AssetPick,
        });
        if (r.tier === "high") high.push(p);
      }
      scene.requestRenderMode = high.length === 0;
      scene.requestRender();
    },
    setSelected(r) {
      ring.removeAll();
      if (r) ring.add({ position: at(r.lon, r.lat, POINT_HEIGHT + 10), pixelSize: 22, color: Cesium.Color.TRANSPARENT, outlineColor: Cesium.Color.WHITE, outlineWidth: 3, disableDepthTestDistance: NO_DEPTH });
      scene.requestRender();
    },
    setPath(list, nodes) {
      arcs.removeAll();
      labels.removeAll();
      for (const { from, to } of list) {
        arcs.add({ positions: arcPositions(from, to), width: 4, material: Cesium.Material.fromType("PolylineOutline", { color: css("#4da3ff"), outlineColor: css("#06142b", 0.9), outlineWidth: 1.5 }) });
      }
      for (const n of nodes) {
        labels.add({
          position: at(n.lon, n.lat), text: `${n.type === "port" ? "⚓ " : n.type === "country" ? "→ " : ""}${n.name}`,
          font: "600 13px Inter, system-ui, sans-serif", fillColor: css("#e6ecf5"), showBackground: true,
          backgroundColor: css("#0e1422", 0.85), backgroundPadding: new Cesium.Cartesian2(7, 4),
          pixelOffset: new Cesium.Cartesian2(12, 0), horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          verticalOrigin: Cesium.VerticalOrigin.CENTER, disableDepthTestDistance: NO_DEPTH,
        });
      }
      scene.requestRender();
    },
    fitTo(points, { panelOpen, minSpanDeg = 6 }) {
      if (!points.length) return;
      // Centre = normalised mean of the points' unit vectors (works across the antimeridian and the poles).
      const vs = points.map(([lon, lat]) => Cesium.Cartesian3.normalize(Cesium.Cartesian3.fromDegrees(lon, lat, 0), new Cesium.Cartesian3()));
      const sum = vs.reduce((acc, v) => Cesium.Cartesian3.add(acc, v, acc), new Cesium.Cartesian3());
      const centre = Cesium.Cartesian3.magnitude(sum) > 1e-6 ? Cesium.Cartesian3.normalize(sum, sum) : vs[0];
      const radius = Math.max(...vs.map((v) => Math.acos(Math.min(1, Cesium.Cartesian3.dot(v, centre)))));
      const span = Math.max(minSpanDeg, Cesium.Math.toDegrees(2 * radius));
      const c = Cesium.Cartographic.fromCartesian(Cesium.Cartesian3.multiplyByScalar(centre, 6_371_000, new Cesium.Cartesian3()));
      const lat = Cesium.Math.toDegrees(c.latitude), lon = Cesium.Math.toDegrees(c.longitude);
      // Shift east for the side panel and north for the header, so the points sit in the open area.
      const k = span > 110 ? 0.12 : 0.32;
      const lonShift = panelOpen ? (span * k) / Math.max(0.25, Math.cos(Cesium.Math.toRadians(lat))) : 0;
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(lon + lonShift, Math.max(-85, Math.min(85, lat + span * 0.06)), Math.min(22_000_000, span * 111_000 * 1.6)),
        duration: 1.4,
      });
    },
    home() {
      viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(10, 20, 22_000_000), duration: 1.2 });
    },
  };
  viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(10, 20, 22_000_000) });
  return globe;
}
