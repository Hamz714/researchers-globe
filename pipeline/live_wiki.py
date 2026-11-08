"""Live Wikipedia edits (Wikimedia EventStreams), the real-time layer. Owner: Person A/B.

ONE long-lived HTTP connection to https://stream.wikimedia.org/v2/stream/recentchange (server-sent
events, every edit on every Wikimedia wiki, ~20/s; no key). There is nothing to rate-limit: we never
poll. We keep only English Wikipedia article edits (namespace 0, type edit/new) to pages of things in
the registry (mines' or owners' pages, ports, countries, US states, companies), in memory for 24 h.

Checked 8 Nov 2026 from the venue: content-type text/event-stream; each event has `data: {json}` with
wiki, namespace, type, title, timestamp (unix s), user, bot, comment, revision {old,new}, server_url.

  * On start it replays the last LIVE_WIKI_BACKFILL_MIN minutes (`?since=`), so "edited in the last
    hour" is meaningful immediately; on reconnect it resumes with Last-Event-ID.
  * Every minute it snapshots the matched edits to cache/live_wiki/edits.json, so a restart, or
    GLOBE_OFFLINE=1 on stage, still has the recent edits (labelled with their age).
  * LIVE_WIKI=0 disables it. It never starts with GLOBE_OFFLINE=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
USER_AGENT = os.environ.get("WIKI_USER_AGENT", "ResearchersGlobe/0.1 (HackTheBurgh 2026 student project)")
KEEP_S = 24 * 3600


def snapshot_path() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "live_wiki" / "edits.json"


class LiveWiki:
    def __init__(self) -> None:
        self.watch: dict[str, list[tuple[str, str]]] = {}
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
        self._stop = threading.Event()

    # ---------------------------------------------------------------- matching (pure, unit-tested)
    def set_watch(self, watch: dict[str, list[tuple[str, str]]]) -> None:
        self.watch = {t.replace("_", " "): v for t, v in watch.items()}

    def handle(self, ev: dict) -> dict | None:
        """One stream event -> a matched edit record (and stored), or None."""
        self.events_seen += 1
        self.last_event_ts = ev.get("timestamp") or self.last_event_ts
        if ev.get("wiki") != "enwiki" or ev.get("namespace") != 0 or ev.get("type") not in ("edit", "new"):
            return None
        title = str(ev.get("title", "")).replace("_", " ")
        things = self.watch.get(title)
        if not things:
            return None
        rev = ev.get("revision") or {}
        rec = {"title": title, "things": [list(t) for t in things], "ts": float(ev.get("timestamp") or time.time()),
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

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            return list(self.edits)[-limit:][::-1]

    def status(self) -> dict:
        with self.lock:
            n = len(self.edits)
        return {"running": bool(self._thread and self._thread.is_alive()), "connected": self.connected,
                "connected_since": self.connected_since, "events_seen": self.events_seen,
                "matched_24h": n, "watching_titles": len(self.watch), "last_event_ts": self.last_event_ts,
                "last_error": self.last_error, "now": time.time()}

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
    def start(self, watch: dict[str, list[tuple[str, str]]]) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.set_watch(watch)
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
        backfill_min = float(os.environ.get("LIVE_WIKI_BACKFILL_MIN", "30"))
        while not self._stop.is_set():
            params, headers = {}, {"User-Agent": USER_AGENT, "Accept": "text/event-stream"}
            if self._last_id:
                headers["Last-Event-ID"] = self._last_id
            else:
                since = datetime.fromtimestamp(time.time() - backfill_min * 60, tz=timezone.utc)
                params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                with requests.get(STREAM_URL, params=params, headers=headers, stream=True, timeout=(10, 60)) as r:
                    r.raise_for_status()
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
        LIVE.load_snapshot()  # still answer from the last snapshot, labelled with its age
        return False
    from reasoning.things import all_wiki_titles

    LIVE.start(all_wiki_titles())
    return True
