import test from 'node:test';
import assert from 'node:assert/strict';
import {
  accumulateExport,
  buildRegionIndex,
  createAccumulator,
  dayTimestamps,
  describeUrl,
  finalizeAccumulator,
  listCountries,
  topicMatcher,
} from './gdeltSentiment.js';

// Two square "regions" in a fake country, encoded like Natural Earth (3 decimals, deltas).
const square = (x, y) => [[x * 1000, y * 1000, 1000, 0, 0, 1000, -1000, 0]];
const BOUNDARIES = {
  meta: { decimals: 3 },
  features: [
    {
      iso2: 'ZZ',
      country: 'Testland',
      iso: 'ZZ-A',
      name: 'West',
      polygons: [square(0, 0)],
    },
    {
      iso2: 'ZZ',
      country: 'Testland',
      iso: 'ZZ-B',
      name: 'East',
      polygons: [square(1, 0)],
    },
    {
      iso2: 'YY',
      country: 'Oneland',
      iso: 'YY-A',
      name: 'Only',
      polygons: [square(5, 5)],
    },
  ],
};

function row({ tone, articles = 1, type = '4', lat, lon, url }) {
  const f = new Array(61).fill('');
  f[33] = String(articles);
  f[34] = String(tone);
  f[51] = type;
  f[56] = String(lat);
  f[57] = String(lon);
  f[60] = url;
  return f.join('\t');
}

test('countries need at least two regions', () => {
  assert.deepEqual(
    listCountries(BOUNDARIES).map((c) => c.iso2),
    ['ZZ'],
  );
});

test('topic matcher: commas are alternatives, words are all required', () => {
  assert.equal(topicMatcher('  '), null);
  const m = topicMatcher('election, sardine price');
  assert.ok(m('https://x.com/2026/election-night'));
  assert.ok(m('https://x.com/sardine-price-rises'));
  assert.ok(!m('https://x.com/sardine-shortage'));
});

test('events are located by polygon, filtered by topic and article-weighted', () => {
  const index = buildRegionIndex(BOUNDARIES, 'ZZ');
  assert.equal(index.locate(0.5, 0.5), 0);
  assert.equal(index.locate(0.5, 1.5), 1);
  assert.equal(index.locate(0.5, 3.5), -1);
  const state = createAccumulator(['2026-11-03', '2026-11-04'], 2);
  const text = [
    row({
      tone: -4,
      articles: 3,
      lat: 0.5,
      lon: 0.5,
      url: 'https://a.com/election-day',
    }),
    row({
      tone: 2,
      articles: 1,
      lat: 0.5,
      lon: 0.5,
      url: 'https://a.com/election-day',
    }),
    row({
      tone: 9,
      articles: 5,
      lat: 0.5,
      lon: 1.5,
      url: 'https://a.com/sports',
    }), // topic miss
    row({
      tone: -9,
      articles: 5,
      type: '1',
      lat: 0.5,
      lon: 1.5,
      url: 'https://a.com/election',
    }), // country-level
    'short\tline',
  ].join('\n');
  accumulateExport(state, text, 0, index, topicMatcher('election'));
  accumulateExport(
    state,
    row({
      tone: 0,
      articles: 10,
      lat: 0.5,
      lon: 0.5,
      url: 'https://a.com/election',
    }),
    1,
    index,
    null,
  );
  const out = finalizeAccumulator(state, index, {
    shrinkArticles: 0,
    minArticles: 1,
  });
  assert.equal(out.days[0].tone, -2.5); // (-4*3 + 2*1) / 4
  assert.equal(out.days[0].articles, 4);
  assert.equal(out.regions[1].values[0], null);
  assert.equal(out.regions[0].values[0].tone, -2.5);
  // window tone for West: (-12 + 2 + 0) / 14
  assert.equal(out.regions[0].windowTone, -0.71);
  assert.equal(out.regions[0].values[0].change, -1.79);
  assert.equal(out.days[0].topStories[0].url, 'https://a.com/election-day');
});

test('timestamps cover a UTC day in 15-minute steps', () => {
  const ts = dayTimestamps('2026-11-03');
  assert.equal(ts.length, 96);
  assert.equal(ts[0], '20261103000000');
  assert.equal(ts[95], '20261103234500');
});

test('url descriptions skip hash-like slugs', () => {
  assert.deepEqual(
    describeUrl('https://www.example.com/article_9445-4d7d-818a.html'),
    { host: 'example.com', title: '' },
  );
  assert.equal(
    describeUrl('https://x.org/news/2026/eastside-youth-make-concerns-heard')
      .title,
    'eastside youth make concerns heard',
  );
});
