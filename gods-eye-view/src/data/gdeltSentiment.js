/**
 * GDELT 2.0 news-sentiment aggregation: pure functions shared by the
 * `/api/sentiment` provider and its tests. No I/O, no browser or Node globals.
 *
 * Input rows are the tab-separated 15-minute event exports
 * (https://data.gdeltproject.org/gdeltv2/<YYYYMMDDHHMMSS>.export.CSV.zip).
 * Each event carries AvgTone (the tone of the articles that mention it, roughly
 * -10 very negative to +10 very positive; most news sits between -5 and +2),
 * NumArticles and an ActionGeo point. Events are assigned to an admin-1 region
 * by point-in-polygon against the bundled Natural Earth boundaries, so no GDELT
 * FIPS country/region code mapping is needed.
 */

export const GDELT_COLUMNS = Object.freeze({
  numArticles: 33,
  avgTone: 34,
  geoType: 51, // 1 country, 2 US state, 3 US city, 4 world city, 5 world ADM1
  lat: 56,
  lon: 57,
  url: 60,
});
const MIN_COLUMNS = 61;
const PRECISE_GEO_TYPES = new Set(['2', '3', '4', '5']);

/** Decode one packed Natural Earth ring (integers in 10^-d degrees, deltas). */
export function decodeRing(encoded, decimals) {
  const factor = 10 ** decimals;
  const ring = [];
  let x = 0;
  let y = 0;
  for (let i = 0; i + 1 < encoded.length; i += 2) {
    x += encoded[i];
    y += encoded[i + 1];
    ring.push([x / factor, y / factor]);
  }
  return ring;
}

function pointInRing(ring, lat, lon) {
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
}

function polygonsContain(polygons, lat, lon) {
  for (const [outer, ...holes] of polygons) {
    if (!pointInRing(outer, lat, lon)) continue;
    if (!holes.some((hole) => pointInRing(hole, lat, lon))) return true;
  }
  return false;
}

/** Countries that have at least two admin-1 regions in the bundled boundaries. */
export function listCountries(boundaries) {
  const byIso = new Map();
  for (const feature of boundaries.features) {
    if (!feature.iso2 || !feature.country) continue;
    const entry = byIso.get(feature.iso2) ?? {
      iso2: feature.iso2,
      name: feature.country,
      regions: 0,
    };
    entry.regions += 1;
    byIso.set(feature.iso2, entry);
  }
  return [...byIso.values()]
    .filter((c) => c.regions >= 2)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Regions of one country with decoded polygons, bounding boxes and a memoised
 * point locator. Parts crossing the antimeridian are kept as stored (rare at
 * admin-1 and only affects a handful of Pacific units).
 */
export function buildRegionIndex(boundaries, iso2) {
  const defaultDecimals = boundaries.meta?.decimals ?? 3;
  const regions = [];
  for (const feature of boundaries.features) {
    if (feature.iso2 !== iso2 || !Array.isArray(feature.polygons)) continue;
    const decimals = feature.d ?? defaultDecimals;
    const polygons = feature.polygons.map((poly) =>
      poly.map((ring) => decodeRing(ring, decimals)),
    );
    let west = Infinity,
      south = Infinity,
      east = -Infinity,
      north = -Infinity;
    for (const [outer] of polygons)
      for (const [x, y] of outer) {
        if (x < west) west = x;
        if (x > east) east = x;
        if (y < south) south = y;
        if (y > north) north = y;
      }
    regions.push({
      id: feature.iso || `${iso2}-${regions.length}`,
      name: feature.nameEn || feature.name,
      label: feature.label ?? [(west + east) / 2, (south + north) / 2],
      bbox: [west, south, east, north],
      polygons,
    });
  }
  if (!regions.length) return null;
  const bbox = regions.reduce(
    (b, r) => [
      Math.min(b[0], r.bbox[0]),
      Math.min(b[1], r.bbox[1]),
      Math.max(b[2], r.bbox[2]),
      Math.max(b[3], r.bbox[3]),
    ],
    [Infinity, Infinity, -Infinity, -Infinity],
  );
  const memo = new Map();
  function locate(lat, lon) {
    if (lon < bbox[0] || lon > bbox[2] || lat < bbox[1] || lat > bbox[3])
      return -1;
    const key = `${lat.toFixed(3)},${lon.toFixed(3)}`;
    const hit = memo.get(key);
    if (hit !== undefined) return hit;
    let found = -1;
    for (let i = 0; i < regions.length; i += 1) {
      const [w, s, e, n] = regions[i].bbox;
      if (lon < w || lon > e || lat < s || lat > n) continue;
      if (polygonsContain(regions[i].polygons, lat, lon)) {
        found = i;
        break;
      }
    }
    if (memo.size < 200_000) memo.set(key, found);
    return found;
  }
  return { iso2, regions, bbox, locate };
}

/**
 * Topic matcher over article URLs. Commas separate alternatives; words inside an
 * alternative must all appear. "election, vote" matches either word;
 * "sardine price" needs both. Blank topic matches everything (returns null).
 */
export function topicMatcher(topic) {
  const alternatives = String(topic ?? '')
    .toLowerCase()
    .split(',')
    .map((alt) =>
      alt
        .split(/\s+/)
        .map((w) => w.replace(/[^\p{L}\p{N}]/gu, ''))
        .filter((w) => w.length >= 2),
    )
    .filter((words) => words.length);
  if (!alternatives.length) return null;
  return (url) => {
    const u = url.toLowerCase();
    return alternatives.some((words) => words.every((w) => u.includes(w)));
  };
}

/** Article slug for display: "theguardian.com › some-headline-words". */
export function describeUrl(url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.replace(/^www\./, '');
    const segments = parsed.pathname.split('/').filter(Boolean);
    const readable = (seg) => {
      let text = seg;
      try {
        text = decodeURIComponent(seg);
      } catch {
        /* keep raw */
      }
      text = text.replace(/\.(html?|php|aspx?)$/i, '').replace(/[-_+]+/g, ' ');
      const words = text.split(/\s+/).filter((w) => /^[\p{L}']{3,}$/u.test(w));
      return words.length >= 3
        ? text
            .replace(/\b\w*\d\w*\b/g, '')
            .replace(/\s+/g, ' ')
            .trim()
        : '';
    };
    const title = [...segments].reverse().map(readable).find(Boolean) ?? '';
    return { host, title: title.slice(0, 110) };
  } catch {
    return { host: '', title: '' };
  }
}

/**
 * Accumulate one export file's rows into per-day, per-region sums.
 * `state` comes from createAccumulator; `dayIndex` is the file's day slot.
 */
export function accumulateExport(state, text, dayIndex, index, matches) {
  const C = GDELT_COLUMNS;
  const day = state.days[dayIndex];
  let start = 0;
  while (start < text.length) {
    let end = text.indexOf('\n', start);
    if (end === -1) end = text.length;
    const line = text.slice(start, end);
    start = end + 1;
    if (!line) continue;
    const f = line.split('\t');
    if (f.length < MIN_COLUMNS || !PRECISE_GEO_TYPES.has(f[C.geoType]))
      continue;
    const lat = Number(f[C.lat]);
    const lon = Number(f[C.lon]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const region = index.locate(lat, lon);
    if (region < 0) continue;
    const url = f[C.url] || '';
    if (matches && !matches(url)) continue;
    const tone = Number(f[C.avgTone]);
    if (!Number.isFinite(tone)) continue;
    const articles = Math.max(1, Number(f[C.numArticles]) || 1);
    const cell = day.regions[region];
    cell.weight += articles;
    cell.toneSum += tone * articles;
    cell.events += 1;
    day.weight += articles;
    day.toneSum += tone * articles;
    day.events += 1;
    if (url.startsWith('http')) {
      const story = day.stories.get(url);
      if (story) {
        story.articles += articles;
        story.toneSum += tone * articles;
        story.weight += articles;
      } else if (day.stories.size < 20_000)
        day.stories.set(url, {
          url,
          region,
          articles,
          toneSum: tone * articles,
          weight: articles,
        });
    }
  }
}

export function createAccumulator(dayKeys, regionCount) {
  return {
    days: dayKeys.map((key) => ({
      key,
      weight: 0,
      toneSum: 0,
      events: 0,
      files: 0,
      stories: new Map(),
      regions: Array.from({ length: regionCount }, () => ({
        weight: 0,
        toneSum: 0,
        events: 0,
      })),
    })),
  };
}

const round = (v, d = 2) =>
  v == null || !Number.isFinite(v) ? null : Math.round(v * 10 ** d) / 10 ** d;

/**
 * Turn sums into the API payload. Per region and day: article-weighted tone,
 * article weight and the change against the region's own window average,
 * shrunk toward zero when the region has few articles that day.
 */
export function finalizeAccumulator(
  state,
  index,
  { shrinkArticles = 40, minArticles = 8 } = {},
) {
  const regions = index.regions.map((region, r) => {
    let weight = 0;
    let toneSum = 0;
    for (const day of state.days) {
      weight += day.regions[r].weight;
      toneSum += day.regions[r].toneSum;
    }
    const windowTone = weight ? toneSum / weight : null;
    const values = state.days.map((day) => {
      const cell = day.regions[r];
      if (!cell.weight) return null;
      const tone = cell.toneSum / cell.weight;
      const shrink = cell.weight / (cell.weight + shrinkArticles);
      return {
        tone: round(tone),
        articles: cell.weight,
        events: cell.events,
        change: windowTone == null ? null : round((tone - windowTone) * shrink),
        thin: cell.weight < minArticles,
      };
    });
    return {
      id: region.id,
      name: region.name,
      label: region.label,
      windowTone: round(windowTone),
      articles: weight,
      values,
    };
  });
  let weight = 0;
  let toneSum = 0;
  for (const day of state.days) {
    weight += day.weight;
    toneSum += day.toneSum;
  }
  const days = state.days.map((day) => {
    const stories = [...day.stories.values()].sort(
      (a, b) => b.articles - a.articles,
    );
    const pick = (list) =>
      list.slice(0, 3).map((s) => ({
        url: s.url,
        ...describeUrl(s.url),
        region: index.regions[s.region]?.name ?? null,
        tone: round(s.toneSum / s.weight),
        articles: s.articles,
      }));
    const covered = stories.filter((s) => s.articles >= 3);
    return {
      date: day.key,
      tone: day.weight ? round(day.toneSum / day.weight) : null,
      articles: day.weight,
      events: day.events,
      files: day.files,
      topStories: pick(stories),
      mostNegative: pick(
        [...covered].sort(
          (a, b) => a.toneSum / a.weight - b.toneSum / b.weight,
        ),
      ),
    };
  });
  return { windowTone: weight ? round(toneSum / weight) : null, days, regions };
}

/** Region outlines for the client, rounded to 3 decimals (~100 m). */
export function regionGeometry(index) {
  return index.regions.map((region) => ({
    id: region.id,
    polygons: region.polygons.map((poly) =>
      poly.map((ring) => ring.map(([x, y]) => [round(x, 3), round(y, 3)])),
    ),
  }));
}

/** 15-minute export timestamps (YYYYMMDDHHMMSS, UTC) for one UTC day. */
export function dayTimestamps(isoDay) {
  const base = Date.parse(`${isoDay}T00:00:00Z`);
  const out = [];
  for (let i = 0; i < 96; i += 1)
    out.push(
      new Date(base + i * 900_000)
        .toISOString()
        .replace(/[-:T]/g, '')
        .slice(0, 14),
    );
  return out;
}
