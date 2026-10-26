"""Live check: which recent players does Xbox itself report as broadcasting?

Source: OpenXBL's player summary (GET /v2/player/summary/{xuid,xuid,...}). Verified against a real account:
* every person record has `isBroadcasting` (bool), `presenceState`, a localised `presenceText` and a
  `broadcast` field (the broadcast record when there is one);
* up to 10 XUIDs work in one request (15 and 20 are rejected as "Invalid URL"), so 20 players cost 2 requests.
Nothing is inferred from names. Xbox does not say which service is broadcasting unless the broadcast record
does, so a player is shown as "broadcasting (per Xbox)" and only labelled Twitch when the record says so.
See docs/TWITCH_RESEARCH.md.
"""
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional

from .db import Database
from .providers import ProviderError, _iso_to_epoch, http_json

SUMMARY_URL = "https://api.xbl.io/v2/player/summary/"
PRESENCE_URL = "https://api.xbl.io/v2/presence/"   # one player: carries the broadcast record
BATCH = 10                    # verified maximum that works in one request
LIVE_SCOPE = 20               # only the top N of Xbox's current recent-players list are ever checked
HOURLY_BUDGET = 30            # requests per hour (20% of the free OpenXBL tier); 20 players = 2 requests
STALE_AFTER = 4 * 60          # a player checked less than 4 minutes ago is not asked again
LIVE_MAX_AGE = 20 * 60        # a "broadcasting" result older than this is no longer shown


@dataclass
class Presence:
    state: str
    title: Optional[str] = None        # Xbox's own (localised) presence text, e.g. what they are playing
    seen_at: Optional[int] = None
    broadcasting: bool = False         # Xbox's isBroadcasting flag (or a broadcast record is present)
    provider: Optional[str] = None     # from the broadcast record, when Xbox provides one
    twitch: bool = False               # the broadcast record names Twitch
    viewers: Optional[int] = None
    started: Optional[int] = None
    broadcast_id: Optional[str] = None


def _find_broadcasts(node):
    """Dicts stored under any key containing 'broadcast' (the REST shape is not documented by Microsoft, so
    this looks for the documented field names rather than assuming a path)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if "broadcast" in k.lower() and isinstance(v, dict):
                yield v
            elif "broadcast" in k.lower() and isinstance(v, list):
                for i in v:
                    if isinstance(i, dict):
                        yield i
            yield from _find_broadcasts(v)
    elif isinstance(node, list):
        for i in node:
            yield from _find_broadcasts(i)


def _apply_broadcast(pres: "Presence", rec: dict) -> None:
    provider = str(rec.get("provider", "")) or None
    pres.broadcasting = True
    pres.provider = provider
    pres.twitch = "twitch" in (provider or "").casefold()
    v = rec.get("viewerCount", rec.get("viewers"))
    pres.viewers = int(v) if isinstance(v, (int, float)) else None
    pres.broadcast_id = str(rec.get("broadcastId", rec.get("id", ""))) or None
    s = rec.get("startTime", rec.get("started"))
    pres.started = int(s) if isinstance(s, (int, float)) else _iso_to_epoch(s)


def parse_presence_detail(payload) -> "Presence":
    """Single-player presence (GET /v2/presence/{xuid}); used only to learn the details of a broadcast."""
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, dict) or "state" not in content:
        raise ProviderError("Unexpected presence response from OpenXBL.")
    p = Presence(state=str(content["state"]))
    rec = next(_find_broadcasts(content), None)
    if rec is not None:
        _apply_broadcast(p, rec)
    return p


def parse_summary(payload) -> Dict[str, Presence]:
    """xuid -> Presence for every person in a player-summary response."""
    content = payload.get("content") if isinstance(payload, dict) else None
    people = content.get("people") if isinstance(content, dict) else None
    if not isinstance(people, list):
        raise ProviderError("Unexpected player summary response from OpenXBL.")
    out: Dict[str, Presence] = {}
    for p in people:
        if not isinstance(p, dict) or not str(p.get("xuid", "")).isdigit():
            continue
        pres = Presence(state=str(p.get("presenceState") or "Unknown"),
                        title=str(p.get("presenceText") or "") or None)
        rec = next(_find_broadcasts(p), None)
        pres.broadcasting = p.get("isBroadcasting") is True or rec is not None
        if rec is not None:
            _apply_broadcast(pres, rec)
        out[str(p["xuid"])] = pres
    return out


def migrate_live_table(c) -> None:
    c.execute("""CREATE TABLE live_status(
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        checked_at INTEGER NOT NULL, state TEXT NOT NULL, twitch INTEGER NOT NULL DEFAULT 0,
        viewers INTEGER, started INTEGER, broadcast_id TEXT)""")


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


class LiveChecker:
    def __init__(self, db: Database, key, http: Callable = http_json, now: Callable[[], float] = time.time,
                 scope: int = LIVE_SCOPE):
        self.db, self.key, self.http, self.now, self.scope = db, key, http, now, scope
        self.spent: Deque[float] = deque()      # timestamps of requests in the last hour
        self.blocked_until = 0.0                # after a 429
        self.last_error: Optional[str] = None

    def _owner(self) -> str:
        active = getattr(self.key, "active", None)
        return (active() if callable(active) else "") or ""

    # ---- budget ----
    def budget_left(self) -> int:
        t = self.now()
        while self.spent and self.spent[0] < t - 3600:
            self.spent.popleft()
        return 0 if t < self.blocked_until else max(0, HOURLY_BUDGET - len(self.spent))

    # ---- candidates ----
    def candidates(self, limit: int) -> List[dict]:
        """In-scope players (top of the current list) not checked in the last few minutes, list order."""
        t = int(self.now())
        with self.db.lock:
            rows = self.db.conn.execute("""
                SELECT a.id, a.xuid, l.pos, s.checked_at
                FROM accounts a
                JOIN latest_by_owner l ON l.account_id = a.id AND l.owner = ?
                LEFT JOIN live_status s ON s.account_id = a.id
                WHERE a.xuid IS NOT NULL AND l.pos < ?
                ORDER BY l.pos""", (self._owner(), self.scope)).fetchall()
        return [dict(r) for r in rows if r["checked_at"] is None or t - r["checked_at"] >= STALE_AFTER][:limit]

    # ---- requests ----
    def _fetch(self, key: str, xuids: List[str]) -> Dict[str, Presence]:
        self.spent.append(self.now())           # every request counts, whether or not it succeeds
        return parse_summary(self.http(SUMMARY_URL + ",".join(xuids), None, {"X-Authorization": key}))

    def _handle_error(self, e: ProviderError) -> str:
        """401/403 -> re-raised as a reauth error; 429 -> pause for 15 minutes; otherwise the error text."""
        code = getattr(e, "code", 0)
        if code in (401, 403):
            raise ProviderError("OpenXBL rejected the key.", reauth=True)
        if code == 429:
            self.blocked_until = self.now() + 15 * 60
            return "OpenXBL rate limit reached; paused for 15 minutes"
        return str(e)

    def _enrich(self, key: str, account_id: int, xuid: str, p: Presence, respect_budget: bool) -> None:
        """The summary says "broadcasting" but may not name the service. Fetch that one player's presence for the
        broadcast record (service, viewers, start time). Skipped when a recent enrichment is stored, so a long
        broadcast does not cost a request every cycle. Failures never lose the summary result."""
        if not p.broadcasting or p.provider:
            return
        t = int(self.now())
        with self.db.lock:
            old = self.db.conn.execute(
                "SELECT provider, viewers, started, broadcast_id, twitch, checked_at FROM live_status "
                "WHERE account_id=? AND broadcasting=1 AND provider IS NOT NULL", (account_id,)).fetchone()
        if old is not None and t - old["checked_at"] < LIVE_MAX_AGE:
            p.provider, p.viewers, p.started = old["provider"], p.viewers or old["viewers"], old["started"]
            p.broadcast_id, p.twitch = old["broadcast_id"], bool(old["twitch"])
            return
        if (respect_budget and self.budget_left() <= 0) or self.now() < self.blocked_until:
            return
        self.spent.append(self.now())
        try:
            detail = parse_presence_detail(self.http(PRESENCE_URL + xuid, None, {"X-Authorization": key}))
        except ProviderError as e:
            self._handle_error(e)                  # 401 raises, 429 pauses; anything else is just skipped
            return
        if detail.broadcasting:
            p.provider, p.twitch, p.viewers = detail.provider, detail.twitch, detail.viewers or p.viewers
            p.started, p.broadcast_id = detail.started, detail.broadcast_id

    def run_cycle(self, max_requests: int = 3) -> str:
        """Automatic check: refresh the stale players in scope, 10 per request, within the hourly budget."""
        key = self.key.get()
        if not key:
            raise ProviderError("No OpenXBL key set.", reauth=True)
        allowed = min(max_requests, self.budget_left())
        checked = requests = 0
        self.last_error = None
        for batch in _chunks(self.candidates(self.scope), BATCH):
            if requests >= allowed:
                break
            requests += 1
            try:
                found = self._fetch(key, [c["xuid"] for c in batch])
            except ProviderError as e:
                self.last_error = self._handle_error(e)
                if getattr(e, "code", 0) == 429:
                    return "OpenXBL rate limit reached; Live checks pause for 15 minutes."
                continue
            for c in batch:
                if c["xuid"] in found:
                    self._enrich(key, c["id"], c["xuid"], found[c["xuid"]], respect_budget=True)
                    self._store(c["id"], found[c["xuid"]])
                    checked += 1
        return (f"Checked {checked} player(s) in {requests} request(s); "
                f"{self.budget_left()} of {HOURLY_BUDGET} hourly requests left.")

    def check_accounts(self, account_ids: List[int], limit: int = 2 * BATCH) -> List[dict]:
        """On-demand check of the given accounts ('Check live'): ignores the freshness rule, 10 players per
        request, at most `limit` players per call. Counts towards the hourly budget but is not blocked by it."""
        key = self.key.get()
        if not key:
            raise ProviderError("No OpenXBL key set.", reauth=True)
        if not account_ids:
            return []
        with self.db.lock:
            marks = ",".join("?" * len(account_ids))
            rows = self.db.conn.execute(
                f"SELECT id, gamertag, xuid FROM accounts WHERE id IN ({marks}) ORDER BY last_seen DESC",
                list(account_ids)).fetchall()
        results: Dict[int, dict] = {}
        askable = []
        for r in rows:
            res = results[r["id"]] = {
                "id": r["id"], "gamertag": r["gamertag"], "state": None, "title": None, "seen_at": None,
                "broadcasting": False, "provider": None, "twitch": False, "viewers": None, "started": None, "error": None}
            if not r["xuid"]:
                res["error"] = "no XUID known (imported by name only), so Xbox can't be asked"
            elif len(askable) >= limit:
                res["error"] = f"skipped: at most {limit} players per check"
            else:
                askable.append(r)
        for batch in _chunks(askable, BATCH):
            if self.now() < self.blocked_until:
                for r in batch:
                    results[r["id"]]["error"] = "OpenXBL rate limit reached recently; try again in a few minutes"
                continue
            try:
                found = self._fetch(key, [r["xuid"] for r in batch])
            except ProviderError as e:
                msg = self._handle_error(e)
                for r in batch:
                    results[r["id"]]["error"] = msg
                continue
            for r in batch:
                p = found.get(r["xuid"])
                if p is None:
                    results[r["id"]]["error"] = "Xbox returned nothing for this player"
                    continue
                self._enrich(key, r["id"], r["xuid"], p, respect_budget=False)   # explicit action: not budget-blocked
                self._store(r["id"], p)
                results[r["id"]].update(state=p.state, title=p.title, broadcasting=p.broadcasting,
                                        provider=p.provider, twitch=p.twitch, viewers=p.viewers, started=p.started)
        return [results[r["id"]] for r in rows]

    def _store(self, account_id: int, p: Presence) -> None:
        now = int(self.now())
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO live_status(account_id,checked_at,state,twitch,viewers,started,broadcast_id,"
                "title,seen_at,broadcasting,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, now, p.state, int(p.twitch), p.viewers, p.started, p.broadcast_id, p.title,
                 now if p.state.casefold() == "online" else p.seen_at, int(p.broadcasting), p.provider))

    # ---- reading results ----
    def live_now(self) -> list:
        """Players Xbox reports as broadcasting, confirmed recently."""
        t = int(self.now())
        with self.db.lock:
            return self.db.conn.execute(
                """SELECT a.id, a.gamertag, a.xuid, s.viewers, s.started, s.checked_at, s.broadcast_id, s.provider,
                          s.twitch
                   FROM live_status s JOIN accounts a ON a.id = s.account_id
                   WHERE (s.broadcasting = 1 OR s.twitch = 1) AND s.checked_at >= ?
                   ORDER BY s.viewers IS NULL, s.viewers DESC""", (t - LIVE_MAX_AGE,)).fetchall()

    def coverage(self) -> tuple:
        """(in-scope players checked at least once, in-scope players)."""
        with self.db.lock:
            c = self.db.conn
            base = ("FROM latest_by_owner l JOIN accounts a ON a.id = l.account_id "
                    "WHERE l.owner = ? AND a.xuid IS NOT NULL AND l.pos < ?")
            args = (self._owner(), self.scope)
            return (c.execute("SELECT COUNT(*) " + base + " AND EXISTS(SELECT 1 FROM live_status s "
                              "WHERE s.account_id = a.id)", args).fetchone()[0],
                    c.execute("SELECT COUNT(*) " + base, args).fetchone()[0])
