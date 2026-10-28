"""Shared Twitch list: one master list kept by the owner, downloaded by every copy of the app, plus suggestions that
users send in from the app and the owner approves or rejects.

Storage is a free Supabase project (Postgres + REST). Setup: docs/SHARED_LIST.md and docs/shared_list_setup.sql.

Two keys, two kinds of copy:
* the PUBLISHABLE key ships with the app (shared_service.json). The database itself only lets it READ the master list
  and ADD suggestions (row-level security, column grants, a trigger with limits). It can not read suggestions, change
  the master list or delete anything, so it being public is fine; that is what this kind of key is made for.
* the SECRET key exists only on the owner's PC (shared_admin.json in the app-data folder, never in the repository or
  the program file). A copy with that file shows the owner tools: review suggestions, publish, remove, block.
"""
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import __version__
from .providers import ProviderError
from .themes import asset_dir

SERVICE_FILE = "shared_service.json"     # {"url": "https://xxxx.supabase.co", "key": "sb_publishable_..."}
ADMIN_FILE = "shared_admin.json"         # {"secret_key": "sb_secret_..."}  owner's PC only
PAGE = 1000                              # rows per request when downloading
SEND_BATCH = 200                         # suggestions per request (the database also limits per sender per day)
MASTER_COLS = "xuid,gamertag,twitch_id,twitch_login,twitch_name"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


class SharedConfig:
    """Where the shared list lives. The data-dir file wins over the one bundled with the program."""

    def __init__(self, data: Path, bundled: Optional[Path] = None):
        self.data = Path(data)
        self.bundled = Path(bundled) if bundled else asset_dir() / SERVICE_FILE

    def service(self) -> Optional[dict]:
        for p in (self.data / SERVICE_FILE, self.bundled):
            d = _read(p)
            if d.get("url") and d.get("key"):
                return {"url": d["url"].rstrip("/"), "key": d["key"]}
        return None

    def save_service(self, url: str, key: str) -> None:
        (self.data / SERVICE_FILE).write_text(json.dumps({"url": url.strip().rstrip("/"), "key": key.strip()}),
                                              encoding="utf-8")

    def admin_key(self) -> Optional[str]:
        return _read(self.data / ADMIN_FILE).get("secret_key") or None

    def save_admin_key(self, key: Optional[str]) -> None:
        p = self.data / ADMIN_FILE
        if key:
            p.write_text(json.dumps({"secret_key": key.strip()}), encoding="utf-8")
        elif p.exists():
            p.unlink()


def http_rest(method: str, url: str, headers: dict, body=None, timeout: int = 20):
    """One REST call; returns the decoded JSON (or None for an empty answer)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": f"BetterRecentlyPlayedWith/{__version__}", "Accept": "application/json", **headers}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            pass
        err = ProviderError(f"Shared list: HTTP {e.code}. {detail}".strip(), reauth=e.code in (401, 403))
        err.code = e.code
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Shared list: network problem: {e}")


def _in(values) -> str:
    return "in.(" + ",".join(str(v) for v in values) + ")"      # only digits / ids reach this


class SharedClient:
    def __init__(self, url: str, key: str, http: Callable = http_rest):
        self.url, self.key, self.http = url.rstrip("/"), key, http

    def _headers(self, prefer: str = "") -> dict:
        h = {"apikey": self.key}
        if not self.key.startswith("sb_"):        # legacy JWT keys also go in Authorization; new sb_ keys must not
            h["Authorization"] = f"Bearer {self.key}"
        if prefer:
            h["Prefer"] = prefer
        return h

    def _call(self, method, table, params=(), body=None, prefer=""):
        q = urllib.parse.urlencode(list(params), safe="(),.*")
        return self.http(method, f"{self.url}/rest/v1/{table}" + (f"?{q}" if q else ""), self._headers(prefer), body)

    def _all(self, table, select, extra=(), order="id"):
        out, offset = [], 0
        while True:
            page = self._call("GET", table, [("select", select), *extra, ("order", order),
                                             ("limit", str(PAGE)), ("offset", str(offset))]) or []
            out += page
            if len(page) < PAGE:
                return out
            offset += PAGE

    # ---- everyone (publishable key)
    def master(self) -> List[dict]:
        return self._all("master_links", MASTER_COLS, order="xuid")

    def submit(self, rows: List[dict], submitter: str, name: str = "") -> int:
        """Send suggestions. The database drops ones it already has and refuses blocked senders / too many."""
        sent = 0
        for i in range(0, len(rows), SEND_BATCH):
            batch = [{"submitter": submitter, "submitter_name": (name or "")[:40], "xuid": r["xuid"],
                      "gamertag": r["gamertag"][:32], "twitch_id": r["twitch_id"],
                      "twitch_login": r["twitch_login"].lower(), "twitch_name": (r.get("twitch_name") or "")[:40]}
                     for r in rows[i:i + SEND_BATCH]]
            self._call("POST", "submissions", body=batch, prefer="return=minimal")
            sent += len(batch)
        return sent

    # ---- owner only (secret key)
    def pending(self) -> List[dict]:
        return self._all("submissions", "*", [("status", "eq.pending")], order="created_at")

    def sender_stats(self) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        for r in self._all("submissions", "submitter,status", order="id"):
            s = stats.setdefault(r["submitter"], {"pending": 0, "approved": 0, "rejected": 0})
            s[r["status"]] = s.get(r["status"], 0) + 1
        return stats

    def blocked(self) -> List[dict]:
        return self._all("blocked_submitters", "*", order="submitter")

    def publish(self, rows: List[dict]) -> int:
        """Add or replace master entries (one per XUID)."""
        for i in range(0, len(rows), SEND_BATCH):
            batch = [{k: r.get(k) or "" for k in MASTER_COLS.split(",")} for r in rows[i:i + SEND_BATCH]]
            self._call("POST", "master_links", [("on_conflict", "xuid")], batch,
                       prefer="resolution=merge-duplicates,return=minimal")
        return len(rows)

    def approve(self, subs: List[dict]) -> None:
        self.publish([{"xuid": s["xuid"], "gamertag": s["gamertag"], "twitch_id": s["twitch_id"],
                       "twitch_login": s["twitch_login"], "twitch_name": s.get("twitch_name")} for s in subs])
        self.set_status([s["id"] for s in subs], "approved")

    def set_status(self, ids: List[int], status: str) -> None:
        for i in range(0, len(ids), 100):
            self._call("PATCH", "submissions", [("id", _in(ids[i:i + 100]))], {"status": status},
                       prefer="return=minimal")

    def block(self, submitter: str, reason: str = "") -> None:
        self._call("POST", "blocked_submitters", [("on_conflict", "submitter")],
                   [{"submitter": submitter, "reason": reason[:100]}],
                   prefer="resolution=merge-duplicates,return=minimal")
        self._call("PATCH", "submissions", [("submitter", f"eq.{submitter}"), ("status", "eq.pending")],
                   {"status": "rejected"}, prefer="return=minimal")

    def unblock(self, submitter: str) -> None:
        self._call("DELETE", "blocked_submitters", [("submitter", f"eq.{submitter}")], prefer="return=minimal")

    def remove(self, xuids: List[str]) -> None:
        for i in range(0, len(xuids), 100):
            self._call("DELETE", "master_links", [("xuid", _in(xuids[i:i + 100]))], prefer="return=minimal")


def new_sender_id() -> str:
    """Random per-install ID so the owner can see which suggestions came from the same copy. Contains nothing else."""
    return secrets.token_hex(16)
