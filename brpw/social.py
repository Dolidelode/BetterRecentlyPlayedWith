"""Xbox social actions through OpenXBL (https://api.xbl.io/docs): add or remove a friend, send a message, and the
full friends list with what each friend is doing.

Endpoints, from OpenXBL's own OpenAPI document:
* PUT    /v2/friends/{xuid}   add a friend (follow them)
* DELETE /v2/friends/{xuid}   remove a friend
* GET    /v2/friends          your friends, with presenceState / presenceText
* POST   /v2/conversations    {"xuid": ..., "message": ...} sends an Xbox message

Inviting someone into a game or joining theirs isn't offered: Xbox only allows that inside the game and its own apps.
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
MAX_MESSAGE = 250          # the limit of Xbox's documented send API (msg.xboxlive.com outbox)


def openxbl_call(method: str, url: str, key: str, data: Optional[dict] = None, timeout: int = 20) -> dict:
    """One OpenXBL request with any HTTP method. Errors become ProviderError (with .code when there is one)."""
    from . import direct
    if direct.is_ms_key(key):                 # Microsoft sign-in: Xbox's own services instead of OpenXBL
        return direct.call(method, url, key.strip(), data)
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


def parse_friend_list(payload, in_list: bool = True) -> List[dict]:
    """Friends with what they are doing, online first. Only fields OpenXBL actually returns are used.
    in_list=False (search results): someone neither side follows is "Not in your list"."""
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
        if p.get("isFriend") is True:                          # Xbox's friend system: a request accepted
            mine = theirs = True
        elif p.get("isFriendRequestSent") is True:
            mine, theirs = "sent", False
        elif p.get("isFriendRequestReceived") is True:
            mine, theirs = False, "asks"
        out.append({
            "xuid": x, "gamertag": str(p.get("gamertag") or p.get("displayName") or x),
            "state": str(p.get("presenceState") or "Unknown"), "text": str(p.get("presenceText") or ""),
            "favorite": bool(p.get("isFavorite")),
            "relation": ("Friend" if mine is True and theirs is True else "Request sent" if mine == "sent"
                         else "Wants to be friends" if theirs == "asks" else "You follow" if mine
                         else "Follows you" if theirs else "In your list" if in_list else "Not in your list"),
            "last_seen": _iso_to_epoch(p.get("lastSeenDateTimeUtc")), "pic": str(p.get("displayPicRaw") or "")})
    out.sort(key=lambda f: (f["state"].casefold() != "online", not f["favorite"], f["gamertag"].casefold()))
    return out


def message_text(m: dict) -> str:
    """The text of one Xbox message: contentPayload.content.parts[] of type "text", plus links ("weblink")."""
    cp = (m.get("contentPayload") or {}).get("content")
    parts = cp.get("parts") if isinstance(cp, dict) else None
    if isinstance(parts, list):
        out = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("contentType") == "text":
                out.append(str(p.get("text") or ""))
            elif p.get("contentType") == "weblink":
                out.append(str(p.get("url") or p.get("text") or p.get("uri") or ""))
        return " ".join(x for x in out if x).strip() or "(not text: picture or other content)"
    return str(cp) if isinstance(cp, str) else "(not text: picture or other content)"


def parse_history(payload) -> dict:
    """{"messages": [{"sender", "text", "at", "id", "deleted"}...] oldest first, "more": bool, "conversation": id}.
    Shape seen for real: content.messages[].contentPayload.content.parts[]. A message its sender deleted stays in
    place with isDeleted and its text replaced by "Deleted" on Xbox's side (the original can't be fetched), so it is
    kept as deleted=True with no text."""
    content = payload.get("content") if isinstance(payload, dict) else None
    msgs = content.get("messages") if isinstance(content, dict) else None
    if not isinstance(msgs, list):
        raise ProviderError("Unexpected message history from OpenXBL.")
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        deleted = bool(m.get("isDeleted"))
        out.append({"sender": str(m.get("sender") or ""), "text": "" if deleted else message_text(m),
                    "at": _iso_to_epoch(m.get("timestamp")), "id": str(m.get("messageId") or ""), "deleted": deleted})
    out.sort(key=lambda m: m["at"] or 0)
    return {"messages": out, "more": content.get("continuationToken") is not None,
            "conversation": str(content.get("conversationId") or ""), "muted": content.get("muted") is True}


def history(key: str, xuid, call: Call = None) -> dict:
    """Your messages with one player (the newest; no way to page further back is documented)."""
    return parse_history((call or openxbl_call)("GET", API + "messages/" + _check_xuid(xuid), key))


def profile_url(gamertag: str) -> str:
    """Xbox's own profile page (sign-in needed); Xbox's own options for a player are there."""
    return "https://www.xbox.com/play/user/" + urllib.parse.quote(gamertag.strip())


# The Xbox app (Microsoft.GamingApp) registers msxbox:, and Microsoft's own Game Bar widget opens profiles with
# exactly this link. Found in the installed app's files; Microsoft does not document it, so the web page stays the
# fallback.
APP_PROFILE = "msxbox://profile?xuid={}"


def _registered(scheme: str) -> bool:
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme))
        return True
    except (ImportError, OSError):
        return False


def xbox_app_installed() -> bool:
    return _registered("msxbox")


# Game Bar shares the Xbox app's Windows sign-in, so signing in there as another account switches Game Bar too.
# The app is started normally: its sign-in link (msxbox://signIn/, from its jump list) showed the login screen once
# but left the Xbox app stuck on "Launching" twice (2026-11 and 2026-11-20), so it is not used.
XBOX_APP = r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"


def open_xbox_app(start=None, installed=xbox_app_installed) -> bool:
    if not installed():
        return False
    try:
        (start or _start)(XBOX_APP)
        return True
    except OSError:
        return False


def open_profile(gamertag: str, xuid, start=None, web=None, installed=xbox_app_installed) -> str:
    """Open the player's profile in the Xbox app when it is installed (by XUID), otherwise on xbox.com.
    Returns "app" or "web"."""
    x = _xuid(xuid)
    if x and installed():
        try:
            (start or _start)(APP_PROFILE.format(x))
            return "app"
        except OSError:
            pass
    (web or _web)(profile_url(gamertag))
    return "web"


def _start(link):
    import os
    os.startfile(link)


def _web(url):
    import webbrowser
    webbrowser.open(url)


def parse_inbox(payload, me: str) -> dict:
    """Your conversations, newest first: {"chats": [{xuid, text, mine, at, unread}], "groups": number skipped}.
    Shape seen for real (Xbox messaging inbox): primary.conversations[] with participants, lastMessage, isRead."""
    content = payload.get("content") if isinstance(payload, dict) else None
    primary = content.get("primary") if isinstance(content, dict) else None
    convs = primary.get("conversations") if isinstance(primary, dict) else None
    if not isinstance(convs, list):
        raise ProviderError("Unexpected message inbox from Xbox.")
    chats, groups = [], 0
    for c in convs:
        if not isinstance(c, dict):
            continue
        people = [str(p) for p in (c.get("participants") or [])]
        if c.get("type") != "OneToOne" or len(people) != 2:
            groups += 1                                       # group chats aren't shown (yet)
            continue
        other = next((p for p in people if p != me), None)
        if not _xuid(other):
            continue
        last = c.get("lastMessage") if isinstance(c.get("lastMessage"), dict) else {}
        chats.append({"xuid": other, "mine": str(last.get("sender")) == me,
                      "text": ("(message deleted)" if last.get("isDeleted") else message_text(last)) if last else "",
                      "at": _iso_to_epoch(last.get("timestamp") or c.get("timestamp")),
                      "unread": c.get("isRead") is False, "muted": c.get("muted") is True,
                      "conversation": str(c.get("conversationId") or ""),
                      "horizon": str(last.get("clock") or last.get("messageId") or "")})
    chats.sort(key=lambda c: -(c["at"] or 0))
    return {"chats": chats, "groups": groups}


def profiles(key: str, xuids: List[str], call: Call = None) -> dict:
    """xuid -> {gamertag, state, text} for these players (10 per request: Xbox refuses 20 with HTTP 400)."""
    from concurrent.futures import ThreadPoolExecutor
    ids = [x for x in dict.fromkeys(xuids) if _xuid(x)]
    batches = [ids[i:i + 10] for i in range(0, len(ids), 10)]

    def one(batch):
        return parse_friend_list((call or openxbl_call)("GET", API + "player/summary/" + ",".join(batch), key),
                                 in_list=False)
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, min(5, len(batches)))) as pool:      # at the same time, not in turn
        for people in pool.map(one, batches):
            for p in people:
                out[p["xuid"]] = {"gamertag": p["gamertag"], "state": p["state"], "text": p["text"], "pic": p["pic"]}
    return out


def inbox(key: str, call: Call = None) -> dict:
    """All your one-to-one Xbox conversations with names and online status. Needs a Microsoft sign-in: OpenXBL's
    inbox endpoints answer HTTP 405."""
    from . import direct
    from .msauth import MARK
    if not direct.is_ms_key(key):
        raise ProviderError("The message inbox needs an account signed in with Microsoft (☰ > Add Xbox account...): "
                            "OpenXBL's inbox doesn't work right now.")
    res = parse_inbox((call or openxbl_call)("GET", API + "conversations", key), key.strip()[len(MARK):])
    names = profiles(key, [c["xuid"] for c in res["chats"]], call)
    for c in res["chats"]:
        c.update(names.get(c["xuid"]) or {"gamertag": c["xuid"], "state": "Unknown", "text": "", "pic": ""},
                 preview=c["text"])
    return res


def unread_chats(key: str, call: Call = None) -> List[dict]:
    """Conversations whose newest message is from the other player and unread on Xbox, newest first, with names.
    Chats you muted on Xbox are left out.
    One inbox request, plus one per 10 unread players for the names. Microsoft sign-in only (see inbox)."""
    from . import direct
    from .msauth import MARK
    if not direct.is_ms_key(key):
        return []
    res = parse_inbox((call or openxbl_call)("GET", API + "conversations", key), key.strip()[len(MARK):])
    out = [c for c in res["chats"] if c["unread"] and not c["mine"] and not c["muted"]]
    names = profiles(key, [c["xuid"] for c in out], call) if out else {}
    for c in out:
        c.update(names.get(c["xuid"]) or {"gamertag": c["xuid"], "state": "Unknown", "text": ""}, preview=c["text"])
    return out


def sot_minutes(key: str, xuids: List[str], call: Call = None) -> dict:
    """Sea of Thieves minutes played per player (Xbox's MinutesPlayed statistic), for those who share it.
    Microsoft sign-in only (an OpenXBL key gives {}). 50 players per request."""
    from . import direct
    if not direct.is_ms_key(key):
        return {}
    ids = [x for x in dict.fromkeys(xuids) if _xuid(x)]
    out = {}
    for i in range(0, len(ids), 50):
        payload = (call or openxbl_call)("POST", API + "stats/sot-minutes", key, {"xuids": ids[i:i + 50]})
        content = payload.get("content", payload) if isinstance(payload, dict) else {}
        for group in content.get("statlistscollection") or []:
            x = _xuid(group.get("arrangebyfieldid"))
            for st in group.get("stats") or []:
                v = str(st.get("value") or "").strip()
                if x and st.get("name") == "MinutesPlayed" and v.isdigit():
                    out[x] = int(v)
    return out


def _ms_only(key: str) -> None:
    from . import direct
    if not direct.is_ms_key(key):
        raise ProviderError("This needs an account signed in with Microsoft (☰ > Add Xbox account...).")


def _conv_id(cid) -> str:
    c = str(cid or "").strip()
    if not c or not all(ch.isalnum() or ch == "-" for ch in c):
        raise ProviderError("Unknown conversation.")
    return c


def mute_chat(key: str, conversation: str, mute: bool = True, call: Call = None) -> None:
    """Mute or unmute a conversation on Xbox (the same as in the Xbox app: no notifications from it)."""
    _ms_only(key)
    (call or openxbl_call)("PUT" if mute else "DELETE", API + f"conversations/{_conv_id(conversation)}/mute", key)


def delete_message(key: str, conversation: str, message_id: str, call: Call = None) -> None:
    """Delete one of your own messages (Xbox shows it as deleted to the other player too)."""
    _ms_only(key)
    mid = str(message_id or "").strip()
    if not mid.isdigit():
        raise ProviderError("Unknown message.")
    (call or openxbl_call)("DELETE", API + f"conversations/{_conv_id(conversation)}/messages/{mid}", key)


def delete_chat(key: str, conversation: str, horizon: str, call: Call = None) -> None:
    """Remove a conversation from your inbox (up to its newest message, like the Xbox app's Delete conversation).
    The other player keeps their copy; a new message brings the conversation back."""
    _ms_only(key)
    h = str(horizon or "").strip()
    if not h.isdigit():
        raise ProviderError("Unknown conversation state: refresh the inbox first.")
    (call or openxbl_call)("PUT", API + "conversations/horizon", key,
                           {"conversations": [{"conversationId": _conv_id(conversation), "conversationType": "OneToOne",
                                               "horizonType": "Delete", "horizon": h}]})


SOT_TITLE_ID = "1717113201"


def playing_sot(payload) -> bool:
    """Your own presence: are you in Sea of Thieves right now, on any device (PC or console)? Only the titles of an
    online device count; lastSeen (what you played last) does not."""
    node = payload.get("content", payload) if isinstance(payload, dict) else payload
    if isinstance(node, list):
        node = next((n for n in node if isinstance(n, dict) and "state" in n), {})
    if not isinstance(node, dict) or str(node.get("state", "")).casefold() != "online":
        return False
    for device in node.get("devices") or []:
        for t in (device.get("titles") or []) if isinstance(device, dict) else []:
            if isinstance(t, dict) and (str(t.get("id") or t.get("titleId") or "") == SOT_TITLE_ID
                                        or "sea of thieves" in str(t.get("name") or t.get("titleName") or "").casefold()):
                return True
    return False


def am_i_playing_sot(key: str, call: Call = None) -> bool:
    return playing_sot((call or openxbl_call)("GET", API + "presence", key))


def search_players(key: str, term: str, call: Call = None) -> List[dict]:
    """Anyone on Xbox by gamertag (Xbox's own fuzzy search, up to 15), with how they relate to you."""
    term = (term or "").strip()
    if not term:
        return []
    payload = (call or openxbl_call)("GET", API + "search/" + urllib.parse.quote(term), key)
    return parse_friend_list(payload, in_list=False)


def friend_list(key: str, call: Call = None) -> List[dict]:
    return parse_friend_list((call or openxbl_call)("GET", FRIENDS_URL, key))


# ---- Sea of Thieves ships (Xbox multiplayer sessions; Microsoft sign-in only)
SOT_SCID = "00000000-0000-0000-0000-000066591171"         # Sea of Thieves' Xbox service config (from titlehub)
# Joining or inviting from the app isn't possible: Sea of Thieves ignores join links opened by another program
# (both its own ms-xbl-66591171 link and ms-xbl-multiplayer were tried for real, with the Store and the Steam version,
# 2026-11-23), and Xbox only lets the game itself write invites (HTTP 403). So the app only shows who is on which ship.
SHIPS = {2: "sloop", 3: "brigantine", 4: "galleon"}         # by crew size
def parse_sessions(payload) -> dict:
    """owner XUID -> {handle, ship, members, max, open, session} for Sea of Thieves activity handles (the same
    information the Xbox app shows as "Join game"). Shape seen for real: results[] with id, ownerXuid, sessionRef
    and relatedInfo (membersCount, maxMembersCount, closed, joinRestriction)."""
    content = payload.get("content") if isinstance(payload, dict) else None
    results = content.get("results") if isinstance(content, dict) else None
    if not isinstance(results, list):
        raise ProviderError("Unexpected session list from Xbox.")
    out = {}
    for r in results:
        if not isinstance(r, dict) or str(r.get("titleId")) != SOT_TITLE_ID:
            continue
        owner, handle = _xuid(r.get("ownerXuid")), str(r.get("id") or "")
        if not owner or not handle or not all(ch.isalnum() or ch == "-" for ch in handle):
            continue
        info = r.get("relatedInfo") if isinstance(r.get("relatedInfo"), dict) else {}
        members, most = int(info.get("membersCount") or 0), int(info.get("maxMembersCount") or 0)
        out[owner] = {"handle": handle, "ship": SHIPS.get(most, "ship"), "members": members, "max": most,
                      "crew": parse_members({"members": r.get("members")}) if r.get("members") else [],
                      "open": not info.get("closed") and (not most or members < most),
                      "restriction": str(info.get("joinRestriction") or ""),
                      "session": r.get("sessionRef") if isinstance(r.get("sessionRef"), dict) else None}
    return out


def parse_members(session) -> List[dict]:
    """[{xuid, gamertag}] of the people in an Xbox multiplayer session document."""
    members = session.get("members") if isinstance(session, dict) else None
    out = []
    for _, m in sorted((members or {}).items(), key=lambda kv: str(kv[0])) if isinstance(members, dict) else []:
        if not isinstance(m, dict):
            continue
        system = (m.get("constants") or {}).get("system") or {} if isinstance(m.get("constants"), dict) else {}
        x = _xuid(system.get("xuid") or m.get("xuid"))
        if x:
            out.append({"xuid": x, "gamertag": str(m.get("gamertag") or x)})
    return out


def my_crew(key: str, call: Call = None) -> Optional[dict]:
    """Your ship right now: {ship, session, members (everyone else aboard), max}, or None when you aren't on one."""
    _ms_only(key)
    from .msauth import MARK
    me = key.strip()[len(MARK):]
    payload = (call or openxbl_call)("GET", API + "sessions/sot/crew", key)
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, dict):
        return None
    ship = parse_sessions({"content": {"results": [content.get("handle")]}}).get(me)
    if not ship:
        return None
    ref = ship.get("session") or {}
    return {"ship": ship["ship"], "max": ship["max"], "session": str(ref.get("name") or ship["handle"]),
            "members": [m for m in parse_members(content.get("session")) if m["xuid"] != me]}


def ship_label(s: dict) -> str:
    """'on a brigantine · 2/3' (or 'full')."""
    crew = f"{s['members']}/{s['max']}" if s.get("max") else ""
    return f"on a {s['ship']}" + (f" · {crew}" if crew else "") + ("" if s["open"] else " · full")


def ships(key: str, call: Call = None) -> dict:
    """Ships of the people you follow and your friends, by owner XUID."""
    _ms_only(key)
    return parse_sessions((call or openxbl_call)("GET", API + "sessions/sot", key))


def parse_requests(payload) -> dict:
    """{"received": [...], "sent": [...]} friend requests, each like a friend_list entry plus "at" (when).
    Xbox marks each person isFriendRequestReceived / isFriendRequestSent (seen for real, peoplehub contract 7), but
    its received list sometimes also holds people you sent a request to, flagged as received (seen for real
    2026-11-23). So the sent list is read first and anyone in it counts as sent, never as received."""
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, dict):
        raise ProviderError("Unexpected friend requests from Xbox.")
    out = {"received": [], "sent": []}
    seen = set()
    for side, flag in (("sent", "isFriendRequestSent"), ("received", "isFriendRequestReceived")):
        people = [p for p in ((content.get(side) or {}).get("people") or []) if isinstance(p, dict)]
        for p in people:
            if p.get(flag) is not True or p.get("isFriend") is True:
                continue
            for f in parse_friend_list({"people": [p]}, in_list=False):
                if f["xuid"] in seen:
                    continue
                seen.add(f["xuid"])
                f["at"] = _iso_to_epoch(p.get("addedDateTimeUtc"))
                out[side].append(f)
    for side in out:
        out[side].sort(key=lambda f: -(f["at"] or 0))
    return out


def friend_requests(key: str, call: Call = None) -> dict:
    """Friend requests waiting for you and the ones you sent (Microsoft sign-in only: OpenXBL has none)."""
    _ms_only(key)
    return parse_requests((call or openxbl_call)("GET", FRIENDS_URL + "/requests", key))


def accept_friend(key: str, xuid, call: Call = None) -> None:
    """Accept a friend request: you become friends on Xbox."""
    _ms_only(key)
    (call or openxbl_call)("POST", FRIENDS_URL + "/requests/accept", key, {"xuid": _check_xuid(xuid)})


def ignore_friend(key: str, xuid, call: Call = None) -> None:
    """Ignore (decline) a friend request, or take back one you sent: the request is removed on Xbox."""
    _ms_only(key)
    (call or openxbl_call)("DELETE", FRIENDS_URL + "/" + _check_xuid(xuid), key)
