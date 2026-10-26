"""Data-source providers. Add a new source by implementing RecentPlayersProvider."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

from .db import Player

UA = "BetterRecentlyPlayedWith"


class ProviderError(Exception):
    """User-presentable failure. `reauth` means the user must sign in again."""

    def __init__(self, message: str, reauth: bool = False):
        super().__init__(message)
        self.reauth = reauth


class RecentPlayersProvider:
    name = "base"
    live = False  # True only for a source that reflects the current Xbox list

    def fetch(self) -> List[Player]:
        raise NotImplementedError


# ---------------------------------------------------------------- manual import
def parse_import(text: str) -> List[Player]:
    """Accepts JSON (list of strings/objects), or lines: `gamertag` or `gamertag,xuid` (CSV/TSV)."""
    text = text.strip()
    players: List[Player] = []
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"Invalid JSON: {e}")
        if isinstance(data, dict):
            data = data.get("people") or data.get("players") or []
        for item in data:
            if isinstance(item, str):
                players.append(Player(item))
            elif isinstance(item, dict) and (item.get("gamertag") or item.get("xuid")):
                ts = item.get("observed_at") or item.get("last_seen")
                players.append(Player(str(item.get("gamertag") or ""), _xuid(item.get("xuid")),
                                      int(ts) if isinstance(ts, (int, float)) else None))
        return players
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("gamertag"):
            continue
        parts = [p.strip().strip('"') for p in re.split(r"[,\t;]", line)]
        xuid = _xuid(parts[1]) if len(parts) > 1 else None
        players.append(Player(parts[0], xuid))
    return players


def _xuid(v) -> Optional[str]:
    s = str(v).strip() if v not in (None, "") else ""
    return s if s.isdigit() else None


class ManualImportProvider(RecentPlayersProvider):
    name = "import"
    live = False

    def __init__(self, text: str):
        self.text = text

    def fetch(self) -> List[Player]:
        players = parse_import(self.text)
        if not players:
            raise ProviderError("No players found in the imported text.")
        return players


# ---------------------------------------------------------------- HTTP helper
Http = Callable[[str, Optional[dict], Optional[dict], bool], dict]


def http_json(url: str, data: Optional[dict] = None, headers: Optional[dict] = None,
              form: bool = False, timeout: int = 20) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        err = ProviderError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}. {detail}".strip(),
                            reauth=e.code in (401, 403))
        err.code = e.code
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Network problem reaching {urllib.parse.urlparse(url).netloc}: {e}")


# ---------------------------------------------------------------- OpenXBL (documented third-party API)
OPENXBL_URL = "https://api.xbl.io/v2/recent-players"  # https://api.xbl.io/openapi.json


def _iso_to_epoch(s) -> Optional[int]:
    if not isinstance(s, str):
        return None
    from datetime import datetime, timezone
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?", s.strip())
    if not m:
        return None
    dt = datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(2) in (None, "Z") else m.group(2)))
    return int(dt.astimezone(timezone.utc).timestamp())


def _find_player_list(node, depth: int = 0):
    """First list of dicts that look like players (have gamertag/xuid), searched to depth 4."""
    if isinstance(node, list):
        if node and all(isinstance(i, dict) for i in node) and any(("gamertag" in i or "xuid" in i) for i in node):
            return node
        return None
    if isinstance(node, dict) and depth < 4:
        for v in node.values():
            found = _find_player_list(v, depth + 1)
            if found is not None:
                return found
    return None


def _shape(node, depth: int = 0) -> str:
    """Keys/types only, never values (except short text under message-like keys). For error reports."""
    if isinstance(node, dict):
        if depth >= 3:
            return "{...}"
        parts = []
        for k, v in list(node.items())[:12]:
            if k in ("message", "error", "description", "code", "status") and isinstance(v, (str, int)):
                parts.append(f"{k}={str(v)[:80]!r}")
            else:
                parts.append(f"{k}: {_shape(v, depth + 1)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(node, list):
        return f"list[{len(node)}]" + (f" of {_shape(node[0], depth + 1)}" if node else "")
    return type(node).__name__


def openxbl_stats(payload) -> str:
    """Counts only (no names): how much of the response is usable. Shown in the status bar."""
    items = _find_player_list(payload) or []
    titles = sum(1 for i in items if i.get("titleName"))
    stamps = sum(1 for i in items if _iso_to_epoch(i.get("timestamp")))
    sot = sum(1 for i in items if "sea of thieves" in str(i.get("titleName", "")).casefold())
    return f"received {len(items)}, with game name {titles}, with time {stamps}, Sea of Thieves {sot}"


def parse_openxbl(payload: dict) -> List[Player]:
    """Filters to Sea of Thieves when entries carry titleName; keeps source timestamps."""
    items = _find_player_list(payload)
    if items is None:
        raise ProviderError(f"Unexpected response from OpenXBL. Structure: {_shape(payload)}")
    has_titles = any(isinstance(i, dict) and i.get("titleName") for i in items)
    out: List[Player] = []
    for i in items:
        if not isinstance(i, dict) or not (i.get("gamertag") or i.get("xuid")):
            continue
        if has_titles and "sea of thieves" not in str(i.get("titleName", "")).casefold():
            continue
        out.append(Player(str(i.get("gamertag") or ""), _xuid(i.get("xuid")), _iso_to_epoch(i.get("timestamp"))))
    return out


OPENXBL_ACCOUNT_URL = "https://api.xbl.io/v2/account"


def parse_account_gamertag(payload) -> Optional[str]:
    try:
        for s in payload["content"]["profileUsers"][0]["settings"]:
            if s.get("id") == "Gamertag" and s.get("value"):
                return str(s["value"])
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return None


def lookup_gamertag(key: str, http=http_json) -> Optional[str]:
    """Raises ProviderError if the key is rejected; returns None if the name can't be read."""
    try:
        return parse_account_gamertag(http(OPENXBL_ACCOUNT_URL, None, {"X-Authorization": key.strip()}))
    except ProviderError as e:
        if getattr(e, "code", 0) == 401:
            raise ProviderError("OpenXBL rejected that key.")
        raise


class OpenXblKey:
    """Several OpenXBL API keys (one per Xbox account) with one active, kept in a JSON file in the
    app-data folder (never in the player database). get() returns the active key."""

    def __init__(self, path: Path):
        self.path = Path(path)  # openxbl_keys.json
        self.legacy = self.path.with_name("openxbl.key")
        self._data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d.get("accounts"), list):
                return d
        except (OSError, ValueError, AttributeError):
            pass
        d = {"active": None, "accounts": []}
        try:  # migrate the single-key file of v0.1.0
            old = self.legacy.read_text(encoding="utf-8").strip()
            if old:
                d = {"active": "Account 1", "accounts": [{"name": "Account 1", "key": old}]}
        except OSError:
            pass
        return d

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data), encoding="utf-8")

    def names(self) -> List[str]:
        return [a["name"] for a in self._data["accounts"]]

    def active(self) -> Optional[str]:
        return self._data["active"] if self._data["active"] in self.names() else None

    def get(self) -> str:
        for a in self._data["accounts"]:
            if a["name"] == self.active():
                return a["key"]
        return ""

    def add(self, name: str, key: str) -> str:
        key, base = key.strip(), (name.strip() or "Account")
        for a in self._data["accounts"]:
            if a["key"] == key:  # same key again: just switch to it
                self._data["active"] = a["name"]
                self._save()
                return a["name"]
        name, n = base, 2
        while name in self.names():
            name, n = f"{base} ({n})", n + 1
        self._data["accounts"].append({"name": name, "key": key})
        self._data["active"] = name
        self._save()
        return name

    def set_active(self, name: str) -> None:
        if name in self.names():
            self._data["active"] = name
            self._save()

    def remove(self, name: str) -> None:
        self._data["accounts"] = [a for a in self._data["accounts"] if a["name"] != name]
        if self._data["active"] == name:
            self._data["active"] = self.names()[0] if self.names() else None
        self._save()
        if not self._data["accounts"]:
            self.legacy.unlink(missing_ok=True)


OPENXBL_FRIENDS_URL = "https://api.xbl.io/v2/friends"
FRIENDS_REFRESH = 30 * 60


def parse_friends(payload) -> dict:
    """xuid -> relation. 'Friend' = you follow them and they follow you back; otherwise one-directional.
    If OpenXBL omits the follow flags the person is only known to be 'In your list'."""
    people = (payload.get("content") or {}).get("people") if isinstance(payload, dict) else None
    if not isinstance(people, list):
        raise ProviderError("Unexpected friends response from OpenXBL.")
    out = {}
    for p in people:
        x = _xuid(p.get("xuid")) if isinstance(p, dict) else None
        if not x:
            continue
        mine, theirs = p.get("isFollowedByCaller"), p.get("isFollowingCaller")
        if mine is None and theirs is None:
            out[x] = "In your list"
        elif mine and theirs:
            out[x] = "Friend"
        elif mine:
            out[x] = "You follow"
        else:
            out[x] = "Follows you"
    return out


class OpenXblProvider(RecentPlayersProvider):
    name = "openxbl"
    live = True
    notes = ""

    def __init__(self, key: OpenXblKey, http=http_json):
        self.key, self.http = key, http

    def fetch(self) -> List[Player]:
        k = self.key.get()
        if not k:
            raise ProviderError("No OpenXBL key set.", reauth=True)
        try:
            payload = self.http(OPENXBL_URL, None, {"X-Authorization": k})
        except ProviderError as e:
            code = getattr(e, "code", 0)
            if code == 401:
                raise ProviderError("OpenXBL rejected the key. Check it or sign in at xbl.io again.", reauth=True)
            if code == 429:
                raise ProviderError("OpenXBL rate limit reached (free tier: 150 requests/hour). Try a longer interval.")
            raise
        self.notes = openxbl_stats(payload)
        return parse_openxbl(payload)

    def post_sync(self, db) -> None:
        """Refresh the friends list for the active account (1 request, at most every 30 min). Failures here
        never fail the player sync."""
        owner, key = self.key.active(), self.key.get()
        if not owner or not key:
            return
        last = db.get_setting("friends_at:" + owner)
        if last and time.time() - int(last) < FRIENDS_REFRESH:
            return
        db.replace_friends(owner, parse_friends(self.http(OPENXBL_FRIENDS_URL, None, {"X-Authorization": key})))