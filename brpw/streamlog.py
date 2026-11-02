"""Stream log (owner/dev tool): remembers every Twitch channel seen live in Sea of Thieves, so the list owner can work
through them later (look them up on SOTREP, add them to the master list, or mark them checked).

It uses the same Twitch app keys as the main app (Twitch setup), asks Twitch for the whole Sea of Thieves category every
few minutes (at most GAME_PAGES requests per round) and keeps the result in its own database, stream_log.db, in the
app-data folder. Nothing here guesses who a streamer is: linking happens only when the owner types the gamertag.
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

STATUSES = ("new", "checked", "linked", "skip")   # checked = looked up, nothing found yet; skip = not interesting
POLL_EVERY = 300                                    # seconds between rounds


class StreamLog:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS channels(
                    twitch_id TEXT PRIMARY KEY, login TEXT NOT NULL, name TEXT NOT NULL,
                    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, seen INTEGER NOT NULL DEFAULT 1,
                    peak INTEGER NOT NULL DEFAULT 0, language TEXT, title TEXT,
                    status TEXT NOT NULL DEFAULT 'new', gamertag TEXT, xuid TEXT, checked_at INTEGER);
                CREATE TABLE IF NOT EXISTS rounds(at INTEGER PRIMARY KEY, streams INTEGER, error TEXT);
            """)

    def close(self):
        with self.lock:
            self.conn.close()

    def record(self, streams: List[dict], now: Optional[int] = None) -> int:
        """Store one round of live streams. Returns how many channels were never seen before."""
        now = int(now if now is not None else time.time())
        new = 0
        with self.lock, self.conn:
            for s in streams:
                cur = self.conn.execute(
                    """UPDATE channels SET login=?, name=?, last_seen=?, seen=seen+1, peak=max(peak, ?), language=?,
                       title=? WHERE twitch_id=?""",
                    (s["login"], s["name"], now, s["viewers"], s["language"], s["title"], s["id"]))
                if cur.rowcount == 0:
                    self.conn.execute(
                        """INSERT INTO channels(twitch_id, login, name, first_seen, last_seen, peak, language, title)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (s["id"], s["login"], s["name"], now, now, s["viewers"], s["language"], s["title"]))
                    new += 1
            self.conn.execute("INSERT OR REPLACE INTO rounds(at, streams, error) VALUES(?,?,NULL)", (now, len(streams)))
        return new

    def record_error(self, error: str, now: Optional[int] = None) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO rounds(at, streams, error) VALUES(?,NULL,?)",
                              (int(now if now is not None else time.time()), error[:300]))

    def last_round(self) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM rounds ORDER BY at DESC LIMIT 1").fetchone()

    def first_round(self) -> Optional[int]:
        with self.lock:
            r = self.conn.execute("SELECT min(at) FROM rounds WHERE error IS NULL").fetchone()
        return r[0] if r else None

    def channels(self) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM channels ORDER BY last_seen DESC").fetchall()

    def set_status(self, twitch_ids: List[str], status: str, gamertag: Optional[str] = None,
                   xuid: Optional[str] = None, now: Optional[int] = None) -> None:
        if status not in STATUSES:
            raise ValueError(status)
        t = None if status == "new" else int(now if now is not None else time.time())
        with self.lock, self.conn:
            for i in twitch_ids:
                self.conn.execute(
                    "UPDATE channels SET status=?, gamertag=?, xuid=?, checked_at=? WHERE twitch_id=?",
                    (status, gamertag if status == "linked" else None, xuid if status == "linked" else None, t, i))

    def counts(self, in_master: Dict[str, str]) -> Dict[str, int]:
        out = {"total": 0, "todo": 0, "master": 0, **{s: 0 for s in STATUSES}}
        for r in self.channels():
            out["total"] += 1
            out[r["status"]] += 1
            if r["twitch_id"] in in_master:
                out["master"] += 1
            elif r["status"] == "new":
                out["todo"] += 1
        return out


def keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep while the log is running (the screen may still turn off). False elsewhere."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False
