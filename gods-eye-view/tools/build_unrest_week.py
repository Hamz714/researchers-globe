#!/usr/bin/env python3
"""7-day Director scene: how election-linked protest reporting evolved around US election day.

One fixed overhead camera, one frame per day. Each frame is a map image draped over
the US carrying the date, the day's numbers, a 7-day bar chart and a legend.

Inputs : output/gdelt-us-unrest/daily_by_state.csv            (from tools/gdelt-us-unrest.mjs)
         src/data/local_data/natural_earth/states_provinces.json (bundled, public domain)
Output : public/scene-assets/us-midterms-2026-week/  (frames + us-midterms-2026-week.scene.json)

Run from the repo root:   python3 tools/build_unrest_week.py        (needs: pip install pillow)
Options:                  --election 2026-11-03 --days-each-side 3
Play:  Authoring -> IMPORT -> public/scene-assets/us-midterms-2026-week/us-midterms-2026-week.scene.json -> Apply -> Play
"""
import argparse, collections, csv, datetime as dt, hashlib, json, math, os
from PIL import Image, ImageDraw, ImageFont

ap = argparse.ArgumentParser()
ap.add_argument('--root', default='.')
ap.add_argument('--election', default='2026-11-03')
ap.add_argument('--days-each-side', type=int, default=3)
args = ap.parse_args()

ROOT = args.root
SLUG = 'us-midterms-2026-week'
OUT = os.path.join(ROOT, 'public/scene-assets', SLUG)
os.makedirs(OUT, exist_ok=True)

ELECTION = dt.date.fromisoformat(args.election)
DAYS = [ELECTION + dt.timedelta(days=i) for i in range(-args.days_each_side, args.days_each_side + 1)]
DAY_KEYS = [d.isoformat() for d in DAYS]
BASELINE_FROM, BASELINE_TO = '2026-10-06', (DAYS[0] - dt.timedelta(days=1)).isoformat()

# ---------- canvas geometry ----------
# Plate carree bounds of the overlay. Margins over Canada / Mexico hold the panels.
W, E, S, N = -128.0, -62.0, 17.0, 54.0
LAT0 = 35.5                                  # pixels are square on the ground at this latitude
PX_W = 4096
K = PX_W / ((E - W) * math.cos(math.radians(LAT0)))   # px per degree latitude
PX_H = round((N - S) * K)
HEIGHT_M = 5000                               # overlay height above ellipsoid (clears terrain)

def px(lon, lat):
    return ((lon - W) / (E - W) * PX_W, (N - lat) / (N - S) * PX_H)

def stretch_at(lat):
    """Horizontal factor that makes something drawn at `lat` look undistorted on the globe."""
    return math.cos(math.radians(LAT0)) / math.cos(math.radians(lat))

# ---------- fonts ----------
FONT_CANDIDATES = {
    'bold': ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
             '/Library/Fonts/Arial Bold.ttf', 'C:/Windows/Fonts/arialbd.ttf'],
    'regular': ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf',
                '/Library/Fonts/Arial.ttf', 'C:/Windows/Fonts/arial.ttf'],
    'mono': ['/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf', '/System/Library/Fonts/Menlo.ttc',
             'C:/Windows/Fonts/consolab.ttf'],
}
_font_cache = {}
def font(kind, size):
    key = (kind, size)
    if key not in _font_cache:
        for path in FONT_CANDIDATES[kind]:
            if os.path.exists(path):
                _font_cache[key] = ImageFont.truetype(path, size); break
        else:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]

# ---------- colours ----------
INK = (236, 240, 245, 255)
MUTED = (160, 170, 182, 255)
FAINT = (95, 105, 118, 255)
PANEL = (6, 10, 16, 215)
EDGE = (255, 255, 255, 40)
ACCENT = (255, 72, 58, 255)
BINS = [(1, 2, (120, 28, 28)), (3, 5, (175, 34, 34)), (6, 10, (225, 48, 40)),
        (11, 20, (255, 92, 70)), (21, 10**9, (255, 170, 145))]
def bin_colour(n):
    for lo, hi, c in BINS:
        if lo <= n <= hi: return c + (235,)
    return None

# ---------- data ----------
FIPS = {'AL':'Alabama','AZ':'Arizona','AR':'Arkansas','CA':'California','CO':'Colorado','CT':'Connecticut',
 'DE':'Delaware','DC':'Washington DC','FL':'Florida','GA':'Georgia','ID':'Idaho','IL':'Illinois','IN':'Indiana',
 'IA':'Iowa','KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland','MA':'Massachusetts',
 'MI':'Michigan','MN':'Minnesota','MS':'Mississippi','MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada',
 'NH':'New Hampshire','NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina','ND':'North Dakota',
 'OH':'Ohio','OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania','RI':'Rhode Island','SC':'South Carolina',
 'SD':'South Dakota','TN':'Tennessee','TX':'Texas','UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington',
 'WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming','AK':'Alaska','HI':'Hawaii'}

el = collections.defaultdict(int)        # (day, state) -> election-linked protest reports
allp = collections.Counter()             # day -> all protest reports (state-located)
files_ok = {}
for r in csv.DictReader(open(os.path.join(ROOT, 'output/gdelt-us-unrest/daily_by_state.csv'))):
    el[(r['day'], r['state'])] += int(r['protest_election_url'])
    allp[r['day']] += int(r['protest_events'])
    files_ok[r['day']] = int(r['files_ok'])
missing = [d for d in DAY_KEYS if d not in files_ok]
if missing: raise SystemExit(f'No data for {missing}: rerun tools/gdelt-us-unrest.mjs with --from/--to covering them')
thin = [d for d in DAY_KEYS if files_ok[d] < 90]
nat = {d: sum(v for (dd, _), v in el.items() if dd == d) for d in DAY_KEYS}
base_days = sorted({d for d in files_ok if BASELINE_FROM <= d <= BASELINE_TO})
baseline = sum(sum(v for (dd, _), v in el.items() if dd == d) for d in base_days) / max(1, len(base_days))

# ---------- boundaries ----------
ne = json.load(open(os.path.join(ROOT, 'src/data/local_data/natural_earth/states_provinces.json')))
def decode(ring, d):
    f = 10 ** d; x = y = 0; pts = []
    for i in range(0, len(ring) - 1, 2):
        x += ring[i]; y += ring[i + 1]; pts.append((x / f, y / f))
    return pts
shapes, label_pt = {}, {}
for feat in ne['features']:
    code = (feat.get('iso') or '')[3:]
    if feat.get('iso2') != 'US' or code not in FIPS or code in ('AK', 'HI'): continue
    d = feat.get('d', ne['meta']['decimals'])
    shapes[code] = [[decode(r, d) for r in poly] for poly in feat['polygons']]
    label_pt[code] = feat.get('label')
# Nudge labels where the Natural Earth point sits awkwardly for a number.
label_pt.update({'MI': [-84.7, 43.4], 'FL': [-81.6, 28.3], 'LA': [-92.4, 31.0], 'DC': [-77.0, 38.9]})

# ---------- drawing helpers ----------
def text_w(draw, s, f):
    b = draw.textbbox((0, 0), s, font=f); return b[2] - b[0]

def panel_image(w, h):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=26, fill=PANEL, outline=EDGE, width=3)
    return img, d

def paste_at_lat(canvas, img, cx, top, lat):
    """Paste a panel centred at x=cx, compensating the globe's horizontal squeeze at `lat`."""
    f = stretch_at(lat)
    img = img.resize((max(1, round(img.width * f)), img.height), Image.LANCZOS)
    canvas.alpha_composite(img, (round(cx - img.width / 2), round(top)))

def draw_map(canvas, counts):
    d = ImageDraw.Draw(canvas)
    for code, polys in shapes.items():
        fill = bin_colour(counts.get(code, 0)) if counts is not None else None
        for poly in polys:
            outer, holes = poly[0], poly[1:]
            d.polygon([px(*p) for p in outer], fill=fill or (14, 20, 30, 185))
            for h in holes: d.polygon([px(*p) for p in h], fill=(0, 0, 0, 0))
    for polys in shapes.values():
        for poly in polys:
            pts = [px(*p) for p in poly[0]]
            d.line(pts + [pts[0]], fill=(255, 255, 255, 120), width=3)

def draw_state_number(canvas, code, n):
    lon, lat = label_pt[code]
    s = f'{code} {n}'
    f = font('bold', 60)
    tmp = Image.new('RGBA', (360, 110), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    tw = text_w(td, s, f)
    td.text(((360 - tw) / 2, 16), s, font=f, fill=(255, 255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0, 230))
    x, y = px(lon, lat)
    if code == 'DC':                       # DC is a dot: ring it and put the label out over the Atlantic
        dd = ImageDraw.Draw(canvas)
        dd.ellipse((x - 18, y - 18, x + 18, y + 18), fill=bin_colour(max(n, 1)), outline=(255, 255, 255, 255), width=5)
        dd.line((x + 18, y, x + 150, y), fill=(255, 255, 255, 220), width=4)
        x += 290
    paste_at_lat(canvas, tmp, x, y - 55, lat)

def weekday_label(day):
    return day.strftime('%a %-d %b').upper() if os.name != 'nt' else day.strftime('%a %d %b').upper()

def relative_label(day):
    k = (day - ELECTION).days
    if k == 0: return 'ELECTION DAY'
    return f"{abs(k)} DAY{'S' if abs(k) > 1 else ''} {'BEFORE' if k < 0 else 'AFTER'} ELECTION DAY"

# ---------- panels ----------
TOP_LAT = 51.5          # latitude of the title panel's middle (over Canada)
BOTTOM_LAT = 20.5       # latitude of the data panel's middle (over Mexico / Gulf)

def title_panel(day):
    w, h = 1900, 250
    img, d = panel_image(w, h)
    if day is None:
        t1 = 'ELECTION-LINKED PROTEST REPORTS'
        t2 = f"United States · {weekday_label(DAYS[0])} – {weekday_label(DAYS[-1])} {DAYS[-1].year}"
        d.text(((w - text_w(d, t1, font('bold', 84))) / 2, 34), t1, font=font('bold', 84), fill=INK)
        d.text(((w - text_w(d, t2, font('regular', 56))) / 2, 150), t2, font=font('regular', 56), fill=MUTED)
        return img
    t1 = f"{weekday_label(day)} {day.year}"
    t2 = relative_label(day)
    d.text(((w - text_w(d, t1, font('bold', 112))) / 2, 18), t1, font=font('bold', 112), fill=INK)
    pill_f = font('bold', 50)
    pw = text_w(d, t2, pill_f) + 70
    is_e = day == ELECTION
    d.rounded_rectangle(((w - pw) / 2, 160, (w + pw) / 2, 228), radius=34,
                        fill=ACCENT if is_e else (40, 48, 60, 255))
    d.text(((w - pw) / 2 + 35, 166), t2, font=pill_f, fill=(255, 255, 255, 255) if is_e else MUTED)
    return img

def data_panel(day):
    w, h = 2900, 520
    img, d = panel_image(w, h)
    # --- bar chart (left) ---
    cx0, cy0, cw, ch = 60, 80, 1300, 320
    top = max(max(nat.values()), baseline) * 1.3
    d.text((cx0, 18), 'Election-linked protest reports per day, US', font=font('regular', 40), fill=MUTED)
    bw = cw / len(DAYS)
    by = lambda v: cy0 + ch - v / top * ch
    upto = DAYS.index(day) if day else len(DAYS) - 1
    for i, dd in enumerate(DAYS):
        x0, x1 = cx0 + i * bw + 22, cx0 + (i + 1) * bw - 22
        v = nat[dd.isoformat()]
        shown = i <= upto
        if shown:
            col = ACCENT if (day and i == upto) else ((150, 60, 55, 255) if day else (190, 70, 60, 255))
            d.rectangle((x0, by(v), x1, cy0 + ch), fill=col)
            vs = str(v)
            d.text(((x0 + x1) / 2 - text_w(d, vs, font('bold', 44)) / 2, by(v) - 56), vs, font=font('bold', 44),
                   fill=INK if (day is None or i == upto) else MUTED)
        else:
            d.rectangle((x0, cy0 + ch - 6, x1, cy0 + ch), fill=FAINT)
        lab = dd.strftime('%a').upper()[:3] + f' {dd.day}'
        lf = font('bold' if dd == ELECTION else 'regular', 36)
        d.text(((x0 + x1) / 2 - text_w(d, lab, lf) / 2, cy0 + ch + 14), lab, font=lf,
               fill=ACCENT if dd == ELECTION else MUTED)
    yb = by(baseline)
    for xx in range(int(cx0), int(cx0 + cw), 28):
        d.line((xx, yb, xx + 14, yb), fill=(255, 255, 255, 150), width=3)
    d.text((cx0 + cw + 12, yb - 22), f'Oct avg {baseline:.0f}', font=font('regular', 34), fill=MUTED)

    # --- numbers (right) ---
    rx = 1620
    if day is None:
        lines = [('How to read', 'bold', 46, INK),
                 ('Each frame is one day. A state is red when news', 'regular', 38, MUTED),
                 ('articles about the election reported protests there.', 'regular', 38, MUTED),
                 ('Brighter = more reports that day.', 'regular', 38, MUTED)]
        y = 40
        for s, k, sz, c in lines:
            d.text((rx, y), s, font=font(k, sz), fill=c); y += sz + 22
    else:
        key = day.isoformat()
        v = nat[key]
        d.text((rx, 24), str(v), font=font('bold', 150), fill=ACCENT)
        nx = rx + text_w(d, str(v), font('bold', 150)) + 30
        d.text((nx, 48), 'election-linked', font=font('regular', 44), fill=INK)
        d.text((nx, 102), 'protest reports', font=font('regular', 44), fill=INK)
        ratio = v / baseline if baseline else 0
        share = v / allp[key] if allp[key] else 0
        d.text((rx, 196), f"{ratio:.1f}× the October daily average ({baseline:.0f})", font=font('regular', 38), fill=MUTED)
        d.text((rx, 244), f"{share:.0%} of all {allp[key]} protest reports that day", font=font('regular', 38), fill=MUTED)
        top3 = sorted(((el[(key, s)], s) for s in shapes if el[(key, s)] > 0), reverse=True)[:3]
        d.text((rx, 300), 'Top: ' + ('  ·  '.join(f"{FIPS[s]} {n}" for n, s in top3) or 'none'), font=font('bold', 38), fill=INK)
    # --- legend (bottom right) ---
    lx, ly = rx, 362
    d.text((lx, ly), 'Reports per state that day', font=font('regular', 34), fill=MUTED)
    sx = lx
    for lo, hi, c in BINS:
        lab = f'{lo}–{hi}' if hi < 10**9 else f'{lo}+'
        d.rectangle((sx, ly + 52, sx + 52, ly + 96), fill=c + (255,), outline=(255, 255, 255, 90), width=2)
        d.text((sx + 62, ly + 54), lab, font=font('regular', 34), fill=INK)
        sx += 62 + text_w(d, lab, font('regular', 34)) + 40
    foot = 'Source: GDELT 2.0 events (protest codes 14x), machine-coded news reports, not verified incidents. Alaska & Hawaii not shown.'
    if thin: foot += f" Incomplete data: {', '.join(thin)}."
    d.text((60, h - 58), foot, font=font('regular', 30), fill=FAINT)
    return img

def frame(day, name):
    canvas = Image.new('RGBA', (PX_W, PX_H), (0, 0, 0, 0))
    counts = {s: el[(day.isoformat(), s)] for s in shapes} if day else None
    draw_map(canvas, counts)
    if day:
        for n, s in sorted(((counts[s], s) for s in shapes if counts[s] > 0), reverse=True)[:3]:
            draw_state_number(canvas, s, n)
    ttl = title_panel(day)
    paste_at_lat(canvas, ttl, PX_W / 2, px(0, TOP_LAT)[1] - ttl.height / 2, TOP_LAT)
    dp = data_panel(day)
    paste_at_lat(canvas, dp, PX_W / 2, px(0, BOTTOM_LAT)[1] - dp.height / 2, BOTTOM_LAT)
    canvas.save(os.path.join(OUT, name), optimize=True)
    return name

names = [frame(None, '00-intro.png')] + [frame(d, f'{i + 1:02d}-{d.isoformat()}.png') for i, d in enumerate(DAYS)]

# ---------- scene document ----------
def digest(name):
    b = open(os.path.join(OUT, name), 'rb').read()
    return len(b), hashlib.sha256(b).hexdigest()
ATTR = {'text': 'GDELT Project 2.0 Events, aggregated by state and day; boundaries: Natural Earth',
        'license': 'GDELT Terms of Use: free use with citation and link; Natural Earth public domain',
        'url': 'https://www.gdeltproject.org/about.html'}
packs = []
for n in names:
    size, sha = digest(n)
    packs.append({'id': n[:-4], 'version': 1, 'format': 'image',
                  'source': {'adapter': 'assets', 'path': f'{SLUG}/{n}'}, 'attribution': ATTR,
                  'placement': {'bounds': [W, S, E, N], 'height': HEIGHT_M, 'altitudeReference': 'ellipsoid'},
                  'byteLength': size, 'sha256': sha})
CAM = {'lat': (S + N) / 2 - 0.5, 'lon': (W + E) / 2, 'alt': 6_400_000, 'heading': 0, 'pitch': -90, 'roll': 0}
VISUAL = {'style': 'normal', 'hud': {'visible': False, 'variant': 'minimal'}}
shots = [{'id': 'intro', 'title': 'Election-linked protest reports · 7 days', 'durationSec': 3, 'holdSec': 3.5,
          'camera': CAM, 'visual': VISUAL, 'dataPackIds': [packs[0]['id']]}]
for i, d in enumerate(DAYS):
    shots.append({'id': f'day-{d.isoformat()}', 'title': f"{weekday_label(d)} · {relative_label(d).lower()} · {nat[d.isoformat()]} reports",
                  'durationSec': 0.2, 'holdSec': 4.0 if d == ELECTION else 2.6,
                  'camera': CAM, 'visual': VISUAL, 'dataPackIds': [packs[i + 1]['id']]})
doc = {'version': 6, 'scenes': [{'id': 'us-midterms-week', 'title': 'US midterms 2026 · election-linked protest reports, day by day',
                                 'dataPacks': packs, 'shots': shots}]}
json.dump(doc, open(os.path.join(OUT, f'{SLUG}.scene.json'), 'w'), indent=1)

total = sum(s['durationSec'] + s['holdSec'] for s in shots)
print('days:', ', '.join(f"{d} {nat[d]}" for d in DAY_KEYS), f'| Oct baseline {baseline:.1f}/day')
print(f'frames: {len(names)}  runtime ~{total:.0f}s  -> {OUT}')
if thin: print('WARNING incomplete days:', thin)
