# God's Eye View add-on: GDELT news sentiment and protest timeline

Work done on top of [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view)
(the open-source Cesium 3D globe) at upstream commit `52187c9`. This folder holds only what we
added or changed, laid out at the same paths as the gods-eye-view repo, so it can be dropped on top
of a checkout. It does not touch the Researcher's Globe backend or frontend.

## What it adds

1. **News sentiment search** (main feature). A **NEWS SENTIMENT** button in the app opens a panel:
   type a topic (blank = all news), pick a country (any of 212 with admin-1 regions) and a centre
   date, choose ±1 to ±7 days. The server downloads the GDELT 2.0 event exports for that window,
   keeps events located inside the country's regions whose article URL matches the topic, and
   returns article-weighted tone per region per day. The globe colours each region red (more
   negative) or blue (more positive), either **vs the region's own window average** (change over
   time) or **vs the national tone that day** (where it is worse). The panel shows the date, the
   day's tone and article count, a 7-day tone chart, play/pause, the most negative/positive
   regions, and the most covered and most negative stories with links.
   - A week already cached loads in ~2 s; a new week takes ~20–30 s (files cached in `.gev-cache/gdelt`).
   - Example: "election", US, 3 Nov ±3. Election coverage gets less negative through the week
     (−1.40 on Sat 31 Oct to −0.52 on Thu 5 Nov) and slips back to −1.00 on Fri 6 Nov.
2. **GDELT protest pipeline** (`tools/gdelt-us-unrest.mjs`). Downloads the 15-minute GDELT event
   files for a date range and writes protest/riot events (CAMEO 14x) per US state per day, with
   election-linked counts. The output for 6 Oct – 7 Nov 2026 is in `output/gdelt-us-unrest/`
   (3,164 of 3,164 files; `events.geojson` has every event with its source link).
3. **7-day protest timeline** (Director scene). A ~28 s animation: one still camera over the US,
   one frame per day from Sat 31 Oct to Fri 6 Nov 2026, election-linked protest reports per state,
   with the date, national count, top states, a 7-day bar chart and a legend on screen
   (60 / 23 / 92 / **120 on election day** / 75 / 30 / 46; October average 50/day).
   Frames are built by `tools/build_unrest_week.py`.

## Apply to a gods-eye-view checkout

```bash
git clone https://github.com/bilawalsidhu/gods-eye-view.git && cd gods-eye-view
git checkout 52187c9            # the commit this was built and tested on
cp -R ../researchers-globe/gods-eye-view/{server,src,tools,public,output} .
git apply ../researchers-globe/gods-eye-view/gods-eye-view-integration.patch
npm ci && npm run dev           # Node 24.x or 26.x; open http://localhost:4173
```

The patch is two small edits: `server/providers/local.js` registers the `/api/sentiment` endpoint,
and `src/main.js` mounts the panel once the globe is ready.

- **Sentiment:** click **NEWS SENTIMENT** (top right), then Search.
- **Protest timeline:** Authoring → IMPORT →
  `public/scene-assets/us-midterms-2026-week/us-midterms-2026-week.scene.json` → Apply → Play.
- **Refresh the protest data:** `node tools/gdelt-us-unrest.mjs --from 2026-10-06 --to 2026-11-07`,
  then `python3 tools/build_unrest_week.py --election 2026-11-03 --days-each-side 3` (needs Pillow).

## Files

| Path | What |
| --- | --- |
| `server/providers/sentiment.js` | `GET /api/sentiment/countries`, `GET /api/sentiment/search` (job + poll) |
| `src/data/gdeltSentiment.js` (+ `.test.mjs`) | Pure parsing, region lookup, topic matching, aggregation; 5 unit tests |
| `src/layers/sentiment/` | Panel, Cesium ground-primitive rendering, API client, styles |
| `tools/gdelt-us-unrest.mjs` | GDELT event downloader/aggregator (no dependencies) |
| `tools/build_unrest_week.py` | Renders the 7-day timeline frames and the Director scene file |
| `public/scene-assets/us-midterms-2026-week/` | Rendered frames + scene JSON (checksums included) |
| `output/gdelt-us-unrest/` | Protest data 6 Oct – 7 Nov 2026 |
| `gods-eye-view-integration.patch` | The two edits to existing gods-eye-view files |

## Checks run

Prettier, `scripts/check-import-directions.mjs`, the icon/label/module-boundary tests and the new
unit tests pass. The feature was run end to end in the app (US election week; a cold
Morocco search downloaded and processed 288 files in 8 s) with no console errors. Run the full
`npm test` on Node 24 before relying on it upstream.

## Caveats

- GDELT tone is a dictionary score of article wording: it measures how negative the **coverage**
  is, not public opinion. Most news averages below zero, hence the relative colouring.
- Topic matching uses words in the article URL. Swapping it for the Jev relevance judge
  (`reasoning/judge.py`) is the natural next step.
- Protest counts are machine-coded news reports, not verified incidents. GDELT's DOC search API
  had a gap from 26 Oct to 5 Nov 2026; the raw event files used here do not.

Data: [GDELT Project](https://www.gdeltproject.org/) (free use with citation and link);
boundaries from Natural Earth (public domain), bundled in gods-eye-view.
