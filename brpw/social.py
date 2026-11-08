"""Xbox social actions through OpenXBL (https://api.xbl.io/docs): add or remove a friend, send a message, and the
full friends list with what each friend is doing.

Endpoints, from OpenXBL's own OpenAPI document:
* PUT    /v2/friends/{xuid}   add a friend (follow them)
* DELETE /v2/friends/{xuid}   remove a friend
* GET    /v2/friends          your friends, with presenceState / presenceText
* POST   /v2/conversations    {"xuid": ..., "message": ...} sends an Xbox message

There is no endpoint to invite someone into a game or to join someone's game, so the app does not offer that.
Every action here is done only when the user clicks for it; nothing is sent automatically.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

from .providers import UA, ProviderError, _iso_to_epoch, _xuid, raise_for_body

API = "https://api.xbl.io/v2/"
FRIENDS_URL = API + "friends"
MESSAGE_URL = API + "conversations"
MAX_MESSAGE = 500          # keep messages short; Xbox rejects very long ones


def openxbl_call(method: str, url: str, key: str, data: Optional[dict] = None, timeout: int = 20) -> dict:
    """One OpenXBL request with any HTTP method. Errors become ProviderError (with .code when there is one)."""
    h = {"User-Agent": UA, "Accept": "application/json", "X-Authorization": key.strip()}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        err = ProviderError(f"HTTP {e.code} from OpenXBL. {detail}".strip(), reauth=e.code in (401, 403))
        err.code = e.code
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Network problem reaching OpenXBL: {e}")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}                                   # some actions answer with plain text
    return raise_for_body(payload)


Call = Optional[Callable[..., dict]]          # None = the real request (looked up at call time, so tests can swap it)


def _check_xuid(xuid) -> str:
    x = _xuid(xuid)
    if not x:
        raise ProviderError("This player has no XUID (imported by name only), so Xbox can't be asked.")
    return x


def add_friend(key: str, xuid, call: Call = None) -> None:
    (call or openxbl_call)("PUT", FRIENDS_URL + "/" + _check_xuid(xuid), key)


def remove_friend(key: str, xuid, call: Call = None) -> None:
    (call or openxbl_call)("DELETE", FRIENDS_URL + "/" + _check_xuid(xuid), key)


def send_message(key: str, xuid, text: str, call: Call = None) -> None:
    text = (text or "").strip()
    if not text:
        raise ProviderError("The message is empty.")
    if len(text) > MAX_MESSAGE:
        raise ProviderError(f"The message is too long ({len(text)} characters, at most {MAX_MESSAGE}).")
    (call or openxbl_call)("POST", MESSAGE_URL, key, {"xuid": _check_xuid(xuid), "message": text})


def parse_friend_list(payload) -> List[dict]:
    """Friends with what they are doing, online first. Only fields OpenXBL actually returns are used."""
    content = payload.get("content") if isinstance(payload, dict) else None
    people = content.get("people") if isinstance(content, dict) else None
    if people is None and isinstance(payload, dict):
        people = payload.get("people")
    if not isinstance(people, list):
        raise ProviderError("Unexpected friends response from OpenXBL.")
    out = []
    for p in people:
        x = _xuid(p.get("xuid")) if isinstance(p, dict) else None
        if not x:
            continue
        mine, theirs = p.get("isFollowedByCaller"), p.get("isFollowingCaller")
        out.append({
            "xuid": x, "gamertag": str(p.get("gamertag") or p.get("displayName") or x),
            "state": str(p.get("presenceState") or "Unknown"), "text": str(p.get("presenceText") or ""),
            "favorite": bool(p.get("isFavorite")),
            "relation": ("Friend" if mine and theirs else "You follow" if mine else "Follows you" if theirs
                         else "In your list"),
            "last_seen": _iso_to_epoch(p.get("lastSeenDateTimeUtc"))})
    out.sort(key=lambda f: (f["state"].casefold() != "online", not f["favorite"], f["gamertag"].casefold()))
    return out


def parse_history(payload) -> dict:
    """{"messages": [{"sender", "text", "at"}...] oldest first, "more": bool}. Shape seen for real:
    content.messages[].contentPayload.content.parts[] with contentType "text"; deleted messages are left out."""
    content = payload.get("content") if isinstance(payload, dict) else None
    msgs = content.get("messages") if isinstance(content, dict) else None
    if not isinstance(msgs, list):
        raise ProviderError("Unexpected message history from OpenXBL.")
    out = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("isDeleted"):
            continue
        cp = (m.get("contentPayload") or {}).get("content")
        parts = cp.get("parts") if isinstance(cp, dict) else None
        if isinstance(parts, list):
            text = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)
                            and p.get("contentType") == "text").strip() or "(not text: picture or other content)"
        else:
            text = str(cp) if isinstance(cp, str) else "(not text: picture or other content)"
        out.append({"sender": str(m.get("sender") or ""), "text": text, "at": _iso_to_epoch(m.get("timestamp"))})
    out.sort(key=lambda m: m["at"] or 0)
    return {"messages": out, "more": content.get("continuationToken") is not None}


def history(key: str, xuid, call: Call = None) -> dict:
    """Your messages with one player. The conversation list endpoints answer 405 at OpenXBL right now, so history
    is per player only. OpenXBL documents no way to page further back."""
    return parse_history((call or openxbl_call)("GET", API + "messages/" + _check_xuid(xuid), key))


def profile_url(gamertag: str) -> str:
    """Xbox's own profile page (sign-in needed); Xbox's own options for a player are there."""
    return "https://www.xbox.com/play/user/" + urllib.parse.quote(gamertag.strip())


def friend_list(key: str, call: Call = None) -> List[dict]:
    return parse_friend_list((call or openxbl_call)("GET", FRIENDS_URL, key))
