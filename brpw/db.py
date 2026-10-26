"""SQLite storage: accounts, people, encounters, settings, with migrations and recovery."""
import csv
import json
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

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


MIGRATIONS = [_m1, _m2, _m3, _m4]  # index i migrates user_version i -> i+1
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
                      now: Optional[int] = None) -> MergeResult:
        """Merge one observed list atomically.

        live=True: the list replaces the "latest list" snapshot; a timestamp-less player is
        recorded as an encounter only when it newly appears in the snapshot.
        live=False (import): timestamp-less players get one encounter per account.
        Accounts are never merged by similar gamertags; only by XUID or by exact gamertag
        among accounts that have no XUID.
        """
        now = int(now if now is not None else time.time())
        res = MergeResult()
        with self.lock, self.conn:
            prev = {r[0] for r in self.conn.execute("SELECT account_id FROM latest_list")}
            if live:
                self.conn.execute("DELETE FROM latest_list")
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
                    record = fresh or aid not in prev
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
                        "INSERT OR REPLACE INTO latest_list(account_id,synced_at,pos) VALUES(?,?,?)", (aid, now, idx))
        return res

    # ---- queries ----
    def list_accounts(self, search: str = "", owner: Optional[str] = None) -> List[sqlite3.Row]:
        q = """SELECT a.id, a.xuid, a.gamertag, a.last_seen, a.first_seen, a.person_id,
                      p.name AS person, a.source,
                      (SELECT COUNT(*) FROM encounters e WHERE e.account_id=a.id) AS encounters,
                      EXISTS(SELECT 1 FROM latest_list l WHERE l.account_id=a.id) AS in_latest,
                      (SELECT l.pos FROM latest_list l WHERE l.account_id=a.id) AS pos,
                      f.relation AS relation
               FROM accounts a LEFT JOIN people p ON p.id=a.person_id
               LEFT JOIN friends f ON f.xuid=a.xuid AND f.owner=?"""
        args: list = [owner]
        if search.strip():
            like = f"%{search.strip().casefold()}%"
            q += (" WHERE a.gamertag_key LIKE ? OR IFNULL(a.xuid,'') LIKE ?"
                  " OR IFNULL(p.name,'') LIKE ? COLLATE NOCASE")
            args += [like, like, like]
        q += " ORDER BY a.last_seen DESC, pos IS NULL, pos ASC, a.id DESC"
        with self.lock:
            return self.conn.execute(q, args).fetchall()

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

    # ---- people (manual groups) ----
    def group_accounts(self, account_ids: List[int], name: str) -> int:
        name = name.strip()
        if not name or not account_ids:
            raise ValueError("A name and at least one account are required")
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id FROM people WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            pid = row[0] if row else self.conn.execute(
                "INSERT INTO people(name,created_at) VALUES(?,?)", (name, int(time.time()))).lastrowid
            self.conn.executemany("UPDATE accounts SET person_id=? WHERE id=?",
                                  [(pid, a) for a in account_ids])
        return pid

    def ungroup_accounts(self, account_ids: List[int]) -> None:
        with self.lock, self.conn:
            self.conn.executemany("UPDATE accounts SET person_id=NULL WHERE id=?",
                                  [(a,) for a in account_ids])
            self.conn.execute("DELETE FROM people WHERE id NOT IN "
                              "(SELECT person_id FROM accounts WHERE person_id IS NOT NULL)")

    # ---- export ----
    def export(self, path: Path) -> None:
        path = Path(path)
        rows = self.list_accounts()
        if path.suffix.lower() == ".json":
            data = [{k: r[k] for k in ("xuid", "gamertag", "person", "first_seen", "last_seen", "source")}
                    | {"encounters": [dict(e) for e in self.encounters_for(r["id"])]} for r in rows]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["gamertag", "xuid", "person", "first_seen", "last_seen", "source", "encounters"])
                for r in rows:
                    w.writerow([r["gamertag"], r["xuid"] or "", r["person"] or "", r["first_seen"],
                                r["last_seen"], r["source"], r["encounters"]])
