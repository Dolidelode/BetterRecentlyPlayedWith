"""Live Now: Xbox-confirmed Twitch broadcasting for players in the local history.

What is (and is not) possible is documented in docs/TWITCH_RESEARCH.md. In short:
* OpenXBL has no batch presence (verified: comma-separated XUIDs -> SerializationError), so each player
  costs one request. Only the top LIVE_SCOPE players of Xbox's current list are checked, within a small hourly
  budget, with a per-player cache.
* "Live" is only reported when Xbox's own presence record for that XUID contains a broadcast whose provider
  is Twitch. Nothing is guessed from names. Xbox does not give the Twitch channel or stream title, so none
  is shown.
"""
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

from .db import Database
from .providers import ProviderError, http_json

PRESENCE_URL = "https://api.xbl.io/v2/presence/"
LIVE_SCOPE = 20              # only the top N of Xbox's current recent-players list are ever checked
HOURLY_BUDGET = 30            # presence requests per hour (20% of the free OpenXBL tier of 150)
ONLINE_TTL = 10 * 60          # re-check a player who was online after 10 min
OFFLINE_TTL = 3 * 3600        # ... and an offline one after 3 h
LIVE_MAX_AGE = 20 * 60        # a "live" result older than this is no longer shown as live


@dataclass
class Presence:
    state: str
    title: Optional[str] = None      # current game, or the game they were last seen in
    seen_at: Optional[int] = None    # when Xbox says they were last online (or now, if online)
    broadcast: Optional[str] = None  # provider of ANY broadcast record Xbox reported (Twitch or not)
    twitch: bool = False
    viewers: Optional[int] = None
    started: Optional[int] = None
    broadcast_id: Optional[str] = None


def _find_broadcasts(node):
    """Dicts stored under any key containing 'broadcast' (shape in REST responses is not documented
    by Microsoft, so this looks for the documented field names rather than assuming a path)."""
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


def parse_presence(payload) -> Presence:
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, dict) or "state" not in content:
        raise ProviderError("Unexpected presence response from OpenXBL.")
    p = Presence(state=str(content["state"]))
    from .providers import _iso_to_epoch
    last = content.get("lastSeen")
    if isinstance(last, dict):                                   # offline players: Xbox's own "last seen"
        p.title = str(last.get("titleName") or "") or None
        p.seen_at = _iso_to_epoch(last.get("timestamp"))
    for d in content.get("devices") or []:                       # online players: what they are playing
        for t in (d.get("titles") or []) if isinstance(d, dict) else []:
            if isinstance(t, dict) and t.get("name"):
                p.title = str(t["name"])
                break
        if p.title:
            break
    for b in _find_broadcasts(content):
        provider = str(b.get("provider", ""))
        if p.broadcast is None:
            p.broadcast = provider or "unknown provider"
        if "twitch" in provider.casefold():
            p.twitch = True
            v = b.get("viewerCount", b.get("viewers"))
            p.viewers = int(v) if isinstance(v, (int, float)) else None
            p.broadcast_id = str(b.get("broadcastId", b.get("id", ""))) or None
            s = b.get("startTime", b.get("started"))
            p.started = int(s) if isinstance(s, (int, float)) else _iso_to_epoch(s)
            break
    return p


def migrate_live_table(c) -> None:
    c.execute("""CREATE TABLE live_status(
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        checked_at INTEGER NOT NULL, state TEXT NOT NULL, twitch INTEGER NOT NULL DEFAULT 0,
        viewers INTEGER, started INTEGER, broadcast_id TEXT)""")


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
        """Accounts due for a check: only the top `scope` of Xbox's current list. Never-checked players first,
        then the stalest; online players are re-checked sooner than offline ones."""
        t = int(self.now())
        with self.db.lock:
            rows = self.db.conn.execute("""
            SELECT a.id, a.xuid, l.pos, a.last_seen, s.checked_at, s.state
            FROM accounts a
            JOIN latest_by_owner l ON l.account_id = a.id AND l.owner = ?
            LEFT JOIN live_status s ON s.account_id = a.id
            WHERE a.xuid IS NOT NULL AND l.pos < ?
            ORDER BY l.pos""", (self._owner(), self.scope)).fetchall()
        due = []
        for r in rows:
            ttl = ONLINE_TTL if (r["state"] or "").casefold() == "online" else OFFLINE_TTL
            if r["checked_at"] is None or t - r["checked_at"] >= ttl:
                due.append(dict(r))
        # never-checked players first (they cost the same and tell us the most), then stalest
        due.sort(key=lambda r: (r["checked_at"] is not None, r["checked_at"] or 0))
        return due[:limit]

    # ---- one cycle ----
    def run_cycle(self, max_requests: int = 3) -> str:
        """Spend at most max_requests (and never more than the hourly budget). Returns a status line."""
        key = self.key.get()
        if not key:
            raise ProviderError("No OpenXBL key set.", reauth=True)
        done = 0
        for cand in self.candidates(min(max_requests, self.budget_left())):
            try:
                payload = self.http(PRESENCE_URL + cand["xuid"], None, {"X-Authorization": key})
                self.spent.append(self.now())
                pres = parse_presence(payload)
            except ProviderError as e:
                code = getattr(e, "code", 0)
                if code == 429:
                    self.blocked_until = self.now() + 15 * 60
                    self.last_error = "OpenXBL rate limit reached; Live Now pauses for 15 minutes."
                    return self.last_error
                if code in (401, 403):
                    raise ProviderError("OpenXBL rejected the key.", reauth=True)
                self.spent.append(self.now())
                self.last_error = str(e)
                continue
            self._store(cand["id"], pres)
            done += 1
        self.last_error = None
        return f"Checked {done} player(s); {self.budget_left()} of {HOURLY_BUDGET} hourly requests left."

    def check_accounts(self, account_ids: List[int], limit: int = 10) -> List[dict]:
        """On-demand check of the given accounts (user pressed 'Check live'): ignores the cache, costs one
        request per player, at most `limit` per call. Counts towards the hourly budget but is not blocked by it."""
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
        out: List[dict] = []
        asked = 0                                  # requests actually sent in this call
        for r in rows:
            res = {"id": r["id"], "gamertag": r["gamertag"], "state": None, "title": None, "seen_at": None,
                   "twitch": False, "viewers": None, "broadcast": None, "error": None}
            if not r["xuid"]:
                res["error"] = "no XUID known (imported by name only), so Xbox can't be asked"
                out.append(res)
                continue
            if asked >= limit:
                res["error"] = f"skipped: at most {limit} players per check"
                out.append(res)
                continue
            if self.now() < self.blocked_until:
                res["error"] = "OpenXBL rate limit reached recently; try again in a few minutes"
                out.append(res)
                continue
            asked += 1
            try:
                p = parse_presence(self.http(PRESENCE_URL + r["xuid"], None, {"X-Authorization": key}))
                self.spent.append(self.now())
            except ProviderError as e:
                code = getattr(e, "code", 0)
                if code in (401, 403):
                    raise ProviderError("OpenXBL rejected the key.", reauth=True)
                self.spent.append(self.now())
                if code == 429:
                    self.blocked_until = self.now() + 15 * 60
                    res["error"] = "OpenXBL rate limit reached; paused for 15 minutes"
                else:
                    res["error"] = str(e)
                out.append(res)
                continue
            self._store(r["id"], p)
            res.update(state=p.state, title=p.title, seen_at=p.seen_at, twitch=p.twitch, viewers=p.viewers,
                       broadcast=p.broadcast)
            out.append(res)
        return out

    def _store(self, account_id: int, p: Presence) -> None:
        now = int(self.now())
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO live_status(account_id,checked_at,state,twitch,viewers,started,broadcast_id,"
                "title,seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (account_id, now, p.state, int(p.twitch), p.viewers, p.started, p.broadcast_id, p.title,
                 now if p.state.casefold() == "online" else p.seen_at))

    # ---- reading results ----
    def live_now(self) -> list:
        """Only confirmed, fresh Xbox broadcast records with provider Twitch."""
        t = int(self.now())
        with self.db.lock:
            return self.db.conn.execute(
                """SELECT a.id, a.gamertag, a.xuid, s.viewers, s.started, s.checked_at, s.broadcast_id
                   FROM live_status s JOIN accounts a ON a.id = s.account_id
                   WHERE s.twitch = 1 AND s.checked_at >= ? ORDER BY s.viewers IS NULL, s.viewers DESC""",
                (t - LIVE_MAX_AGE,)).fetchall()

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
