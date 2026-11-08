import * as Cesium from 'cesium';

/**
 * Researcher's Globe drawing on God's Eye View's Cesium viewer.
 * Ported from frontend/src/globe.ts (PR #17): lit mines coloured by tier with
 * the high tier pulsing, unlit mines as small grey dots, a ring on the selected
 * mine, lifted geodesic arcs mine -> port -> destination country with labels,
 * hover tooltip and click-to-select.
 */

export const TIER_COLOR = { high: '#ff4d4f', medium: '#ffa940', low: '#ffe7a3' };
const POINT_HEIGHT = 5000; // m above the ellipsoid, above the terrain under every mine
const NO_DEPTH = Number.POSITIVE_INFINITY;
// GEV renders at full device resolution: point sizes are drawing-buffer pixels, so scale them.
const K = Math.max(1, Math.min(2, (globalThis.devicePixelRatio || 1) * 0.8));
const css = (c, a = 1) => Cesium.Color.fromCssColorString(c).withAlpha(a);

export function createRgGlobe(viewer, { onSelect, describe }) {
  const scene = viewer.scene;
  const root = new Cesium.PrimitiveCollection();
  scene.primitives.add(root);
  const dark = root.add(new Cesium.PointPrimitiveCollection());
  const lit = root.add(new Cesium.PointPrimitiveCollection());
  const ring = root.add(new Cesium.PointPrimitiveCollection());
  const arcs = root.add(new Cesium.PolylineCollection());
  const labels = root.add(new Cesium.LabelCollection());
  const at = (lon, lat, h = POINT_HEIGHT) => Cesium.Cartesian3.fromDegrees(lon, lat, h);
  let high = [];
  let pulseTimer = null;

  // Points and labels ignore depth (the globe never clips them) and are hidden
  // behind the horizon with an ellipsoid occluder instead. High-tier points pulse.
  const occluder = new Cesium.EllipsoidalOccluder(Cesium.Ellipsoid.WGS84, Cesium.Cartesian3.ZERO);
  const removePreRender = scene.preRender.addEventListener(() => {
    occluder.cameraPosition = viewer.camera.positionWC;
    for (const c of [dark, lit, ring])
      for (let i = 0; i < c.length; i++) {
        const p = c.get(i);
        p.show = occluder.isPointVisible(p.position);
      }
    for (let i = 0; i < labels.length; i++) {
      const l = labels.get(i);
      l.show = occluder.isPointVisible(l.position);
    }
    const t = performance.now() / 1000;
    for (const p of high) p.pixelSize = K * (12 + 6 * (0.5 + 0.5 * Math.sin(t * 3)));
  });
  const render = () => scene.requestRender();

  // GEV may run in request-render mode: keep frames coming while something pulses.
  function setPulsing(on) {
    if (on && !pulseTimer) pulseTimer = setInterval(render, 50);
    if (!on && pulseTimer) {
      clearInterval(pulseTimer);
      pulseTimer = null;
    }
  }

  // Hover tooltip + click to select.
  const tip = document.createElement('div');
  tip.className = 'rg-tip';
  tip.hidden = true;
  document.body.appendChild(tip);
  const pickAsset = (pos) => {
    const picked = scene.pick(pos);
    return picked?.id?.kind === 'rg-asset' ? picked.id : null;
  };
  const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  let lastMove = 0;
  handler.setInputAction((m) => {
    const now = performance.now();
    if (now - lastMove < 60) return;
    lastMove = now;
    const a = pickAsset(m.endPosition);
    if (a) scene.canvas.style.cursor = 'pointer';
    else if (!tip.hidden) scene.canvas.style.cursor = '';
    tip.hidden = !a;
    if (a) {
      const r = scene.canvas.getBoundingClientRect();
      tip.innerHTML = describe(a);
      tip.style.transform = `translate(${r.left + m.endPosition.x + 14}px, ${r.top + m.endPosition.y + 10}px)`;
    }
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
  handler.setInputAction((c) => {
    const a = pickAsset(c.position);
    if (a) onSelect(a.id);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  /** Points along the geodesic from a to b, lifted into an arc so long hops read as routes. */
  function arcPositions(a, b) {
    const geo = new Cesium.EllipsoidGeodesic(
      Cesium.Cartographic.fromDegrees(a.lon, a.lat),
      Cesium.Cartographic.fromDegrees(b.lon, b.lat),
    );
    const lift = Math.min(1_200_000, geo.surfaceDistance * 0.12);
    const out = [];
    for (let i = 0; i <= 64; i++) {
      const c = geo.interpolateUsingFraction(i / 64);
      out.push(
        Cesium.Cartesian3.fromRadians(c.longitude, c.latitude, POINT_HEIGHT + lift * Math.sin((Math.PI * i) / 64)),
      );
    }
    return out;
  }

  /** flyTo that retries once if something else (GEV's own camera restore) cancels it. */
  let flight = 0;
  function fly(options, retries = 1, gen = ++flight) {
    viewer.camera.flyTo({
      ...options,
      cancel: () => {
        // Only retry if no newer flight of ours replaced this one.
        if (retries > 0 && gen === flight) setTimeout(() => gen === flight && fly(options, retries - 1, gen), 900);
      },
    });
  }

  return {
    setVisible(on) {
      root.show = on;
      render();
    },
    setDark(points) {
      dark.removeAll();
      for (const a of points)
        dark.add({
          position: at(a.lon, a.lat),
          pixelSize: 5 * K,
          color: css('#c9d1dc', 0.8),
          outlineColor: css('#000000', 0.6),
          outlineWidth: 1,
          disableDepthTestDistance: NO_DEPTH,
        });
      render();
    },
    setResults(results, rank) {
      lit.removeAll();
      high = [];
      for (const r of results) {
        const p = lit.add({
          position: at(r.lon, r.lat),
          pixelSize: K * (r.tier === 'high' ? 12 : r.tier === 'medium' ? 10 : 8),
          color: css(TIER_COLOR[r.tier]),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 1,
          disableDepthTestDistance: NO_DEPTH,
          id: { kind: 'rg-asset', id: r.id, name: r.name, tier: r.tier, score: r.score, rank: rank.get(r.id) ?? 0 },
        });
        if (r.tier === 'high') high.push(p);
      }
      setPulsing(high.length > 0);
      render();
    },
    setSelected(r) {
      ring.removeAll();
      if (r)
        ring.add({
          position: at(r.lon, r.lat, POINT_HEIGHT + 10),
          pixelSize: 22 * K,
          color: Cesium.Color.TRANSPARENT,
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 3 * K,
          disableDepthTestDistance: NO_DEPTH,
        });
      render();
    },
    setPath(list, nodes) {
      arcs.removeAll();
      labels.removeAll();
      for (const { from, to } of list)
        arcs.add({
          positions: arcPositions(from, to),
          width: 4,
          material: Cesium.Material.fromType('PolylineOutline', {
            color: css('#4da3ff'),
            outlineColor: css('#06142b', 0.9),
            outlineWidth: 1.5,
          }),
        });
      for (const n of nodes)
        labels.add({
          position: at(n.lon, n.lat),
          text: `${n.type === 'port' ? '⚓ ' : n.type === 'country' ? '→ ' : ''}${n.name}`,
          font: '600 13px Inter, system-ui, sans-serif',
          fillColor: css('#e6ecf5'),
          showBackground: true,
          backgroundColor: css('#0e1422', 0.85),
          backgroundPadding: new Cesium.Cartesian2(7, 4),
          pixelOffset: new Cesium.Cartesian2(12, 0),
          horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          disableDepthTestDistance: NO_DEPTH,
        });
      render();
    },
    /** Fly so the points are in view; shift west of the right-hand detail panel when it is open. */
    fitTo(points, { panelOpen = false, minSpanDeg = 6 } = {}) {
      if (!points.length) return;
      const vs = points.map(([lon, lat]) =>
        Cesium.Cartesian3.normalize(Cesium.Cartesian3.fromDegrees(lon, lat, 0), new Cesium.Cartesian3()),
      );
      const sum = vs.reduce((acc, v) => Cesium.Cartesian3.add(acc, v, acc), new Cesium.Cartesian3());
      const centre = Cesium.Cartesian3.magnitude(sum) > 1e-6 ? Cesium.Cartesian3.normalize(sum, sum) : vs[0];
      const radius = Math.max(...vs.map((v) => Math.acos(Math.min(1, Cesium.Cartesian3.dot(v, centre)))));
      const span = Math.max(minSpanDeg, Cesium.Math.toDegrees(2 * radius));
      const c = Cesium.Cartographic.fromCartesian(
        Cesium.Cartesian3.multiplyByScalar(centre, 6_371_000, new Cesium.Cartesian3()),
      );
      const lat = Cesium.Math.toDegrees(c.latitude);
      const lon = Cesium.Math.toDegrees(c.longitude);
      // The HUD panel sits on the left: shift the view west so points land right of it,
      // and east again when the detail panel on the right is open.
      const k = span > 110 ? 0.1 : 0.25;
      const cos = Math.max(0.25, Math.cos(Cesium.Math.toRadians(lat)));
      const lonShift = ((panelOpen ? 0.1 : -1) * span * k) / cos;
      fly({
        destination: Cesium.Cartesian3.fromDegrees(
          lon + lonShift,
          Math.max(-85, Math.min(85, lat)),
          Math.min(22_000_000, span * 111_000 * 1.7),
        ),
        orientation: { heading: 0, pitch: -Cesium.Math.PI_OVER_TWO, roll: 0 },
        duration: 1.4,
      });
    },
    home() {
      fly({
        destination: Cesium.Cartesian3.fromDegrees(10, 20, 22_000_000),
        orientation: { heading: 0, pitch: -Cesium.Math.PI_OVER_TWO, roll: 0 },
        duration: 1.2,
      });
    },
    destroy() {
      setPulsing(false);
      removePreRender();
      handler.destroy();
      tip.remove();
      scene.primitives.remove(root);
    },
  };
}
