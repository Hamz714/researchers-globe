import * as Cesium from 'cesium';

/**
 * Region fill + outline for the sentiment explorer. Fills are ground
 * primitives (draped on terrain and 3D tiles) whose per-instance colours are
 * updated in place when the day changes. Both primitives skip the pick pass so
 * they never steal clicks from other layers (see src/layers/gnss/index.js).
 */
class RenderOnlyGroundPrimitive extends Cesium.GroundPrimitive {
  update(frameState) {
    if (!frameState.passes.pick) super.update(frameState);
  }
}
class RenderOnlyGroundPolylinePrimitive extends Cesium.GroundPolylinePrimitive {
  update(frameState) {
    if (!frameState.passes.pick) super.update(frameState);
  }
}

const NEUTRAL = [150, 160, 175];
const NEGATIVE = [236, 64, 58];
const POSITIVE = [56, 140, 255];
const NO_DATA = Cesium.Color.fromBytes(90, 96, 110, 40);

/** Diverging colour: value in tone points, saturating at ±scale. */
export function sentimentColor(value, scale = 1) {
  if (value == null || !Number.isFinite(value)) return NO_DATA;
  const t = Math.max(-1, Math.min(1, value / scale));
  const target = t < 0 ? NEGATIVE : POSITIVE;
  const k = Math.abs(t);
  const mix = NEUTRAL.map((c, i) => Math.round(c + (target[i] - c) * k));
  return Cesium.Color.fromBytes(
    mix[0],
    mix[1],
    mix[2],
    Math.round(70 + 120 * k),
  );
}

export function createSentimentRenderer(viewer) {
  let fill = null;
  let outline = null;
  let instanceIds = []; // per region: list of instance ids
  let pendingColors = null;
  let removeListener = null;

  function applyColors() {
    if (!fill || !pendingColors || !fill.ready) return false;
    pendingColors.forEach((color, r) => {
      for (const id of instanceIds[r] ?? []) {
        const attrs = fill.getGeometryInstanceAttributes(id);
        if (attrs)
          attrs.color = Cesium.ColorGeometryInstanceAttribute.toValue(
            color,
            attrs.color,
          );
      }
    });
    pendingColors = null;
    viewer.scene.requestRender();
    return true;
  }

  function clear() {
    removeListener?.();
    removeListener = null;
    if (fill) viewer.scene.primitives.remove(fill);
    if (outline) viewer.scene.primitives.remove(outline);
    fill = outline = null;
    instanceIds = [];
    pendingColors = null;
    viewer.scene.requestRender();
  }

  function setGeometry(geometry) {
    clear();
    const fills = [];
    const lines = [];
    geometry.forEach((region, r) => {
      instanceIds[r] = [];
      region.polygons.forEach(([outer, ...holes], p) => {
        if (outer.length < 3) return;
        const id = `sentiment:${r}:${p}`;
        instanceIds[r].push(id);
        const toCartesian = (ring) =>
          Cesium.Cartesian3.fromDegreesArray(ring.flat());
        fills.push(
          new Cesium.GeometryInstance({
            id,
            geometry: new Cesium.PolygonGeometry({
              polygonHierarchy: new Cesium.PolygonHierarchy(
                toCartesian(outer),
                holes
                  .filter((h) => h.length >= 3)
                  .map((h) => new Cesium.PolygonHierarchy(toCartesian(h))),
              ),
              vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT,
            }),
            attributes: {
              color: Cesium.ColorGeometryInstanceAttribute.fromColor(NO_DATA),
            },
          }),
        );
        lines.push(
          new Cesium.GeometryInstance({
            geometry: new Cesium.GroundPolylineGeometry({
              positions: toCartesian(outer),
              loop: true,
              width: 1.5,
            }),
            attributes: {
              color: Cesium.ColorGeometryInstanceAttribute.fromColor(
                Cesium.Color.WHITE.withAlpha(0.45),
              ),
            },
          }),
        );
      });
    });
    fill = viewer.scene.primitives.add(
      new RenderOnlyGroundPrimitive({
        geometryInstances: fills,
        appearance: new Cesium.PerInstanceColorAppearance({
          flat: true,
          translucent: true,
        }),
        classificationType: Cesium.ClassificationType.BOTH,
        allowPicking: false,
      }),
    );
    outline = viewer.scene.primitives.add(
      new RenderOnlyGroundPolylinePrimitive({
        geometryInstances: lines,
        appearance: new Cesium.PolylineColorAppearance(),
        classificationType: Cesium.ClassificationType.BOTH,
        allowPicking: false,
      }),
    );
    // Ground primitives build asynchronously; colours apply once ready.
    removeListener = viewer.scene.postRender.addEventListener(() => {
      if (pendingColors && applyColors()) viewer.scene.requestRender();
      if (fill?.ready && !pendingColors) {
        removeListener?.();
        removeListener = null;
      }
    });
    viewer.scene.requestRender();
  }

  function setColors(colors) {
    pendingColors = colors;
    if (!applyColors() && !removeListener && fill) {
      removeListener = viewer.scene.postRender.addEventListener(() => {
        if (applyColors()) {
          removeListener?.();
          removeListener = null;
        }
      });
    }
    viewer.scene.requestRender();
  }

  function setVisible(visible) {
    if (fill) fill.show = visible;
    if (outline) outline.show = visible;
    viewer.scene.requestRender();
  }

  function flyTo(geometry, labels) {
    // Frame by region label points, ignoring far-flung territories (Alaska,
    // Hawaii, overseas departments) so the main landmass fills the view.
    let pts = labels.filter((p) => Array.isArray(p) && p.length === 2);
    if (!pts.length) return;
    const median = (a) =>
      [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)];
    const mx = median(pts.map((p) => p[0]));
    const my = median(pts.map((p) => p[1]));
    const madX = median(pts.map((p) => Math.abs(p[0] - mx))) || 1;
    const madY = median(pts.map((p) => Math.abs(p[1] - my))) || 1;
    const core = pts.filter(
      (p) =>
        Math.abs(p[0] - mx) <= 3 * madX + 2 &&
        Math.abs(p[1] - my) <= 3 * madY + 2,
    );
    if (core.length >= Math.max(2, pts.length * 0.6)) pts = core;
    let [w, s, e, n] = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [x, y] of pts) {
      w = Math.min(w, x);
      e = Math.max(e, x);
      s = Math.min(s, y);
      n = Math.max(n, y);
    }
    const width = Math.max(1, e - w);
    const height = Math.max(1, n - s);
    // Extra room on the east so the country sits left of the panel.
    viewer.camera.flyTo({
      destination: Cesium.Rectangle.fromDegrees(
        Math.max(-180, w - width * 0.12 - 1),
        Math.max(-89, s - height * 0.15 - 1),
        Math.min(180, e + width * 0.7 + 1),
        Math.min(89, n + height * 0.15 + 1),
      ),
      duration: 2,
    });
  }

  /** Lon/lat under a canvas position, or null when the cursor is off the globe. */
  function lonLatAt(windowPosition) {
    const cartesian = viewer.camera.pickEllipsoid(
      windowPosition,
      viewer.scene.globe.ellipsoid,
    );
    if (!cartesian) return null;
    const c = Cesium.Cartographic.fromCartesian(cartesian);
    return {
      lon: Cesium.Math.toDegrees(c.longitude),
      lat: Cesium.Math.toDegrees(c.latitude),
    };
  }

  return {
    setGeometry,
    setColors,
    setVisible,
    flyTo,
    lonLatAt,
    clear,
    destroy: clear,
  };
}
