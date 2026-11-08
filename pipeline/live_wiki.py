"""Live Wikipedia edits (Wikimedia EventStreams), the real-time layer. Owner: Person A/B.

ONE long-lived HTTP connection to https://stream.wikimedia.org/v2/stream/recentchange (server-sent
events, every edit on every Wikimedia wiki, ~20/s; no key). There is nothing to rate-limit: we never
poll. We keep only English Wikipedia article edits (namespace 0, type edit/new) about things in
the registry (mines' or owners' pages, ports, countries, US states, companies), in memory for 24 h.

Checked 8 Nov 2026 from the venue: content-type text/event-stream; each event has `data: {json}` with
wiki, namespace, type, title, timestamp (unix s), user, bot, comment, revision {old,new}, server_url.

  * Besides each thing's own page, an edit counts for a thing when the edited article's TITLE names it
    ("Springfield, Illinois" -> Illinois): exact pages alone get ~2 edits/hour in total, titles ~800.
  * On start it replays the last LIVE_WIKI_BACKFILL_MIN (default 60) minutes (`?since=`), so "edited in the last
    hour" is meaningful immediately; on reconnect it resumes with Last-Event-ID.
  * Every minute it snapshots the matched edits to cache/live_wiki/edits.json, so a restart, or
    GLOBE_OFFLINE=1 on stage, still has the recent edits (labelled with their age).
  * LIVE_WIKI=0 disables it. It never starts with GLOBE_OFFLINE=1.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
# Wikimedia's User-Agent policy wants a way to contact the client (URL or email); without one, requests
# can be refused with 403. The shared WIKI_USER_AGENT in old .env files has no contact, so it isn't used here.
USER_AGENT = os.environ.get("LIVE_WIKI_USER_AGENT") or \
    "ResearchersGlobe/0.2 (https://github.com/Hamz714/researchers-globe; HackTheBurgh 2026) python-requests"
KEEP_S = 24 * 3600


def snapshot_path() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "live_wiki" / "edits.json"


class LiveWiki:
    def __init__(self) -> None:
        self.watch: dict[str, list[tuple[str, str]]] = {}
        self.mentions: list[tuple[re.Pattern, tuple]] = []   # article-title patterns -> things
        self.edits: deque[dict] = deque()       # matched edits, oldest first
        self.lock = threading.Lock()
        self.connected = False
        self.started_at: float | None = None
        self.connected_since: float | None = None
        self.events_seen = 0
        self.last_event_ts: float | None = None
        self.last_error: str | None = None
        self._last_id: str | None = None
        self._thread: threading.Thread | None = None
        self.disabled_reason: str | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- matching (pure, unit-tested)
    def set_watch(self, watch: dict[str, list[tuple[str, str]]], mentions=()) -> None:
        """watch: exact page title -> things. mentions: (regex, things) - an edit to any article whose
        title matches also counts ("2026 Brazilian Grand Prix" -> Brazil). See reasoning.things."""
        self.watch = {t.replace("_", " "): v for t, v in watch.items()}
        self.mentions = [(re.compile(p), tuple(v)) for p, v in mentions]

    def match(self, title: str) -> list[list[str]]:
        """[[kind, id, "page"|"mention"], ...] for one article title; its own page wins over a mention."""
        out = {tid: [kind, tid, "page"] for kind, tid in self.watch.get(title, [])}
        for rx, things in self.mentions:
            if rx.search(title):
                for kind, tid in things:
                    out.setdefault(tid, [kind, tid, "mention"])
        return list(out.values())

    def handle(self, ev: dict) -> dict | None:
        """One stream event -> a matched edit record (and stored), or None."""
        self.events_seen += 1
        self.last_event_ts = ev.get("timestamp") or self.last_event_ts
        if ev.get("wiki") != "enwiki" or ev.get("namespace") != 0 or ev.get("type") not in ("edit", "new"):
            return None
        title = str(ev.get("title", "")).replace("_", " ")
        things = self.match(title)
        if not things:
            return None
        rev = ev.get("revision") or {}
        rec = {"title": title, "things": things, "ts": float(ev.get("timestamp") or time.time()),
               "user": ev.get("user", ""), "bot": bool(ev.get("bot")), "type": ev.get("type"),
               "comment": str(ev.get("comment", ""))[:160],
               "url": (f"{ev.get('server_url', 'https://en.wikipedia.org')}/w/index.php?diff={rev['new']}&oldid={rev['old']}"
                       if rev.get("new") and rev.get("old") else f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")}
        with self.lock:
            self.edits.append(rec)
            self._prune()
        return rec

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_S
        while self.edits and self.edits[0]["ts"] < cutoff:
            self.edits.popleft()

    # ---------------------------------------------------------------- queries
    def edits_for(self, title: str, since_ts: float) -> list[dict]:
        with self.lock:
            return [e for e in self.edits if e["title"] == title and e["ts"] >= since_ts]

    def edits_for_thing(self, thing_id: str, since_ts: float) -> list[dict]:
        """Edits in the window that count for this thing, each with "via" (page / mention), oldest first."""
        with self.lock:
            recs = [e for e in self.edits if e["ts"] >= since_ts]
        out = []
        for e in recs:
            for t in e["things"]:
                if t[1] == thing_id:
                    out.append({**e, "via": t[2] if len(t) > 2 else "page"})
                    break
        return out

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            return list(self.edits)[-limit:][::-1]

    def status(self) -> dict:
        with self.lock:
            n = len(self.edits)
        return {"running": bool(self._thread and self._thread.is_alive()), "connected": self.connected,
                "connected_since": self.connected_since, "events_seen": self.events_seen,
                "matched_24h": n, "watching_titles": len(self.watch), "last_event_ts": self.last_event_ts,
                "last_error": self.last_error, "disabled_reason": self.disabled_reason, "now": time.time()}

    # ---------------------------------------------------------------- snapshot
    def load_snapshot(self) -> None:
        p = snapshot_path()
        if p.exists():
            try:
                recs = json.loads(p.read_text())
            except json.JSONDecodeError:
                return
            with self.lock:
                self.edits = deque(sorted(recs, key=lambda r: r["ts"]))
                self._prune()

    def save_snapshot(self) -> None:
        p = snapshot_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            recs = list(self.edits)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(recs))
        tmp.replace(p)

    # ---------------------------------------------------------------- stream
    def start(self, watch: dict[str, list[tuple[str, str]]], mentions=()) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.set_watch(watch, mentions)
        self.load_snapshot()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="live-wiki", daemon=True)
        self._thread.start()
        threading.Thread(target=self._snapshots, name="live-wiki-snap", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _snapshots(self) -> None:
        while not self._stop.wait(60):
            try:
                self.save_snapshot()
            except OSError as e:
                log.warning("live wiki snapshot failed: %s", e)

    def _run(self) -> None:
        import requests

        backoff = 2.0
        backfill_min = float(os.environ.get("LIVE_WIKI_BACKFILL_MIN", "60"))
        while not self._stop.is_set():
            params, headers = {}, {"User-Agent": USER_AGENT, "Accept": "text/event-stream"}
            if self._last_id:
                headers["Last-Event-ID"] = self._last_id
            else:
                since = datetime.fromtimestamp(time.time() - backfill_min * 60, tz=timezone.utc)
                params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                with requests.get(STREAM_URL, params=params, headers=headers, stream=True, timeout=(10, 60)) as r:
                    if r.status_code != 200:
                        body = r.raw.read(300, decode_content=True).decode("utf-8", "replace")
                        raise RuntimeError(f"HTTP {r.status_code} from Wikimedia: {' '.join(body.split())[:200]}")
                    self.connected, self.connected_since, self.last_error = True, time.time(), None
                    backoff = 2.0
                    data, ev_id = [], None
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if raw is None:
                            continue
                        line = raw.rstrip("\r")
                        if not line:  # end of one event
                            if data:
                                try:
                                    self.handle(json.loads("".join(data)))
                                except json.JSONDecodeError:
                                    pass
                                if ev_id:
                                    self._last_id = ev_id
                            data, ev_id = [], None
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line.startswith("id:"):
                            ev_id = line[3:].strip()
            except Exception as e:  # network drop, timeout, HTTP error: reconnect with backoff
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("live wiki stream: %s (reconnecting in %.0fs)", self.last_error, backoff)
            self.connected = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)


LIVE = LiveWiki()


def start_if_enabled() -> bool:
    """Start the stream (backend startup). Returns True if it was started."""
    if os.environ.get("LIVE_WIKI", "1") == "0" or os.environ.get("GLOBE_OFFLINE", "0") == "1":
        LIVE.disabled_reason = "LIVE_WIKI=0 in .env" if os.environ.get("LIVE_WIKI", "1") == "0" else \
            "GLOBE_OFFLINE=1 in .env (set it to 0 for the live stream)"
        log.warning("live wiki stream NOT started: %s", LIVE.disabled_reason)
        LIVE.load_snapshot()  # still answer from the last snapshot, labelled with its age
        return False
    from reasoning.things import all_wiki_titles, mention_patterns

    LIVE.start(all_wiki_titles(), mention_patterns())
    return True


def check(seconds: float = 20.0) -> int:
    """`python -m pipeline.live_wiki`: connect the way the backend does and say exactly what happens."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    print(f"LIVE_WIKI={os.environ.get('LIVE_WIKI', '(unset = on)')}  GLOBE_OFFLINE={os.environ.get('GLOBE_OFFLINE', '(unset = 0)')}")
    if os.environ.get("LIVE_WIKI", "1") == "0" or os.environ.get("GLOBE_OFFLINE", "0") == "1":
        print("-> the backend will NOT start the stream with these settings. Set GLOBE_OFFLINE=0 and LIVE_WIKI=1 in .env.")
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if os.environ.get(k):
            print(f"{k}={os.environ[k]} (requests will use it)")
    print(f"User-Agent: {USER_AGENT}")
    from reasoning.things import all_wiki_titles, mention_patterns

    lw = LiveWiki()
    watch, mentions = all_wiki_titles(), mention_patterns()
    print(f"watching {len(watch)} pages + {len(mentions)} title patterns; connecting to {STREAM_URL} ...")
    lw.start(watch, mentions)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(2)
        st = lw.status()
        lag = f"{(time.time() - st['last_event_ts']) / 60:.0f} min behind" if st["last_event_ts"] else "no events yet"
        print(f"  {time.time() - t0:4.0f}s connected={st['connected']} events={st['events_seen']} "
              f"matched={len(lw.edits)} ({lag}) error={st['last_error']}")
    lw.stop()
    ok = lw.events_seen > 0
    print("OK: the stream works from this machine. If the globe still says offline, restart the backend "
          "and check GET /live/status." if ok else
          "FAILED: no events. Paste the error lines above to the tech lead.")
    for e in list(lw.edits)[-5:]:
        print(f"  matched: {e['title']} by {e['user']}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    sys.exit(check(float(sys.argv[1]) if len(sys.argv) > 1 else 20.0))
