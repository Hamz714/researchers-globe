import * as Cesium from 'cesium';
import './sentiment.css';
import {
  fetchSentimentCountries,
  locateRegion,
  regionValue,
  runSentimentSearch,
} from './source.js';
import { createSentimentRenderer, sentimentColor } from './rendering.js';

/**
 * News-sentiment explorer: a search panel (topic, country, centre date) that
 * asks /api/sentiment for GDELT tone by region and day, colours the regions on
 * the globe and steps through the days with play/pause.
 */

const MODES = {
  time: {
    label: 'vs own average',
    help: 'Each region compared with its own average over the whole window: shows how coverage changed day to day.',
    scale: 1,
  },
  place: {
    label: 'vs national',
    help: 'Each region compared with the whole country that day: shows where coverage was more negative.',
    scale: 1.5,
  },
};
const PLAY_MS = 1800;

/** Colour scale = 85th percentile of |value| across the window, in 0.25 steps. */
function autoScale(result, mode) {
  const values = [];
  result.regions.forEach((_, i) =>
    result.days.forEach((__, d) => {
      const v = regionValue(result, i, d, mode);
      if (v != null) values.push(Math.abs(v));
    }),
  );
  if (!values.length) return 1;
  values.sort((a, b) => a - b);
  const p85 = values[Math.floor(0.85 * (values.length - 1))];
  return Math.max(0.25, Math.round(p85 * 4) / 4);
}

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null)
      node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
};
const svg = (tag, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};
const fmtTone = (v) =>
  v == null
    ? '–'
    : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}`;
const fmtInt = (v) => (v == null ? '–' : Math.round(v).toLocaleString('en-GB'));
const dayLabel = (
  iso,
  opts = { weekday: 'short', day: 'numeric', month: 'short' },
) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    ...opts,
    timeZone: 'UTC',
  });
const relLabel = (iso, center) => {
  const k = Math.round(
    (Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${center}T00:00:00Z`)) /
      86_400_000,
  );
  if (k === 0) return 'Centre day';
  return `${Math.abs(k)} day${Math.abs(k) > 1 ? 's' : ''} ${k < 0 ? 'before' : 'after'}`;
};
const FULL_DATE = Object.freeze({
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});
const SHORT_DAY = Object.freeze({ weekday: 'short', day: 'numeric' });
const safeHref = (url) => (/^https?:\/\//i.test(url) ? url : null);

export function mountSentimentExplorer({
  viewer,
  root = document.body,
  fetchImpl = (...a) => fetch(...a),
}) {
  const renderer = createSentimentRenderer(viewer);
  const state = {
    scales: { time: 1, place: 1 },
    result: null,
    day: 0,
    mode: 'time',
    playing: false,
    timer: null,
    search: null,
    hover: -1,
  };

  // ---------- skeleton ----------
  const toggle = el('button', {
    class: 'gev-sent-toggle',
    type: 'button',
    'aria-expanded': 'false',
    text: 'NEWS SENTIMENT',
  });
  const topic = el('input', {
    class: 'gev-sent-input',
    type: 'text',
    maxlength: '120',
    placeholder: 'election, vote   (blank = all news)',
    value: 'election',
  });
  const country = el(
    'select',
    { class: 'gev-sent-input' },
    el('option', { value: 'US', text: 'United States of America' }),
  );
  const center = el('input', {
    class: 'gev-sent-input',
    type: 'date',
    value: '2026-11-03',
    min: '2015-02-19',
    max: new Date().toISOString().slice(0, 10),
  });
  const span = el(
    'select',
    { class: 'gev-sent-input' },
    ...[1, 2, 3, 5, 7].map((d) =>
      el('option', {
        value: String(d),
        text: `±${d} day${d > 1 ? 's' : ''} (${2 * d + 1} days)`,
        selected: d === 3,
      }),
    ),
  );
  const go = el('button', {
    class: 'gev-sent-go',
    type: 'submit',
    text: 'Search',
  });
  const form = el(
    'form',
    { class: 'gev-sent-form' },
    el('label', {}, el('span', { text: 'Topic in article address' }), topic),
    el(
      'div',
      { class: 'gev-sent-row' },
      el('label', {}, el('span', { text: 'Country' }), country),
      el('label', {}, el('span', { text: 'Centre date' }), center),
    ),
    el(
      'div',
      { class: 'gev-sent-row' },
      el('label', {}, el('span', { text: 'Window' }), span),
      go,
    ),
  );
  const status = el('div', { class: 'gev-sent-status', role: 'status' });
  const results = el('div', { class: 'gev-sent-results', hidden: true });
  const body = el('div', { class: 'gev-sent-body' }, form, status, results);
  const close = el('button', {
    class: 'gev-sent-close',
    type: 'button',
    'aria-label': 'Close news sentiment',
    text: '×',
  });
  const panel = el(
    'section',
    {
      class: 'gev-sent-panel',
      hidden: true,
      'aria-label': 'News sentiment explorer',
    },
    el(
      'header',
      { class: 'gev-sent-head' },
      el(
        'div',
        {},
        el('strong', { text: 'NEWS SENTIMENT' }),
        el('small', { text: 'GDELT 2.0 · tone of coverage by region' }),
      ),
      close,
    ),
    body,
  );
  root.append(toggle, panel);

  // ---------- results skeleton ----------
  const title = el('div', { class: 'gev-sent-title' });
  const dateBig = el('div', { class: 'gev-sent-date' });
  const dateRel = el('div', { class: 'gev-sent-rel' });
  const kpiTone = el('div', { class: 'gev-sent-kpi-value' });
  const kpiNote = el('div', { class: 'gev-sent-kpi-note' });
  const chart = svg('svg', {
    class: 'gev-sent-chart',
    viewBox: '0 0 340 120',
    role: 'img',
  });
  const play = el('button', {
    class: 'gev-sent-play',
    type: 'button',
    text: '▶ Play',
  });
  const slider = el('input', {
    class: 'gev-sent-slider',
    type: 'range',
    min: '0',
    max: '0',
    value: '0',
    'aria-label': 'Day',
  });
  const modeButtons = Object.entries(MODES).map(([key, m]) =>
    el('button', {
      type: 'button',
      class: 'gev-sent-mode',
      'data-mode': key,
      text: m.label,
      onclick: () => setMode(key),
    }),
  );
  const legend = el('div', { class: 'gev-sent-legend' });
  const hover = el('div', {
    class: 'gev-sent-hover',
    text: 'Hover a region for its numbers.',
  });
  const lists = el('div', { class: 'gev-sent-lists' });
  const stories = el('div', { class: 'gev-sent-stories' });
  const foot = el('p', { class: 'gev-sent-foot' });
  results.append(
    title,
    el(
      'div',
      { class: 'gev-sent-daybox' },
      el('div', {}, dateBig, dateRel),
      el('div', { class: 'gev-sent-kpi' }, kpiTone, kpiNote),
    ),
    chart,
    el('div', { class: 'gev-sent-controls' }, play, slider),
    el(
      'div',
      { class: 'gev-sent-modes' },
      el('span', { text: 'Colour' }),
      ...modeButtons,
    ),
    legend,
    hover,
    lists,
    stories,
    foot,
  );

  // ---------- behaviour ----------
  function setOpen(open) {
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.classList.toggle('is-open', open);
    renderer.setVisible(open);
    if (!open) stop();
  }
  toggle.addEventListener('click', () => setOpen(panel.hidden));
  close.addEventListener('click', () => setOpen(false));

  fetchSentimentCountries({ fetchImpl })
    .then((list) => {
      country.replaceChildren(
        ...list.map((c) =>
          el('option', {
            value: c.iso2,
            text: c.name,
            selected: c.iso2 === 'US',
          }),
        ),
      );
    })
    .catch(() => {
      status.textContent =
        'Sentiment service unavailable: restart `npm run dev` after pulling this feature.';
    });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    stop();
    state.search?.abort();
    const controller = new AbortController();
    state.search = controller;
    go.disabled = true;
    status.className = 'gev-sent-status';
    status.textContent = 'Starting search…';
    try {
      const result = await runSentimentSearch(
        {
          country: country.value,
          topic: topic.value.trim(),
          center: center.value,
          days: Number(span.value),
        },
        {
          fetchImpl,
          signal: controller.signal,
          onProgress: ({ done, total }) => {
            status.textContent = total
              ? `Reading GDELT news files ${done.toLocaleString('en-GB')} / ${total.toLocaleString('en-GB')}${done < total * 0.98 ? ' (a new week takes ~20–30 s; repeats are instant)' : ''}`
              : 'Preparing…';
          },
        },
      );
      if (controller.signal.aborted) return;
      showResult(result);
      status.textContent = '';
    } catch (error) {
      if (controller.signal.aborted) return;
      status.className = 'gev-sent-status is-error';
      status.textContent = error?.message || 'Search failed';
    } finally {
      if (state.search === controller) go.disabled = false;
    }
  });

  function showResult(result) {
    state.result = result;
    state.scales = {
      time: autoScale(result, 'time'),
      place: autoScale(result, 'place'),
    };
    state.day = Math.max(
      0,
      result.days.findIndex((d) => d.date === result.query.center),
    );
    renderer.setGeometry(result.geometry);
    renderer.setVisible(!panel.hidden);
    renderer.flyTo(
      result.geometry,
      result.regions.map((r) => r.label),
    );
    slider.max = String(result.days.length - 1);
    const q = result.query;
    title.textContent = `${q.topic ? `“${q.topic}”` : 'All news'} · ${q.countryName}`;
    const missing = result.coverage.expectedFiles - result.coverage.files;
    foot.textContent = `${result.source.note} Source: ${result.source.name}.${missing ? ` ${missing} of ${result.coverage.expectedFiles} 15-minute files were unavailable.` : ''}`;
    results.hidden = false;
    renderDay();
  }

  function setMode(mode) {
    state.mode = mode;
    if (state.result) renderDay();
  }

  function renderDay() {
    const r = state.result;
    if (!r) return;
    const d = state.day;
    const day = r.days[d];
    slider.value = String(d);
    const dateText = dayLabel(day.date, FULL_DATE);
    dateBig.textContent = dateText;
    dateRel.textContent = `${relLabel(day.date, r.query.center)} · day ${d + 1} of ${r.days.length}`;
    dateRel.classList.toggle('is-centre', day.date === r.query.center);
    kpiTone.textContent = fmtTone(day.tone);
    const delta =
      day.tone != null && r.windowTone != null ? day.tone - r.windowTone : null;
    kpiNote.textContent = `tone · ${fmtInt(day.articles)} articles · ${delta == null ? '' : `${fmtTone(delta)} vs window avg`}`;
    for (const b of modeButtons)
      b.classList.toggle('is-active', b.dataset.mode === state.mode);

    // Map colours
    const mode = MODES[state.mode];
    const values = r.regions.map((_, i) => regionValue(r, i, d, state.mode));
    renderer.setColors(
      values.map((v) => sentimentColor(v, state.scales[state.mode])),
    );

    // Legend
    legend.replaceChildren(
      el('div', { class: 'gev-sent-legend-bar' }),
      el(
        'div',
        { class: 'gev-sent-legend-labels' },
        el('span', { text: `more negative (−${state.scales[state.mode]})` }),
        el('span', { text: 'same' }),
        el('span', { text: `more positive (+${state.scales[state.mode]})` }),
      ),
      el('div', {
        class: 'gev-sent-legend-help',
        text: `${mode.help} Grey: too few articles.`,
      }),
    );

    // Region lists
    const ranked = values
      .map((v, i) => ({ v, i }))
      .filter((x) => x.v != null)
      .sort((a, b) => a.v - b.v);
    const item = ({ v, i }) => {
      const cell = r.regions[i].values[d];
      return el(
        'li',
        {},
        el('span', { text: r.regions[i].name }),
        el('b', { text: fmtTone(v), class: v < 0 ? 'neg' : 'pos' }),
        el('small', { text: `${fmtInt(cell.articles)} art.` }),
      );
    };
    lists.replaceChildren(
      el(
        'div',
        {},
        el('h4', { text: 'Most negative' }),
        el('ol', {}, ...ranked.slice(0, 3).map(item)),
      ),
      el(
        'div',
        {},
        el('h4', { text: 'Most positive' }),
        el('ol', {}, ...ranked.slice(-3).reverse().map(item)),
      ),
    );

    // Stories
    const story = (s) => {
      const href = safeHref(s.url);
      const label = s.title ? `${s.title}` : s.host;
      return el(
        'li',
        {},
        href
          ? el('a', {
              href,
              target: '_blank',
              rel: 'noopener noreferrer',
              text: label,
            })
          : el('span', { text: label }),
        el('small', {
          text: `${s.host}${s.region ? ` · ${s.region}` : ''} · tone ${fmtTone(s.tone)} · ${s.articles} art.`,
        }),
      );
    };
    stories.replaceChildren(
      el('h4', { text: 'Most covered stories that day' }),
      el('ul', {}, ...day.topStories.map(story)),
      ...(day.mostNegative.length
        ? [
            el('h4', { text: 'Most negative widely covered stories' }),
            el('ul', {}, ...day.mostNegative.map(story)),
          ]
        : []),
    );

    drawChart();
    updateHover();
  }

  function drawChart() {
    const r = state.result;
    const n = r.days.length;
    const tones = r.days.map((x) => x.tone).filter((v) => v != null);
    const lo = Math.min(...tones, r.windowTone ?? 0) - 0.15;
    const hi = Math.max(...tones, r.windowTone ?? 0) + 0.15;
    const maxArticles = Math.max(1, ...r.days.map((x) => x.articles));
    const W = 340,
      top = 14,
      H = 72,
      left = 10,
      step = (W - 2 * left) / n;
    const y = (v) => top + ((hi - v) / (hi - lo || 1)) * H;
    chart.replaceChildren();
    chart.setAttribute(
      'aria-label',
      `Daily tone: ${r.days.map((x) => `${x.date} ${fmtTone(x.tone)}`).join(', ')}`,
    );
    r.days.forEach((x, i) => {
      const cx = left + step * (i + 0.5);
      const bh = (x.articles / maxArticles) * 34;
      const g = svg('g', {
        class: `gev-sent-col${i === state.day ? ' is-current' : ''}${x.date === r.query.center ? ' is-centre' : ''}`,
      });
      g.append(
        svg('rect', {
          x: cx - step / 2 + 2,
          y: 0,
          width: step - 4,
          height: 120,
          class: 'hit',
        }),
      );
      g.append(
        svg('rect', {
          x: cx - step * 0.3,
          y: top + H + 2 - bh + 2,
          width: step * 0.6,
          height: bh,
          class: 'vol',
        }),
      );
      const lab = svg('text', {
        x: cx,
        y: 116,
        'text-anchor': 'middle',
        class: 'day',
      });
      const dayText = dayLabel(x.date, SHORT_DAY).replace(',', '');
      lab.textContent = dayText;
      g.append(lab);
      g.addEventListener('click', () => {
        stop();
        state.day = i;
        renderDay();
      });
      chart.append(g);
    });
    if (r.windowTone != null) {
      chart.append(
        svg('line', {
          x1: left,
          x2: W - left,
          y1: y(r.windowTone),
          y2: y(r.windowTone),
          class: 'avg',
        }),
      );
      const t = svg('text', {
        x: W - left,
        y: y(r.windowTone) - 3,
        'text-anchor': 'end',
        class: 'avg-label',
      });
      t.textContent = `window avg ${fmtTone(r.windowTone)}`;
      chart.append(t);
    }
    const pts = r.days
      .map((x, i) =>
        x.tone == null ? null : [left + step * (i + 0.5), y(x.tone)],
      )
      .filter(Boolean);
    chart.append(
      svg('polyline', {
        points: pts.map((p) => p.join(',')).join(' '),
        class: 'line',
      }),
    );
    r.days.forEach((x, i) => {
      if (x.tone == null) return;
      const cx = left + step * (i + 0.5);
      chart.append(
        svg('circle', {
          cx,
          cy: y(x.tone),
          r: i === state.day ? 5 : 3,
          class: i === state.day ? 'pt is-current' : 'pt',
        }),
      );
      if (i === state.day) {
        const t = svg('text', {
          x: cx,
          y: y(x.tone) - 9,
          'text-anchor': 'middle',
          class: 'val',
        });
        t.textContent = fmtTone(x.tone);
        chart.append(t);
      }
    });
  }

  // ---------- playback ----------
  function stop() {
    state.playing = false;
    clearInterval(state.timer);
    state.timer = null;
    play.textContent = '▶ Play';
  }
  play.addEventListener('click', () => {
    if (!state.result) return;
    if (state.playing) return stop();
    state.playing = true;
    play.textContent = '❚❚ Pause';
    if (state.day >= state.result.days.length - 1) {
      state.day = 0;
      renderDay();
    }
    state.timer = setInterval(() => {
      if (state.day >= state.result.days.length - 1) return stop();
      state.day += 1;
      renderDay();
    }, PLAY_MS);
  });
  slider.addEventListener('input', () => {
    stop();
    state.day = Number(slider.value);
    renderDay();
  });

  // ---------- hover read-out ----------
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  let lastMove = 0;
  handler.setInputAction((movement) => {
    if (!state.result || panel.hidden) return;
    const t = performance.now();
    if (t - lastMove < 80) return;
    lastMove = t;
    const ll = renderer.lonLatAt(movement.endPosition);
    const idx = ll ? locateRegion(state.result.geometry, ll.lat, ll.lon) : -1;
    if (idx !== state.hover) {
      state.hover = idx;
      updateHover();
    }
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  function updateHover() {
    const r = state.result;
    if (!r || state.hover < 0) {
      hover.textContent = 'Hover a region for its numbers.';
      return;
    }
    const region = r.regions[state.hover];
    const cell = region.values[state.day];
    if (!cell) {
      hover.textContent = `${region.name}: no located articles this day.`;
      return;
    }
    const value = regionValue(r, state.hover, state.day, state.mode);
    hover.replaceChildren(
      el('strong', { text: region.name }),
      el('span', {
        text: ` tone ${fmtTone(cell.tone)} · ${fmtInt(cell.articles)} articles · ${MODES[state.mode].label}: ${value == null ? 'too few articles' : fmtTone(value)}`,
      }),
    );
  }

  setOpen(false);
  return {
    open: () => setOpen(true),
    destroy() {
      stop();
      state.search?.abort();
      handler.destroy();
      renderer.destroy();
      toggle.remove();
      panel.remove();
    },
  };
}
