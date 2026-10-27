"""Optional Twitch section: exact live status for players whose Twitch channel the user has saved.

Nothing here guesses identities. The user pastes a Twitch name (or link) once per player; Twitch resolves it to a
numeric user ID, and that ID is what is stored and checked. Live status comes from Twitch's Get Streams (up to 100
channels per request). Twitch requires an application token, so the user enters their own Twitch client ID and secret
in the app; they are stored only in the per-user app-data folder, never in the repository or the executable.
See docs/TWITCH_RESEARCH.md.
"""
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .providers import ProviderError, _iso_to_epoch, http_json

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix/"
CHUNK = 100                   # Get Streams takes at most 100 user_id values per request
CHECK_EVERY = 120             # seconds between automatic checks while the app is open
STATUS_MAX_AGE = 10 * 60      # a Twitch status older than this is no longer shown as live
CONSOLE_URL = "https://dev.twitch.tv/console/apps"

_RESERVED = {"directory", "videos", "settings", "downloads", "jobs", "turbo", "p", "search", "friends", "subscriptions",
             "inventory", "drops", "wallet", "moderator", "popout", "embed", "u", "login", "signup"}


@dataclass
class TwitchUser:
    id: str
    login: str
    display_name: str


def parse_channel_input(text: str) -> str:
    """Accepts `name`, `@name`, `twitch.tv/name` or a full twitch.tv URL; returns the login/ID to look up."""
    t = (text or "").strip().lstrip("@").strip()
    m = re.fullmatch(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{1,25})(?:[/?#].*)?", t, re.I)
    if m:
        name = m.group(1)
        if name.lower() in _RESERVED:
            raise ValueError("That link is not a channel page.")
        return name
    if re.fullmatch(r"[A-Za-z0-9_]{3,25}", t):
        return t
    raise ValueError("That doesn't look like a Twitch name or channel link.")


def login_candidates(gamertag: str) -> List[str]:
    """Twitch logins that a gamertag could plausibly be: lower-case, spaces removed or turned into underscores,
    other characters dropped. Only valid Twitch logins (4-25 of a-z, 0-9, _) are returned. These are GUESSES."""
    g = (gamertag or "").strip().lower()
    out = []
    for v in (re.sub(r"\s+", "", g), re.sub(r"\s+", "_", g)):
        v = re.sub(r"[^a-z0-9_]", "", v)
        if re.fullmatch(r"[a-z0-9_]{4,25}", v) and v not in out:
            out.append(v)
    return out


class TwitchCreds:
    """Client ID + secret kept in a JSON file in the app-data folder (never in the database or the repo)."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> Optional[Tuple[str, str]]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            cid, sec = str(d["client_id"]).strip(), str(d["client_secret"]).strip()
            return (cid, sec) if cid and sec else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def configured(self) -> bool:
        return self.load() is not None

    def save(self, client_id: str, client_secret: str) -> None:
        cid, sec = (client_id or "").strip(), (client_secret or "").strip()
        if not cid or not sec:
            raise ValueError("Both the client ID and the client secret are required.")
        self.path.write_text(json.dumps({"client_id": cid, "client_secret": sec}), encoding="utf-8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class TwitchClient:
    def __init__(self, creds, http: Callable = http_json, now: Callable[[], float] = time.time):
        self.creds, self.http, self.now = creds, http, now
        self._token_value: Optional[str] = None
        self._expires = 0.0

    def token(self, force: bool = False) -> str:
        creds = self.creds.load()
        if not creds:
            raise ProviderError("Twitch is not set up. Use 'Twitch setup...' first.", reauth=True)
        if self._token_value and not force and self.now() < self._expires - 60:
            return self._token_value
        try:
            r = self.http(TOKEN_URL, {"client_id": creds[0], "client_secret": creds[1],
                                      "grant_type": "client_credentials"}, None, True)
        except ProviderError as e:
            if getattr(e, "code", 0) in (400, 401, 403):
                raise ProviderError("Twitch rejected the client ID or secret. Check them in 'Twitch setup...'.",
                                    reauth=True)
            raise
        if not isinstance(r, dict) or "access_token" not in r:
            raise ProviderError("Unexpected answer from Twitch when signing in.")
        self._token_value = r["access_token"]
        self._expires = self.now() + int(r.get("expires_in", 3600))
        return self._token_value

    def _get(self, path: str, params: List[Tuple[str, str]], _retry: bool = True) -> dict:
        creds = self.creds.load()
        if not creds:
            raise ProviderError("Twitch is not set up. Use 'Twitch setup...' first.", reauth=True)
        headers = {"Client-Id": creds[0], "Authorization": "Bearer " + self.token()}
        try:
            return self.http(API + path + "?" + urllib.parse.urlencode(params), None, headers)
        except ProviderError as e:
            code = getattr(e, "code", 0)
            if code == 401 and _retry:                    # token expired or revoked: get a new one once
                self.token(force=True)
                return self._get(path, params, _retry=False)
            if code == 429:
                raise ProviderError("Twitch rate limit reached; try again in a minute.")
            raise

    def resolve(self, text: str) -> TwitchUser:
        """Turn what the user typed into an exact Twitch user (ID, login, display name)."""
        name = parse_channel_input(text)
        data = self._get("users", [("login", name)]).get("data") or []
        if not data and name.isdigit():
            data = self._get("users", [("id", name)]).get("data") or []
        if not data:
            raise ProviderError(f"Twitch has no channel named '{name}'.")
        u = data[0]
        return TwitchUser(str(u["id"]), str(u["login"]), str(u.get("display_name") or u["login"]))

    def users_by_login(self, logins: List[str]) -> Dict[str, TwitchUser]:
        """login -> user for the logins that exist (100 per request)."""
        out: Dict[str, TwitchUser] = {}
        names = sorted(set(l.lower() for l in logins))
        for i in range(0, len(names), CHUNK):
            for u in self._get("users", [("login", n) for n in names[i:i + CHUNK]]).get("data") or []:
                out[str(u["login"]).lower()] = TwitchUser(str(u["id"]), str(u["login"]),
                                                          str(u.get("display_name") or u["login"]))
        return out

    def suggest(self, players: List[Tuple[int, str]]) -> List[dict]:
        """Possible Twitch channels for (account_id, gamertag) pairs, found only by NAME. Not confirmed identities:
        the caller must present them as guesses. Live status comes from Twitch itself."""
        cands = {aid: login_candidates(gt) for aid, gt in players}
        found = self.users_by_login([c for cs in cands.values() for c in cs])
        live = self.streams([u.id for u in found.values()]) if found else {}
        out = []
        for aid, gt in players:
            for c in cands[aid]:
                u = found.get(c)
                if u is None or any(o["account_id"] == aid and o["id"] == u.id for o in out):
                    continue
                s = live.get(u.id)
                out.append({"account_id": aid, "gamertag": gt, "id": u.id, "login": u.login, "name": u.display_name,
                            "live": bool(s), "game": s and s["game"], "title": s and s["title"],
                            "viewers": s and s["viewers"],
                            "sot": bool(s) and "sea of thieves" in (s["game"] or "").casefold()})
        return out

    def streams(self, user_ids: List[str]) -> Dict[str, dict]:
        """user_id -> stream details, for those that are live right now (100 IDs per request)."""
        out: Dict[str, dict] = {}
        ids = sorted(set(user_ids))
        for i in range(0, len(ids), CHUNK):
            params = [("user_id", x) for x in ids[i:i + CHUNK]] + [("first", str(CHUNK))]
            for s in self._get("streams", params).get("data") or []:
                if s.get("type", "live") != "live":
                    continue
                out[str(s["user_id"])] = {
                    "title": str(s.get("title") or ""), "game": str(s.get("game_name") or ""),
                    "viewers": int(s.get("viewer_count") or 0), "started": _iso_to_epoch(s.get("started_at")),
                    "login": str(s.get("user_login") or "")}
        return out


class TwitchChecker:
    """Checks the saved Twitch links of the local players and stores the result."""

    def __init__(self, db, client: TwitchClient, now: Callable[[], float] = time.time):
        self.db, self.client, self.now = db, client, now

    def check_accounts(self, account_ids: Optional[List[int]] = None) -> Dict[int, dict]:
        links = self.db.twitch_links(account_ids)
        if not links:
            return {}
        live = self.client.streams([l["twitch_id"] for l in links])
        t = int(self.now())
        rows, out = [], {}
        for l in links:
            s = live.get(l["twitch_id"])
            rows.append((l["account_id"], t, 1 if s else 0, s and s["title"], s and s["game"], s and s["viewers"],
                         s and s["started"]))
            out[l["account_id"]] = {"live": bool(s), "login": l["login"], "name": l["display_name"],
                                    **({"title": s["title"], "game": s["game"], "viewers": s["viewers"],
                                        "started": s["started"]} if s else {})}
        self.db.store_twitch_status(rows)
        return out
