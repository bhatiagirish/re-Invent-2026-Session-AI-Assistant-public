"""Catalog index cache: search the WHOLE catalog without a slow live walk.

Why this exists
---------------
AWS Events' ``ListSessions`` can only be paged (250 sessions per page, ~10 pages
for ~2,400 sessions) and has no search or filter. Reading every page takes far
longer than one chat turn, so a live search could only cover the first few
pages and silently missed ~70% of the catalog.

What it is (and is not)
-----------------------
* It is a compact INDEX of the catalog (code, title, type, level, venue, room,
  services, topics, features, speakers, time). It is used ONLY to decide which
  sessions match a search or a count. The sessions actually shown to the user
  are re-read live from AWS Events by the caller, so room/time/availability are
  never stale.
* It is NOT a cache of user state. Favorites, reservations, the schedule and
  personal time are always read live and never stored here.
* It has a bounded lifetime: re-checked against AWS Events' reported
  ``totalCount`` every ``CATALOG_RECHECK_SECONDS`` and rebuilt when that changes
  or after ``CATALOG_MAX_AGE_SECONDS``. Every use reports how old it is.

Storage (same DynamoDB table)
-----------------------------
  PK ``CATALOG#<event>``            SK ``META``            pointers + progress
  PK ``CATALOG#<event>#<gen>``      SK ``ROWS#<page>#<n>`` JSON rows (<= ~300 KB)

A rebuild writes a NEW generation and flips ``ready_gen`` only when complete, so
the index being served is never half-written. Old generations expire via TTL.
The build is RESUMABLE: each ``warm()`` call reads pages for a bounded time and
saves its position, so the build completes across a few calls.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from models import Session, SessionTime

_log = logging.getLogger("reinvent_assistant")

# In-process copy of the last index read, keyed by (event, generation), so a
# warm Lambda container does not re-read ~1 MB from DynamoDB on every search.
_MEM: dict = {"key": None, "sessions": []}


# ── Session <-> compact row ────────────────────────────────────────────────
_SIMPLE = (("ty", "type"), ("l", "level"), ("r", "room"), ("v", "venue"))
_LISTS = (("sv", "services"), ("tp", "topics"), ("f", "features"), ("sp", "speakers"))


def session_to_row(s: Session) -> dict:
    """Compact, JSON-safe row. Abstracts are never stored (they are large and
    only needed for details, which are read live)."""
    row: dict = {"i": s.session_id, "c": s.abbreviation or "", "t": s.title or ""}
    for key, attr in _SIMPLE:
        value = getattr(s, attr)
        if value:
            row[key] = value
    for key, attr in _LISTS:
        value = getattr(s, attr)
        if value:
            row[key] = list(value)
    st = s.session_time
    if st:
        row["d"], row["tm"], row["ln"] = st.date, st.time, st.length
    if s.is_reservable:
        row["rs"] = 1
    if s.is_all_day:
        row["ad"] = 1
    return row


def row_to_session(row: dict) -> Session:
    st = None
    if "d" in row or "tm" in row or "ln" in row:
        st = SessionTime(date=row.get("d"), time=row.get("tm"), length=row.get("ln"))
    return Session(
        session_id=row["i"],
        abbreviation=row.get("c", ""),
        title=row.get("t", ""),
        type=row.get("ty"),
        level=row.get("l"),
        room=row.get("r"),
        venue=row.get("v"),
        services=list(row.get("sv", [])),
        topics=list(row.get("tp", [])),
        features=list(row.get("f", [])),
        speakers=list(row.get("sp", [])),
        session_time=st,
        is_reservable=bool(row.get("rs")),
        is_all_day=bool(row.get("ad")),
    )


def _split_rows(rows: list, max_bytes: int) -> list[str]:
    """JSON payloads for `rows`, split (halving) until each fits `max_bytes`, so
    one DynamoDB item never exceeds the 400 KB limit."""
    payload = json.dumps(rows, separators=(",", ":"))
    if len(payload.encode("utf-8")) <= max_bytes or len(rows) <= 1:
        return [payload]
    mid = len(rows) // 2
    return _split_rows(rows[:mid], max_bytes) + _split_rows(rows[mid:], max_bytes)


def format_age(seconds: float) -> str:
    """'under a minute', '12 min', '2 h 5 min' -- for 'index built X ago'."""
    s = max(0, int(seconds))
    if s < 60:
        return "under a minute"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


# ── Page walking (shared with the code lookup) ─────────────────────────────
def walk_pages(events, on_page: Callable[[list], bool], next_token: Optional[str],
               *, max_pages: int, budget_seconds: float) -> tuple[int, Optional[str], str]:
    """Read ListSessions pages, returning ``(pages_read, next_token, stopped)``
    with `stopped` in found/end/cap/budget. Uses the client's single-connection
    walk when available (one handshake for all pages); otherwise falls back to
    one ``list_sessions`` call per page. `on_page(sessions)` returns True to stop.
    At least one page is always read."""
    walker = getattr(events, "walk_session_pages", None)
    if walker is not None:
        return walker(on_page, max_pages=max_pages, budget_seconds=budget_seconds,
                      next_token=next_token)
    deadline = time.monotonic() + budget_seconds
    pages = 0
    while True:
        page, next_token = events.list_sessions(include_abstracts=False,
                                                next_token=next_token)
        pages += 1
        if on_page(page):
            return pages, next_token, "found"
        if not next_token:
            return pages, None, "end"
        if pages >= max_pages:
            return pages, next_token, "cap"
        if time.monotonic() > deadline:
            return pages, next_token, "budget"


# ── Snapshot handed to the agent ───────────────────────────────────────────
@dataclass
class CatalogSnapshot:
    """A read-only view of the index. `total_count` is what AWS Events reported
    when it was built; `len(sessions)` is how many rows were indexed."""

    sessions: list
    built_at: float
    checked_at: float
    total_count: int
    now: float

    @property
    def age_seconds(self) -> int:
        return max(0, int(self.now - self.built_at))

    @property
    def age_text(self) -> str:
        return format_age(self.now - self.built_at)

    @property
    def checked_text(self) -> str:
        return format_age(self.now - self.checked_at)


# ── The cache ──────────────────────────────────────────────────────────────
_STR_KEYS = ("ready_gen", "build_gen", "build_token")
_NUM_KEYS = ("ready_at", "checked_at", "ready_total", "ready_count",
             "build_pages", "build_count", "build_started")
_INT_KEYS = {"ready_total", "ready_count", "build_pages", "build_count"}


class CatalogCache:
    """Builds and reads the catalog index in DynamoDB (low-level boto3 client)."""

    def __init__(self, ddb, event_id: Optional[str] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.ddb = ddb
        self.event_id = event_id or config.EVENT_ID
        self._clock = clock or time.time

    # -- keys / meta ------------------------------------------------------
    def _meta_key(self) -> dict:
        return {"PK": {"S": f"CATALOG#{self.event_id}"}, "SK": {"S": "META"}}

    def _rows_pk(self, gen: str) -> str:
        return f"CATALOG#{self.event_id}#{gen}"

    def _get_meta(self) -> dict:
        item = self.ddb.get_item(TableName=config.DDB_TABLE_NAME,
                                 Key=self._meta_key()).get("Item")
        if not item:
            return {}
        meta: dict = {}
        for k in _STR_KEYS:
            if k in item:
                meta[k] = item[k]["S"]
        for k in _NUM_KEYS:
            if k in item:
                value = float(item[k]["N"])
                meta[k] = int(value) if k in _INT_KEYS else value
        return meta

    def _put_meta(self, meta: dict, now: float) -> None:
        item = dict(self._meta_key())
        item["ttl"] = {"N": str(int(now) + 7 * 86400)}
        for k in _STR_KEYS:
            if meta.get(k):
                item[k] = {"S": str(meta[k])}
        for k in _NUM_KEYS:
            if meta.get(k) is not None:
                item[k] = {"N": str(meta[k])}
        self.ddb.put_item(TableName=config.DDB_TABLE_NAME, Item=item)

    # -- state ------------------------------------------------------------
    def _is_ready(self, meta: dict, now: float) -> bool:
        return bool(meta.get("ready_gen")) and \
            (now - float(meta.get("ready_at", 0))) < config.CATALOG_MAX_AGE_SECONDS

    def _status(self, meta: dict, now: float) -> dict:
        status: dict = {
            "enabled": True,
            "ready": self._is_ready(meta, now),
            "building": bool(meta.get("build_gen")),
        }
        if meta.get("ready_gen"):
            status["sessions"] = int(meta.get("ready_count", 0))
            status["totalCount"] = int(meta.get("ready_total", 0))
            status["ageSeconds"] = max(0, int(now - float(meta.get("ready_at", now))))
            status["builtAt"] = datetime.fromtimestamp(
                float(meta["ready_at"]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if status["building"]:
            status["pagesBuilt"] = int(meta.get("build_pages", 0))
        return status

    def status(self) -> dict:
        """Current state without doing any work."""
        return self._status(self._get_meta(), self._clock())

    # -- reading ----------------------------------------------------------
    def load_ready(self) -> Optional[CatalogSnapshot]:
        """The usable index, or None when there isn't one (never built, expired,
        or incomplete). Best-effort: any storage error returns None so a cache
        problem can never break a search -- the caller falls back to a live,
        partial search and says so."""
        try:
            meta = self._get_meta()
            now = self._clock()
            if not self._is_ready(meta, now):
                return None
            expected = int(meta.get("ready_count", -1))
            sessions = self._read_sessions(meta["ready_gen"], expected)
            if not sessions or len(sessions) != expected:
                return None
            ready_at = float(meta["ready_at"])
            return CatalogSnapshot(
                sessions=sessions, built_at=ready_at,
                checked_at=float(meta.get("checked_at") or ready_at),
                total_count=int(meta.get("ready_total") or len(sessions)), now=now)
        except Exception as exc:                          # noqa: BLE001 - best effort
            _log.warning("catalog index unavailable (%s)", type(exc).__name__)
            return None

    def _read_sessions(self, gen: str, expected: int) -> list[Session]:
        key = (self.event_id, gen)
        if _MEM["key"] == key and len(_MEM["sessions"]) == expected:
            return _MEM["sessions"]
        rows: list = []
        start = None
        while True:
            kwargs = dict(
                TableName=config.DDB_TABLE_NAME,
                KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                ExpressionAttributeValues={":p": {"S": self._rows_pk(gen)},
                                           ":s": {"S": "ROWS#"}},
            )
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = self.ddb.query(**kwargs)
            for item in resp.get("Items", []):
                rows.extend(json.loads(item["rows"]["S"]))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        sessions = [row_to_session(r) for r in rows]
        if len(sessions) == expected:
            _MEM["key"], _MEM["sessions"] = key, sessions
        return sessions

    # -- building ---------------------------------------------------------
    def _put_rows(self, gen: str, page_index: int, sessions: list[Session],
                  now: float) -> None:
        rows = [session_to_row(s) for s in sessions]
        ttl = int(now) + config.CATALOG_MAX_AGE_SECONDS + 7200
        for part, payload in enumerate(_split_rows(rows, config.CATALOG_CHUNK_BYTES)):
            self.ddb.put_item(TableName=config.DDB_TABLE_NAME, Item={
                "PK": {"S": self._rows_pk(gen)},
                "SK": {"S": f"ROWS#{page_index:04d}#{part:03d}"},
                "rows": {"S": payload},
                "ttl": {"N": str(ttl)},
            })

    @staticmethod
    def _probe_total(events) -> Optional[int]:
        """One cheap ListSessions call, just to read AWS Events' totalCount."""
        events.list_sessions(include_abstracts=False)
        return getattr(events, "last_total_count", None)

    def warm(self, events, budget_seconds: Optional[float] = None) -> dict:
        """Advance the index towards 'ready' and return its status.

        * Fresh and recently verified: do nothing.
        * Fresh but not verified lately: compare AWS Events' totalCount with the
          index's; unchanged -> just record the check; changed -> rebuild.
        * Older than the soft limit (CATALOG_REFRESH_SECONDS): rebuild, whatever
          the count says, while the old index keeps serving until the new one is
          complete. (A count check cannot see a swap: one session added and one
          removed leaves the total unchanged.)
        * Missing / past the hard limit / changed: build (or RESUME a partial
          build) for up to `budget_seconds`, saving progress. A build finishes
          across a few calls.

        MCP errors propagate to the caller (typed); progress already saved stays.
        """
        budget = config.CATALOG_WARM_BUDGET_SECONDS if budget_seconds is None else budget_seconds
        now = self._clock()
        meta = self._get_meta()

        # A build already under way is simply continued. This must come BEFORE
        # the freshness check: while rebuilding because the catalog size changed,
        # the old index still reports the old size, and re-checking it would
        # restart the build on every call so it could never finish.
        resumable = bool(meta.get("build_gen")) and \
            now - float(meta.get("build_started", 0)) < config.CATALOG_BUILD_MAX_AGE_SECONDS

        if not resumable and self._is_ready(meta, now):
            if now - float(meta["ready_at"]) < config.CATALOG_REFRESH_SECONDS:
                since = now - float(meta.get("checked_at") or meta["ready_at"])
                if since < config.CATALOG_RECHECK_SECONDS:
                    return self._status(meta, now)
                total = self._probe_total(events)
                if total is not None and total == int(meta.get("ready_total") or -1):
                    meta["checked_at"] = now
                    self._put_meta(meta, now)
                    return self._status(meta, now)
                # catalog size changed -> fall through and start a new build
            # Otherwise the index is past the soft refresh age: rebuild it now.
            # The old index keeps serving searches until the new one completes
            # (the pointer only flips at the end), so there is no coverage gap.

        if resumable:
            gen = meta["build_gen"]
            token = meta.get("build_token") or None
            state = {"pages": int(meta.get("build_pages", 0)),
                     "count": int(meta.get("build_count", 0))}
            started = float(meta["build_started"])
        else:
            gen = f"{int(now)}-{secrets.token_hex(3)}"
            token, started = None, now
            state = {"pages": 0, "count": 0}

        def on_page(page: list) -> bool:
            self._put_rows(gen, state["pages"], page, now)
            state["pages"] += 1
            state["count"] += len(page)
            return False

        # A brand-new build answers after its first page(s): the caller learns
        # within seconds that a build is under way, instead of only after a
        # full ~15 s step. The following calls continue at full size.
        max_pages = config.CATALOG_MAX_PAGES
        if not resumable and config.CATALOG_FIRST_STEP_PAGES > 0:
            max_pages = min(max_pages, config.CATALOG_FIRST_STEP_PAGES)

        _, next_token, stopped = walk_pages(
            events, on_page, token,
            max_pages=max_pages, budget_seconds=budget)

        done_at = self._clock()
        total = getattr(events, "last_total_count", None)
        if stopped == "end":
            # Complete: flip the pointer to the new generation in one write.
            meta.update(ready_gen=gen, ready_at=done_at, checked_at=done_at,
                        ready_count=state["count"],
                        ready_total=total if total is not None else state["count"])
            for k in ("build_gen", "build_token", "build_pages", "build_count",
                      "build_started"):
                meta.pop(k, None)
        else:
            meta.update(build_gen=gen, build_token=next_token,
                        build_pages=state["pages"], build_count=state["count"],
                        build_started=started)
        self._put_meta(meta, done_at)
        return self._status(meta, done_at)
