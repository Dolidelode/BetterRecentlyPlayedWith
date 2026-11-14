"""Xbox's own services for accounts signed in with Microsoft (see msauth.py), instead of OpenXBL.

The rest of the app speaks OpenXBL: it builds api.xbl.io URLs and parses OpenXBL-shaped answers. For an account whose
"key" is a Microsoft sign-in (msauth.MARK + XUID), openxbl_json / social.openxbl_call hand the request to call() here,
which asks the matching Xbox service and returns the answer in the shape OpenXBL would have returned. OpenXBL is a thin
layer over these same services, so most answers are identical; recent players gain the game and the time.

Shapes checked against a real account: peoplehub (recent players, friends, people by XUID, search), userpresence,
profile, xblmessaging (inbox, conversation). Adding/removing friends and sending messages use the services the Xbox
app uses for that.
"""
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .providers import UA, ProviderError

OPENXBL = "https://api.xbl.io/v2/"
PEOPLEHUB = "https://peoplehub.xboxlive.com/users/me/people"
MESSAGING = "https://xblmessaging.xboxlive.com/network/Xbox/users/me"
SUMMARY_DECORATIONS = "detail,presenceDetail,multiplayerSummary"
SOT = "sea of thieves"

_accounts = None                 # msauth.MsAccounts, set by the app at start (configure)
_lock = threading.Lock()


def configure(accounts) -> None:
    global _accounts
    with _lock:
        _accounts = accounts


def is_ms_key(key: Optional[str]) -> bool:
    from .msauth import MARK
    return bool(key) and str(key).strip().startswith(MARK)


def _request(method: str, url: str, auth: str, version: str, data=None, timeout: int = 20) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US", "Authorization": auth,
         "x-xbl-contract-version": version}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code == 429:
            msg = "Xbox is rate limiting this account right now. Wait a few minutes; the app retries by itself."
        elif e.code in (401, 403):
            msg = f"Xbox refused the request (HTTP {e.code}). If it keeps happening, sign in again."
        else:
            msg = f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}. {detail}".strip()
        err = ProviderError(msg, reauth=e.code == 401)
        err.code = e.code
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Network problem reaching Xbox: {e}")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def flatten_recent(payload: dict) -> list:
    """peoplehub recent players -> OpenXBL-like entries with titleName and timestamp (Sea of Thieves preferred)."""
    out = []
    for p in (payload or {}).get("people") or []:
        if not isinstance(p, dict) or not p.get("xuid"):
            continue
        titles = [t for t in ((p.get("recentPlayer") or {}).get("titles") or []) if isinstance(t, dict)]
        t = next((t for t in titles if SOT in str(t.get("titleName", "")).casefold()), titles[0] if titles else {})
        out.append({"xuid": str(p["xuid"]), "gamertag": p.get("gamertag") or p.get("modernGamertag") or "",
                    "titleName": t.get("titleName"), "titleId": t.get("titleId"),
                    "timestamp": t.get("lastPlayedWithDateTime")})
    return out


def call(method: str, url: str, key: str, data: Optional[dict] = None) -> dict:
    """Answer an OpenXBL request for a Microsoft sign-in account."""
    from .msauth import MARK
    if _accounts is None:
        raise ProviderError("Microsoft sign-in isn't set up in this window.")
    xuid = str(key).strip()[len(MARK):]
    tok = _accounts.auth(xuid)
    auth = tok["header"]
    if not url.startswith(OPENXBL):
        raise ProviderError("Not an Xbox request.")
    u = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(u.path[len("/v2/"):]).strip("/")
    q = urllib.parse.parse_qs(u.query)
    m = method.upper()

    if m == "GET" and path == "recent-players":
        return {"content": {"people": flatten_recent(_request("GET", PEOPLEHUB + "/recentplayers", auth, "5"))}}
    if m == "GET" and path == "friends" and not q.get("xuid"):
        return {"content": _request("GET", PEOPLEHUB + "/social/decoration/presencedetail", auth, "5")}
    if m == "GET" and path.startswith("player/summary/"):
        xuids = [x for x in path.split("/", 2)[2].split(",") if x.isdigit()]
        return {"content": _request("GET", f"{PEOPLEHUB}/xuids({','.join(xuids)})/decoration/{SUMMARY_DECORATIONS}",
                                    auth, "5")}
    if m == "GET" and re.fullmatch(r"presence/\d+", path):
        x = path.split("/")[1]
        return {"content": _request("GET", f"https://userpresence.xboxlive.com/users/xuid({x})?level=all", auth, "3")}
    if m == "GET" and path.startswith("search/"):
        term = path.split("/", 1)[1]
        return {"content": _request("GET", f"{PEOPLEHUB}/search?q={urllib.parse.quote(term)}&maxItems=15", auth, "5")}
    if m == "GET" and path == "account":
        return {"content": _request("GET", "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag",
                                    auth, "2")}
    if m in ("PUT", "DELETE") and re.fullmatch(r"friends/\d+", path):
        target = path.split("/")[1]
        _request(m, f"https://social.xboxlive.com/users/xuid({tok['xuid']})/people/xuid({target})", auth, "2")
        return {}
    if m == "GET" and re.fullmatch(r"messages/\d+", path):
        x = path.split("/")[1]
        return {"content": _request("GET", f"{MESSAGING}/conversations/users/xuid({x})?maxItems=100", auth, "1")}
    if m == "GET" and path in ("conversations", "messages"):
        return {"content": _request("GET", f"{MESSAGING}/inbox", auth, "1")}
    if m == "POST" and path == "conversations" and isinstance(data, dict):
        x = str(data.get("xuid") or "")
        if not x.isdigit():
            raise ProviderError("No XUID for the message.")
        _request("POST", f"{MESSAGING}/conversations/users/xuid({x})", auth, "1",
                 {"parts": [{"contentType": "text", "version": 0, "text": str(data.get("message") or "")}]})
        return {}
    raise ProviderError(f"'{path}' isn't available with Microsoft sign-in yet.")
