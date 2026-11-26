"""Xbox's own services for accounts signed in with Microsoft (see msauth.py), instead of OpenXBL.

The rest of the app speaks OpenXBL: it builds api.xbl.io URLs and parses OpenXBL-shaped answers. For an account whose
"key" is a Microsoft sign-in (msauth.MARK + XUID), openxbl_json / social.openxbl_call hand the request to call() here,
which asks the matching Xbox service and returns the answer in the shape OpenXBL would have returned. OpenXBL is a thin
layer over these same services, so most answers are identical; recent players gain the game and the time.

Shapes checked against a real account: peoplehub (recent players, friends, people by XUID, search), userpresence,
profile, xblmessaging (inbox, conversation). Adding/removing friends uses the service the Xbox app uses for that.
Sending messages uses Microsoft's documented outbox (see call()).
"""
import base64
import copy
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


def _session(handle: dict, auth: str) -> dict:
    """The multiplayer session an activity handle points to (sessiondirectory, documented)."""
    ref = handle.get("sessionRef") if isinstance(handle.get("sessionRef"), dict) else {}
    parts = [str(ref.get(k) or "") for k in ("scid", "templateName", "name")]
    if not all(p and all(ch.isalnum() or ch in "-_" for ch in p) for p in parts):
        raise ProviderError("Unexpected session reference from Xbox.")
    return _request("GET", "https://sessiondirectory.xboxlive.com/serviceconfigs/{}/sessionTemplates/{}/sessions/{}"
                    .format(*parts), auth, "107")


def crew_target(doc: dict) -> Optional[tuple]:
    """(template, name) of the ship session a Sea of Thieves lobby points to. Seen for real 2026-11-25: the activity
    handle leads to a per-player session (custom SessionTarget "CrewSession") that lists only that player; its custom
    TargetSessionHandle is base64 of "template;session name;..." for the session with the whole crew."""
    custom = ((doc.get("properties") or {}).get("custom") or {}) if isinstance(doc, dict) else {}
    raw = custom.get("TargetSessionHandle") if isinstance(custom, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parts = base64.b64decode(raw, validate=True).decode("utf-8").split(";")
    except (ValueError, UnicodeDecodeError):
        return None
    if len(parts) < 2 or not all(p and all(ch.isalnum() or ch in "-_" for ch in p) for p in parts[:2]):
        return None
    return parts[0], parts[1]


def _ship(handle: dict, auth: str) -> dict:
    """The session with everyone aboard for an activity handle: the crew session its lobby points to when there is
    one (else the lobby itself). The handle's member count is corrected to the crew's."""
    return _ship_and_url(handle, auth)[0]


def _counts(handle: dict, crew: dict) -> None:
    """Correct the handle's member count and size to the crew session's."""
    info = handle.get("relatedInfo") if isinstance(handle.get("relatedInfo"), dict) else None
    if info is not None:
        count = (crew.get("membersInfo") or {}).get("count")
        most = ((crew.get("constants") or {}).get("system") or {}).get("maxMembersCount")
        if isinstance(count, int):
            info["membersCount"] = count
        if isinstance(most, int) and most:
            info["maxMembersCount"] = most


def _ship_and_url(handle: dict, auth: str):
    """(_ship's session, the crew session's address or None when that isn't the crew session)."""
    doc = _session(handle, auth)
    target = crew_target(doc)
    ref = handle.get("sessionRef") or {}
    scid = str(ref.get("scid") or "")
    # Only a session for the same kind of ship (seen: both SmallShipSessionTemplate for a sloop). Anything else,
    # like a session that could hold another crew too (Hourglass opponents), is never read as your crew.
    # (The session list writes template names in lower case, handles don't: compared without case.)
    if not target or target[0].lower() != str(ref.get("templateName") or "").lower():
        return doc, None
    url = "https://sessiondirectory.xboxlive.com/serviceconfigs/{}/sessionTemplates/{}/sessions/{}".format(scid, *target)
    try:
        crew = _request("GET", url, auth, "107")
    except ProviderError:
        return doc, None
    most_lobby = ((doc.get("constants") or {}).get("system") or {}).get("maxMembersCount")
    most_crew = ((crew.get("constants") or {}).get("system") or {}).get("maxMembersCount")
    if not isinstance(most_crew, int) or (isinstance(most_lobby, int) and most_crew > most_lobby) or most_crew > 4:
        return doc, None                                   # bigger than your ship: not just your crew
    _counts(handle, crew)
    return crew, url


# Your last crew session per account: Sea of Thieves stops publishing your ship to Xbox partway through a voyage (seen
# 2026-11-25: the activity handle was gone for 10+ minutes while the same crew sailed on). As a member you can still
# read the session itself, so it is read directly while you are in Sea of Thieves and still listed in it.
_last_crew: dict = {}


def _in_sot(presence) -> bool:
    from .social import SOT_TITLE_ID
    devices = presence.get("devices") if isinstance(presence, dict) else None
    return any(isinstance(t, dict) and str(t.get("id")) == SOT_TITLE_ID
               for d in devices or [] if isinstance(d, dict) for t in d.get("titles") or [])


def _remembered_crew(me: str, auth: str) -> Optional[dict]:
    with _lock:
        saved = _last_crew.get(me)
    if not saved:
        return None
    handle, url = saved
    try:
        playing = _in_sot(_request("GET", f"https://userpresence.xboxlive.com/users/xuid({me})?level=all", auth, "3"))
        crew = _request("GET", url, auth, "107") if playing else None
    except ProviderError:
        crew = None
    members = (crew or {}).get("members")
    aboard = isinstance(members, dict) and any(
        isinstance(m, dict) and str(((m.get("constants") or {}).get("system") or {}).get("xuid")) == me
        for m in members.values())
    if not aboard:                                         # left the game or the ship: forget it
        with _lock:
            _last_crew.pop(me, None)
        return None
    handle = copy.deepcopy(handle)
    _counts(handle, crew)
    return {"handle": handle, "session": crew}


def _listed_lobby(me: str, auth: str) -> Optional[dict]:
    """An activity-handle-like entry for your lobby, found in the list of sessions you're in. Seen for real 2026-11-26
    in Hourglass: no activity handle at all, but sessions?xuid=me&private=true lists your lobby (pointing to the crew
    session) and the crew session itself."""
    from .social import SOT_SCID, SOT_TITLE_ID
    base = f"https://sessiondirectory.xboxlive.com/serviceconfigs/{SOT_SCID}"
    listed = _request("GET", f"{base}/sessions?xuid={me}&private=true", auth, "107").get("results") or []
    for r in listed[:6]:
        ref = r.get("sessionRef") if isinstance(r, dict) else None
        if not isinstance(ref, dict) or str(ref.get("scid") or "").lower() != SOT_SCID:
            continue
        handle = {"id": str(ref.get("name") or ""), "ownerXuid": me, "titleId": SOT_TITLE_ID, "sessionRef": ref,
                  "relatedInfo": {}}
        try:
            lobby = _session(handle, auth)
        except ProviderError:
            continue
        if crew_target(lobby):                             # the lobby, not the crew session itself
            most = ((lobby.get("constants") or {}).get("system") or {}).get("maxMembersCount")
            handle["relatedInfo"] = {"membersCount": 1, "maxMembersCount": most if isinstance(most, int) else 0}
            return handle
    return None


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
            msg = f"Xbox refused the request (HTTP {e.code}). If it keeps happening, sign in again. {detail}".strip()
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
                    "timestamp": t.get("lastPlayedWithDateTime"), "displayPicRaw": p.get("displayPicRaw")})
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
        # Xbox's friend system keeps real friends (a request accepted) in /friends; /social only has who follows
        # whom. Seen for real 2026-11-23: 113 friends there, 12 follows (none of them friends) in /social. Both,
        # friends first, each person once.
        friends = _request("GET", PEOPLEHUB + "/friends/decoration/presencedetail", auth, "7")
        follows = _request("GET", PEOPLEHUB + "/social/decoration/presencedetail", auth, "7")
        people, seen = [], set()
        for p in (friends.get("people") or []) + (follows.get("people") or []):
            if isinstance(p, dict) and p.get("xuid") not in seen:
                seen.add(p.get("xuid"))
                people.append(p)
        return {"content": {"people": people}}
    if m == "GET" and path == "sessions/sot":
        # Xbox multiplayer sessions (sessiondirectory, documented): Sea of Thieves activity handles, the "Join game"
        # information. Needs the game's service config ID, else Xbox answers 403.
        from .social import SOT_SCID
        url = "https://sessiondirectory.xboxlive.com/handles/query?include=relatedInfo"
        results, seen = [], set()
        for moniker in ("friends", "people"):           # friends (Xbox's friend system) and who you follow
            j = _request("POST", url, auth, "107", {"type": "activity", "scid": SOT_SCID, "owners": {
                "people": {"moniker": moniker, "monikerXuid": tok["xuid"]}}})
            for r in j.get("results") or []:
                if isinstance(r, dict) and r.get("id") not in seen:
                    seen.add(r.get("id"))
                    results.append(r)
        for r in results[:12]:                          # who is aboard (sessions friends may read)
            try:
                r["members"] = (_ship(r, auth).get("members") or {})
            except ProviderError:
                pass
        return {"content": {"results": results}}
    if m == "GET" and path == "sessions/sot/crew":
        # your own ship: your activity handle, then that session's member list (seen for real: members by index,
        # each with gamertag and constants.system.xuid)
        from .social import SOT_SCID
        j = _request("POST", "https://sessiondirectory.xboxlive.com/handles/query?include=relatedInfo", auth, "107",
                     {"type": "activity", "scid": SOT_SCID, "owners": {"xuids": [tok["xuid"]]}})
        mine = [r for r in j.get("results") or [] if isinstance(r, dict) and str(r.get("ownerXuid")) == tok["xuid"]]
        if not mine:
            remembered = _remembered_crew(tok["xuid"], auth)
            if remembered:
                return {"content": remembered}
            try:
                lobby = _listed_lobby(tok["xuid"], auth)
            except ProviderError:
                lobby = None
            if not lobby:
                return {"content": None}
            mine = [lobby]
        doc, url = _ship_and_url(mine[0], auth)
        with _lock:
            if url:
                _last_crew[tok["xuid"]] = (copy.deepcopy(mine[0]), url)
            else:
                _last_crew.pop(tok["xuid"], None)
        return {"content": {"handle": mine[0], "session": doc}}
    if m == "GET" and path == "friends/requests":
        return {"content": {side: _request("GET", f"{PEOPLEHUB}/friendrequests({side})/decoration/presencedetail",
                                           auth, "7") for side in ("received", "sent")}}
    if m == "POST" and path == "friends/requests/accept" and isinstance(data, dict):
        # as the Xbox app's social service takes it (bulk add, contract 3); seen in an open-source Xbox client
        x = str(data.get("xuid") or "")
        if not x.isdigit():
            raise ProviderError("No XUID for the friend request.")
        _request("POST", "https://social.xboxlive.com/bulk/users/me/people/friends/v2?method=add", auth, "3",
                 {"xuids": [x]})
        return {}
    if m == "GET" and path.startswith("player/summary/"):
        xuids = [x for x in path.split("/", 2)[2].split(",") if x.isdigit()]
        return {"content": _request("GET", f"{PEOPLEHUB}/xuids({','.join(xuids)})/decoration/{SUMMARY_DECORATIONS}",
                                    auth, "5")}
    if m == "GET" and path == "presence":
        return {"content": _request("GET", "https://userpresence.xboxlive.com/users/me?level=all", auth, "3")}
    if m == "GET" and re.fullmatch(r"presence/\d+", path):
        x = path.split("/")[1]
        return {"content": _request("GET", f"https://userpresence.xboxlive.com/users/xuid({x})?level=all", auth, "3")}
    if m == "GET" and path.startswith("search/"):
        term = path.split("/", 1)[1]
        return {"content": _request("GET", f"{PEOPLEHUB}/search?q={urllib.parse.quote(term)}&maxItems=15", auth, "5")}
    if m == "GET" and path == "account":
        return {"content": _request("GET", "https://profile.xboxlive.com/users/me/profile/settings"
                                    "?settings=Gamertag,GameDisplayPicRaw",
                                    auth, "2")}
    if m == "PUT" and re.fullmatch(r"friends/\d+", path):
        # Xbox's friend system: the same bulk add as accepting sends a friend request when none is waiting. The old
        # per-person PUT on social.xboxlive.com only follows and no longer makes anyone a friend.
        _request("POST", "https://social.xboxlive.com/bulk/users/me/people/friends/v2?method=add", auth, "3",
                 {"xuids": [path.split("/")[1]]})
        return {}
    if m == "DELETE" and re.fullmatch(r"friends/\d+", path):
        target = path.split("/")[1]
        _request(m, f"https://social.xboxlive.com/users/xuid({tok['xuid']})/people/xuid({target})", auth, "2")
        return {}
    if m == "GET" and re.fullmatch(r"messages/\d+", path):
        x = path.split("/")[1]
        return {"content": _request("GET", f"{MESSAGING}/conversations/users/xuid({x})?maxItems=100", auth, "1")}
    if m == "GET" and path in ("conversations", "messages"):
        return {"content": _request("GET", f"{MESSAGING}/inbox", auth, "1")}
    if m == "POST" and path == "conversations" and isinstance(data, dict):
        # Microsoft's documented send API (msg.xboxlive.com outbox). The messaging service the Xbox app uses
        # (xblmessaging) answers with a message ID but never delivers messages from this kind of sign-in; the
        # outbox does (tested for real 2026-11-15: the message appeared in the conversation).
        x = str(data.get("xuid") or "")
        if not x.isdigit():
            raise ProviderError("No XUID for the message.")
        _request("POST", f"https://msg.xboxlive.com/users/xuid({tok['xuid']})/outbox", auth, "3",
                 {"header": {"recipients": [{"xuid": int(x)}]}, "messageText": str(data.get("message") or "")})
        return {}
    if m == "POST" and path == "stats/sot-minutes" and isinstance(data, dict):
        xuids = [str(x) for x in data.get("xuids") or [] if str(x).isdigit()]
        return {"content": _request("POST", "https://userstats.xboxlive.com/batch", auth, "2",
                                    {"arrangebyfield": "xuid", "xuids": xuids,
                                     "stats": [{"name": "MinutesPlayed", "titleid": "1717113201"}]})}
    # conversation actions; mute and message delete found by trying them on the user's own chats (2026-11-23)
    if m in ("PUT", "DELETE") and re.fullmatch(r"conversations/[A-Za-z0-9-]+/mute", path):
        _request(m, f"{MESSAGING}/{path}", auth, "1")
        return {}
    if m == "DELETE" and re.fullmatch(r"conversations/[A-Za-z0-9-]+/messages/\d+", path):
        _request("DELETE", f"{MESSAGING}/{path}", auth, "1")
        return {}
    if m == "PUT" and path == "conversations/horizon" and isinstance(data, dict):     # as xbox-webapi-python does
        _request("PUT", f"{MESSAGING}/conversations/horizon", auth, "2", data)
        return {}
    raise ProviderError(f"'{path}' isn't available with Microsoft sign-in yet.")
