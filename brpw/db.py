"""SQLite storage: accounts, people, encounters, settings, with migrations and recovery."""
import csv
import json
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

MAX_BACKUPS = 5


@dataclass
class Player:
    gamertag: str
    xuid: Optional[str] = None
    observed_at: Optional[int] = None  # only set when the source really provides a timestamp


@dataclass
class MergeResult:
    new: int = 0
    known: int = 0
    encounters: int = 0


def _m1(c: sqlite3.Connection) -> None:
    # executescript() would COMMIT mid-migration, so run statements one by one.
    for stmt in _SCHEMA_V1.split(";"):
        if stmt.strip():
            c.execute(stmt)


_SCHEMA_V1 = """
    CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE accounts(
      id INTEGER PRIMARY KEY,
      xuid TEXT,
      gamertag TEXT NOT NULL,
      gamertag_key TEXT NOT NULL,
      person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
      first_seen INTEGER NOT NULL,
      last_seen INTEGER NOT NULL,
      source TEXT NOT NULL);
    CREATE UNIQUE INDEX ux_accounts_xuid ON accounts(xuid) WHERE xuid IS NOT NULL;
    CREATE UNIQUE INDEX ux_accounts_tag_noxuid ON accounts(gamertag_key) WHERE xuid IS NULL;
    CREATE INDEX ix_accounts_tag ON accounts(gamertag_key);
    CREATE INDEX ix_accounts_last_seen ON accounts(last_seen DESC);
    CREATE INDEX ix_accounts_person ON accounts(person_id);
    CREATE TABLE gamertag_history(
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      gamertag TEXT NOT NULL, seen_at INTEGER NOT NULL,
      PRIMARY KEY(account_id, gamertag));
    CREATE TABLE encounters(
      id INTEGER PRIMARY KEY,
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      observed_at INTEGER NOT NULL,
      source TEXT NOT NULL,
      UNIQUE(account_id, observed_at, source));
    CREATE INDEX ix_enc_account ON encounters(account_id, observed_at DESC);
    CREATE TABLE latest_list(
      account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
      synced_at INTEGER NOT NULL);
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _m2(c: sqlite3.Connection) -> None:
    c.execute("ALTER TABLE latest_list ADD COLUMN pos INTEGER")  # position in the source's list (0 = first)


def _m3(c: sqlite3.Connection) -> None:
    from .live import migrate_live_table
    migrate_live_table(c)


def _m4(c: sqlite3.Connection) -> None:
    # Who is a friend depends on which Xbox account (OpenXBL account name) the app is syncing as.
    c.execute("""CREATE TABLE friends(
        owner TEXT NOT NULL, xuid TEXT NOT NULL, relation TEXT NOT NULL,
        PRIMARY KEY(owner, xuid))""")


def _m5(c: sqlite3.Connection) -> None:
    # Free-form tags replace the old "person" groups. Existing groups become tags, nothing is lost.
    c.execute("""CREATE TABLE tags(
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        tag TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(account_id, tag))""")
    c.execute("CREATE INDEX ix_tags_tag ON tags(tag)")
    c.execute("INSERT OR IGNORE INTO tags(account_id, tag) "
              "SELECT a.id, p.name FROM accounts a JOIN people p ON p.id = a.person_id")


def _m6(c: sqlite3.Connection) -> None:
    # "Latest list" per synced Xbox account: switching accounts must not look like new encounters.
    c.execute("""CREATE TABLE latest_by_owner(
        owner TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        synced_at INTEGER NOT NULL, pos INTEGER, PRIMARY KEY(owner, account_id))""")
    c.execute("ALTER TABLE live_status ADD COLUMN title TEXT")        # game from Xbox presence
    c.execute("ALTER TABLE live_status ADD COLUMN seen_at INTEGER")   # when Xbox says they were last online


def _m7(c: sqlite3.Connection) -> None:
    # Batched summaries: Xbox's own isBroadcasting flag and the provider named in the broadcast record, if any.
    c.execute("ALTER TABLE live_status ADD COLUMN broadcasting INTEGER NOT NULL DEFAULT 0")
    c.execute("ALTER TABLE live_status ADD COLUMN provider TEXT")


def _m8(c: sqlite3.Connection) -> None:
    # Optional Twitch section: the Twitch channel a user saved for a player (exact numeric Twitch ID) and its last status.
    c.execute("""CREATE TABLE twitch_links(
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        twitch_id TEXT NOT NULL, login TEXT NOT NULL, display_name TEXT NOT NULL, linked_at INTEGER NOT NULL)""")
    c.execute("CREATE INDEX ix_twitch_links_tid ON twitch_links(twitch_id)")
    c.execute("""CREATE TABLE twitch_status(
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        checked_at INTEGER NOT NULL, live INTEGER NOT NULL, title TEXT, game TEXT, viewers INTEGER, started_at INTEGER)""")


def _m9(c: sqlite3.Connection) -> None:
    # "Never link": players the user never wants a Twitch channel for (cheaters, ...). Guess Twitch skips them.
    c.execute("""CREATE TABLE twitch_never(
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        reason TEXT NOT NULL DEFAULT '', set_at INTEGER NOT NULL)""")


def _m10(c: sqlite3.Connection) -> None:
    # Auto-matcher: where a Twitch link came from, and per Twitch stream whether the player was in the game.
    c.execute("ALTER TABLE twitch_links ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    c.execute("""CREATE TABLE twitch_evidence(
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        twitch_id TEXT NOT NULL, login TEXT NOT NULL, display_name TEXT NOT NULL,
        session INTEGER NOT NULL, result INTEGER NOT NULL, at INTEGER NOT NULL,
        PRIMARY KEY(account_id, twitch_id, session))""")


def _m11(c: sqlite3.Connection) -> None:
    # Shared list (see shared.py): the downloaded master list, master links this user removed (not re-added),
    # and own links already sent as suggestions.
    c.execute("""CREATE TABLE shared_links(
        xuid TEXT PRIMARY KEY, gamertag TEXT NOT NULL, twitch_id TEXT NOT NULL, login TEXT NOT NULL,
        display_name TEXT NOT NULL)""")
    c.execute("CREATE TABLE shared_hidden(xuid TEXT NOT NULL, twitch_id TEXT NOT NULL, PRIMARY KEY(xuid, twitch_id))")
    c.execute("""CREATE TABLE shared_sent(xuid TEXT NOT NULL, twitch_id TEXT NOT NULL, sent_at INTEGER NOT NULL,
        PRIMARY KEY(xuid, twitch_id))""")


MIGRATIONS = [_m1, _m2, _m3, _m4, _m5, _m6, _m7, _m8, _m9, _m10, _m11]  # index i migrates user_version i -> i+1
SCHEMA_VERSION = len(MIGRATIONS)


def _key(gamertag: str) -> str:
    return " ".join(gamertag.casefold().split())


class Database:
    def __init__(self, path: Path, migrations=None):
        self.path = Path(path)
        self.migrations = migrations if migrations is not None else MIGRATIONS
        self.lock = threading.RLock()
        self.recovered_from: Optional[str] = None
        self.conn = self._open()

    # ---- open / recovery / migration ----
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _healthy(self) -> bool:
        try:
            conn = self._connect()
            try:
                return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return False

    def _backups(self) -> List[Path]:
        d = self.path.parent / "backups"
        return sorted(d.glob(self.path.stem + "-*.db")) if d.exists() else []

    def backup(self, tag: str = "") -> Optional[Path]:
        if not self.path.exists():
            return None
        d = self.path.parent / "backups"
        d.mkdir(exist_ok=True)
        dest = d / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{tag}.db"
        src = sqlite3.connect(self.path)
        try:
            dst = sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()
        for old in self._backups()[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
        return dest

    def _open(self) -> sqlite3.Connection:
        if self.path.exists() and not self._healthy():
            bad = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
            self.path.replace(bad)
            self.recovered_from = str(bad)
            backups = self._backups()
            if backups:
                shutil.copy2(backups[-1], self.path)
        conn = self._connect()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(self.migrations):
            conn.close()
            raise RuntimeError(
                f"Database was created by a newer version (schema {version}); please update the app.")
        if version < len(self.migrations):
            if version > 0:
                conn.close()
                self.backup(tag="-premigrate")
                conn = self._connect()
            try:
                for i in range(version, len(self.migrations)):
                    conn.execute("BEGIN")
                    self.migrations[i](conn)
                    conn.execute(f"PRAGMA user_version={i + 1}")
                    conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return conn

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    # ---- settings ----
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ---- merging observations ----
    def merge_players(self, players: Iterable[Player], source: str, live: bool,
                      now: Optional[int] = None, owner: str = "") -> MergeResult:
        """Merge one observed list atomically.

        live=True: the list replaces that Xbox account's (`owner`) "latest list" snapshot; a timestamp-less
        player is recorded as an encounter only when it newly appears in THAT account's snapshot. The very
        first sync of an account is a baseline: only brand-new players get an encounter.
        live=False (import): timestamp-less players get one encounter per account.
        Accounts are never merged by similar gamertags; only by XUID or by exact gamertag
        among accounts that have no XUID.
        """
        now = int(now if now is not None else time.time())
        res = MergeResult()
        with self.lock, self.conn:
            prev = {r[0] for r in self.conn.execute(
                "SELECT account_id FROM latest_by_owner WHERE owner=?", (owner,))}
            baseline = live and self.conn.execute(
                "SELECT 1 FROM settings WHERE key=?", ("list_synced:" + owner,)).fetchone() is None
            if live:
                self.conn.execute("DELETE FROM latest_by_owner WHERE owner=?", (owner,))
            seen_ids = set()
            for idx, p in enumerate(players):
                tag = p.gamertag.strip()
                if not tag and not p.xuid:
                    continue
                tag = tag or p.xuid
                acc = None
                if p.xuid:
                    acc = self.conn.execute("SELECT * FROM accounts WHERE xuid=?", (p.xuid,)).fetchone()
                else:
                    acc = self.conn.execute(
                        "SELECT * FROM accounts WHERE xuid IS NULL AND gamertag_key=?",
                        (_key(tag),)).fetchone()
                observed = p.observed_at or now
                if acc is None:
                    cur = self.conn.execute(
                        "INSERT INTO accounts(xuid,gamertag,gamertag_key,first_seen,last_seen,source) "
                        "VALUES(?,?,?,?,?,?)", (p.xuid, tag, _key(tag), observed, observed, source))
                    aid = cur.lastrowid
                    res.new += 1
                    fresh = True
                else:
                    aid = acc["id"]
                    fresh = False
                    if aid in seen_ids:
                        continue
                    res.known += 1
                    if acc["gamertag"] != tag and p.xuid:
                        self.conn.execute("UPDATE accounts SET gamertag=?, gamertag_key=? WHERE id=?",
                                          (tag, _key(tag), aid))
                seen_ids.add(aid)
                self.conn.execute(
                    "INSERT OR IGNORE INTO gamertag_history(account_id,gamertag,seen_at) VALUES(?,?,?)",
                    (aid, tag, now))
                if p.observed_at:
                    record = True
                elif live:
                    record = fresh or (aid not in prev and not baseline)
                else:
                    record = fresh or not self.conn.execute(
                        "SELECT 1 FROM encounters WHERE account_id=? AND source=?",
                        (aid, source)).fetchone()
                if record:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO encounters(account_id,observed_at,source) VALUES(?,?,?)",
                        (aid, observed, source))
                    res.encounters += cur.rowcount
                    self.conn.execute("UPDATE accounts SET last_seen=MAX(last_seen,?) WHERE id=?",
                                      (observed, aid))
                if live:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO latest_by_owner(owner,account_id,synced_at,pos) VALUES(?,?,?,?)",
                        (owner, aid, now, idx))
            if live:
                self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                  ("list_synced:" + owner, "1"))
        return res

    # ---- queries ----
    def list_accounts(self, search: str = "", owner: Optional[str] = "") -> List[sqlite3.Row]:
        owner = owner or ""
        q = """SELECT a.id, a.xuid, a.gamertag, a.last_seen, a.first_seen, a.person_id,
                      (SELECT GROUP_CONCAT(tag, ', ') FROM (SELECT tag FROM tags t WHERE t.account_id=a.id
                                                            ORDER BY tag)) AS tags, a.source,
                      (SELECT COUNT(*) FROM encounters e WHERE e.account_id=a.id) AS encounters,
                      EXISTS(SELECT 1 FROM latest_by_owner l WHERE l.account_id=a.id AND l.owner=?) AS in_latest,
                      (SELECT l.pos FROM latest_by_owner l WHERE l.account_id=a.id AND l.owner=?) AS pos,
                      f.relation AS relation,
                      tl.twitch_id AS twitch_id, tl.login AS twitch_login, tl.display_name AS twitch_name,
                      ts.live AS twitch_live, ts.viewers AS twitch_viewers, ts.game AS twitch_game,
                      ts.title AS twitch_title, ts.checked_at AS twitch_checked, tl.source AS twitch_source,
                      tn.reason AS twitch_never
               FROM accounts a
               LEFT JOIN friends f ON f.xuid=a.xuid AND f.owner=?
               LEFT JOIN twitch_links tl ON tl.account_id=a.id
               LEFT JOIN twitch_status ts ON ts.account_id=a.id
               LEFT JOIN twitch_never tn ON tn.account_id=a.id"""
        args: list = [owner, owner, owner]      # in_latest, pos, friends join (textual order)
        if search.strip():
            like = f"%{search.strip().casefold()}%"
            q += (" WHERE a.gamertag_key LIKE ? OR IFNULL(a.xuid,'') LIKE ?"
                  " OR EXISTS(SELECT 1 FROM tags t WHERE t.account_id=a.id AND t.tag LIKE ?)")
            args += [like, like, like]
        q += " ORDER BY a.last_seen DESC, pos IS NULL, pos ASC, a.id DESC"
        with self.lock:
            return self.conn.execute(q, args).fetchall()

    def account_details(self, account_ids: List[int], owner: Optional[str] = "") -> List[sqlite3.Row]:
        """One summary row per account for the Encounters view (everything here comes from stored data)."""
        if not account_ids:
            return []
        owner = owner or ""
        marks = ",".join("?" * len(account_ids))
        with self.lock:
            return self.conn.execute(f"""
                SELECT a.id, a.gamertag, a.xuid, a.first_seen, a.last_seen,
                  (SELECT COUNT(*) FROM encounters e WHERE e.account_id=a.id) AS times,
                  (SELECT l.pos FROM latest_by_owner l WHERE l.account_id=a.id AND l.owner=?) AS pos,
                  f.relation AS relation,
                  (SELECT GROUP_CONCAT(tag, ', ') FROM tags t WHERE t.account_id=a.id) AS tags,
                  (SELECT GROUP_CONCAT(gamertag, ', ') FROM gamertag_history h
                     WHERE h.account_id=a.id AND h.gamertag<>a.gamertag) AS old_names,
                  s.state, s.title, s.seen_at, s.checked_at
                FROM accounts a
                LEFT JOIN friends f ON f.xuid=a.xuid AND f.owner=?
                LEFT JOIN live_status s ON s.account_id=a.id
                WHERE a.id IN ({marks}) ORDER BY a.last_seen DESC""", [owner, owner, *account_ids]).fetchall()

    # ---- friends (per synced Xbox account) ----
    def replace_friends(self, owner: str, relations: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM friends WHERE owner=?", (owner,))
            self.conn.executemany("INSERT INTO friends(owner,xuid,relation) VALUES(?,?,?)",
                                  [(owner, x, r) for x, r in relations.items()])
        self.set_setting("friends_at:" + owner, str(int(time.time())))

    def friend_relations(self, owner: Optional[str]) -> dict:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(
                "SELECT xuid, relation FROM friends WHERE owner=?", (owner,))}

    def count_accounts(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def encounters_for(self, account_id: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT observed_at, source FROM encounters WHERE account_id=? ORDER BY observed_at DESC",
                (account_id,)).fetchall()

    def ensure_account(self, gamertag: str, xuid: str, source: str = "lookup") -> int:
        """The account with this XUID, added if new (no encounter: the user looked them up, not met them)."""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id FROM accounts WHERE xuid=?", (xuid,)).fetchone()
            if row:
                return row[0]
            row = self.conn.execute("SELECT id FROM accounts WHERE xuid IS NULL AND gamertag_key=?",
                                    (_key(gamertag),)).fetchone()
            if row:                                      # an imported name without XUID: it now has an exact one
                self.conn.execute("UPDATE accounts SET xuid=? WHERE id=?", (xuid, row[0]))
                return row[0]
            now = int(time.time())
            return self.conn.execute(
                "INSERT INTO accounts(xuid,gamertag,gamertag_key,first_seen,last_seen,source) VALUES(?,?,?,?,?,?)",
                (xuid, gamertag, _key(gamertag), now, now, source)).lastrowid

    # ---- Twitch (saved by the user, verified against Twitch, matched by exact Twitch ID) ----
    def link_twitch(self, account_id: int, twitch_id: str, login: str, display_name: str,
                    source: str = "manual") -> None:
        if self.twitch_never([account_id]):
            raise ValueError("This player is marked 'never link'. Remove that mark first.")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO twitch_links(account_id,twitch_id,login,display_name,linked_at,source) "
                "VALUES(?,?,?,?,?,?)", (account_id, twitch_id, login, display_name, int(time.time()), source))
            self.conn.execute("DELETE FROM twitch_status WHERE account_id=?", (account_id,))   # old status is stale now

    def unlink_twitch(self, account_ids: List[int]) -> None:
        """Remove links. A removed link that came from the shared list is remembered, so syncing won't re-add it."""
        with self.lock, self.conn:
            for a in account_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO shared_hidden(xuid, twitch_id) SELECT ac.xuid, l.twitch_id "
                    "FROM twitch_links l JOIN accounts ac ON ac.id=l.account_id "
                    "WHERE l.account_id=? AND l.source='shared' AND ac.xuid IS NOT NULL", (a,))
            self.conn.executemany("DELETE FROM twitch_links WHERE account_id=?", [(a,) for a in account_ids])
            self.conn.executemany("DELETE FROM twitch_status WHERE account_id=?", [(a,) for a in account_ids])

    def twitch_links(self, account_ids: Optional[List[int]] = None) -> List[sqlite3.Row]:
        with self.lock:
            if account_ids is None:
                return self.conn.execute("SELECT * FROM twitch_links").fetchall()
            if not account_ids:
                return []
            marks = ",".join("?" * len(account_ids))
            return self.conn.execute(f"SELECT * FROM twitch_links WHERE account_id IN ({marks})", list(account_ids)).fetchall()

    def set_twitch_never(self, account_ids: List[int], reason: str = "") -> None:
        """Mark players as 'never link a Twitch channel'; any existing link is removed."""
        self.unlink_twitch(account_ids)
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO twitch_never(account_id,reason,set_at) VALUES(?,?,?)",
                                  [(a, (reason or "").strip(), int(time.time())) for a in account_ids])

    def clear_twitch_never(self, account_ids: List[int]) -> None:
        with self.lock, self.conn:
            self.conn.executemany("DELETE FROM twitch_never WHERE account_id=?", [(a,) for a in account_ids])

    def twitch_never(self, account_ids: List[int]) -> dict:
        """account_id -> reason, for those marked 'never link'."""
        if not account_ids:
            return {}
        marks = ",".join("?" * len(account_ids))
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(
                f"SELECT account_id, reason FROM twitch_never WHERE account_id IN ({marks})", list(account_ids))}

    # ---- shared list (see shared.py) ----
    def replace_shared(self, rows: List[dict]) -> None:
        """Store a freshly downloaded master list and apply it."""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM shared_links")
            self.conn.executemany(
                "INSERT OR REPLACE INTO shared_links(xuid,gamertag,twitch_id,login,display_name) VALUES(?,?,?,?,?)",
                [(r["xuid"], r["gamertag"], r["twitch_id"], r["twitch_login"], r.get("twitch_name") or r["twitch_login"])
                 for r in rows])
        self.apply_shared()

    def apply_shared(self) -> int:
        """Give players in this history their master-list channel, unless the user linked one themselves, marked them
        'never link' or removed that shared link. Shared links that left the master list (or changed) are updated.
        Returns the number of players that got a new shared link."""
        now = int(time.time())
        with self.lock, self.conn:
            self.conn.execute(
                """DELETE FROM twitch_links WHERE source='shared' AND NOT EXISTS(
                     SELECT 1 FROM shared_links s JOIN accounts a ON a.xuid=s.xuid
                     WHERE a.id=twitch_links.account_id AND s.twitch_id=twitch_links.twitch_id)""")
            cur = self.conn.execute(
                """INSERT INTO twitch_links(account_id,twitch_id,login,display_name,linked_at,source)
                   SELECT a.id, s.twitch_id, s.login, s.display_name, ?, 'shared'
                   FROM shared_links s JOIN accounts a ON a.xuid=s.xuid
                   WHERE NOT EXISTS(SELECT 1 FROM twitch_links l WHERE l.account_id=a.id)
                     AND NOT EXISTS(SELECT 1 FROM twitch_never n WHERE n.account_id=a.id)
                     AND NOT EXISTS(SELECT 1 FROM shared_hidden h WHERE h.xuid=s.xuid AND h.twitch_id=s.twitch_id)""",
                (now,))
            return cur.rowcount

    def shared_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM shared_links").fetchone()[0]

    def links_to_share(self, include_sent: bool = False) -> List[dict]:
        """Own hand-made links (with an XUID) that the master list does not have yet."""
        with self.lock:
            rows = self.conn.execute(
                f"""SELECT a.xuid, a.gamertag, l.twitch_id, l.login AS twitch_login, l.display_name AS twitch_name
                   FROM twitch_links l JOIN accounts a ON a.id=l.account_id
                   WHERE l.source='manual' AND a.xuid IS NOT NULL
                     AND NOT EXISTS(SELECT 1 FROM shared_links s WHERE s.xuid=a.xuid AND s.twitch_id=l.twitch_id)
                     {"" if include_sent else
                      "AND NOT EXISTS(SELECT 1 FROM shared_sent x WHERE x.xuid=a.xuid AND x.twitch_id=l.twitch_id)"}
                   ORDER BY a.gamertag_key""").fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, rows: List[dict]) -> None:
        now = int(time.time())
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO shared_sent(xuid,twitch_id,sent_at) VALUES(?,?,?)",
                                  [(r["xuid"], r["twitch_id"], now) for r in rows])

    def shared_entry(self, xuids: List[str]) -> Dict[str, sqlite3.Row]:
        if not xuids:
            return {}
        marks = ",".join("?" * len(xuids))
        with self.lock:
            return {r["xuid"]: r for r in self.conn.execute(
                f"SELECT * FROM shared_links WHERE xuid IN ({marks})", list(xuids))}

    def store_twitch_status(self, rows) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO twitch_status(account_id,checked_at,live,title,game,viewers,started_at) "
                "VALUES(?,?,?,?,?,?,?)", list(rows))

    # ---- tags (manual labels; never applied automatically) ----
    @staticmethod
    def _clean_tag(tag: str) -> str:
        return " ".join(tag.replace(",", " ").split())[:40]

    def add_tag(self, account_ids: List[int], tag: str) -> None:
        tag = self._clean_tag(tag)
        if not tag or not account_ids:
            raise ValueError("A tag and at least one account are required")
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO tags(account_id, tag) VALUES(?,?)",
                                  [(a, tag) for a in account_ids])

    def remove_tag(self, account_ids: List[int], tag: str) -> None:
        with self.lock, self.conn:
            self.conn.executemany("DELETE FROM tags WHERE account_id=? AND tag=?", [(a, tag) for a in account_ids])

    def all_tags(self) -> List[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag")]

    def tags_for(self, account_ids: List[int]) -> List[str]:
        if not account_ids:
            return []
        with self.lock:
            marks = ",".join("?" * len(account_ids))
            return [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT tag FROM tags WHERE account_id IN ({marks}) ORDER BY tag", account_ids)]

    def encounters_for_many(self, account_ids: List[int], limit: int = 2000) -> List[sqlite3.Row]:
        if not account_ids:
            return []
        with self.lock:
            marks = ",".join("?" * len(account_ids))
            return self.conn.execute(
                f"SELECT a.gamertag, e.observed_at, e.source FROM encounters e JOIN accounts a ON a.id=e.account_id "
                f"WHERE e.account_id IN ({marks}) ORDER BY e.observed_at DESC, e.id DESC LIMIT ?",
                [*account_ids, limit]).fetchall()

    # ---- export ----
    def export(self, path: Path) -> None:
        path = Path(path)
        rows = self.list_accounts()
        if path.suffix.lower() == ".json":
            data = [{k: r[k] for k in ("xuid", "gamertag", "tags", "first_seen", "last_seen", "source")}
                    | {"encounters": [dict(e) for e in self.encounters_for(r["id"])]} for r in rows]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["gamertag", "xuid", "tags", "first_seen", "last_seen", "source", "encounters"])
                for r in rows:
                    w.writerow([r["gamertag"], r["xuid"] or "", r["tags"] or "", r["first_seen"],
                                r["last_seen"], r["source"], r["encounters"]])
