import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from brpw import direct, msauth, providers, social
from brpw.providers import ProviderError

XUID = "2535400000000099"


class FakeAccounts:
    def __init__(self):
        self.asked = []

    def auth(self, xuid):
        self.asked.append(xuid)
        return {"header": "XBL3.0 x=uhs;tok", "xuid": xuid, "gamertag": "Me", "expires": time.time() + 3600}


@pytest.fixture
def routed(monkeypatch):
    seen = []

    def fake_request(method, url, auth, version, data=None, timeout=20):
        seen.append((method, url, version, data))
        if "recentplayers" in url:
            return {"people": [
                {"xuid": "1", "gamertag": "Salty", "recentPlayer": {"titles": [
                    {"titleId": "1", "titleName": "Other Game", "lastPlayedWithDateTime": "2026-11-01T10:00:00Z"},
                    {"titleId": "1717113201", "titleName": "Sea of Thieves: 2026 Edition",
                     "lastPlayedWithDateTime": "2026-11-05T22:13:00Z"}]}},
                {"xuid": "2", "gamertag": "Elsewhere", "recentPlayer": {"titles": [
                    {"titleName": "Other Game", "lastPlayedWithDateTime": "2026-11-02T10:00:00Z"}]}}]}
        if "xblmessaging" in url:
            return {"messages": [], "continuationToken": None}
        return {"people": [{"xuid": "1", "gamertag": "Salty", "presenceState": "Online"}]}
    monkeypatch.setattr(direct, "_request", fake_request)
    accs = FakeAccounts()
    direct.configure(accs)
    yield seen, accs
    direct.configure(None)


def test_recent_players_keep_game_and_time_and_only_sea_of_thieves(routed):
    seen, accs = routed
    key = msauth.MARK + XUID
    players = providers.parse_openxbl(providers.openxbl_json(providers.OPENXBL_URL, None, {"X-Authorization": key}))
    assert [(p.gamertag, p.xuid) for p in players] == [("Salty", "1")]                # the other game is left out
    assert players[0].observed_at == providers._iso_to_epoch("2026-11-05T22:13:00Z")  # Sea of Thieves time, not the other
    assert seen[0][:3] == ("GET", "https://peoplehub.xboxlive.com/users/me/people/recentplayers", "5")
    assert accs.asked == [XUID]


def test_every_openxbl_address_maps_to_the_xbox_service(routed):
    seen, _ = routed
    key = msauth.MARK + XUID
    social.friend_list(key)
    social.add_friend(key, "123")
    social.remove_friend(key, "123")
    social.send_message(key, "123", " ahoy ")
    social.history(key, "123")
    providers.find_gamertag(key, "Salty")
    providers.openxbl_json("https://api.xbl.io/v2/player/summary/1,2", None, {"X-Authorization": key})
    providers.openxbl_json("https://api.xbl.io/v2/presence/1", None, {"X-Authorization": key})
    providers.openxbl_json(providers.OPENXBL_ACCOUNT_URL, None, {"X-Authorization": key})
    got = [(m, u, v) for m, u, v, d in seen]
    assert got == [
        ("GET", "https://peoplehub.xboxlive.com/users/me/people/friends/decoration/presencedetail", "7"),
        ("GET", "https://peoplehub.xboxlive.com/users/me/people/social/decoration/presencedetail", "7"),
        ("POST", "https://social.xboxlive.com/bulk/users/me/people/friends/v2?method=add", "3"),
        ("DELETE", f"https://social.xboxlive.com/users/xuid({XUID})/people/xuid(123)", "2"),
        ("POST", f"https://msg.xboxlive.com/users/xuid({XUID})/outbox", "3"),
        ("GET", "https://xblmessaging.xboxlive.com/network/Xbox/users/me/conversations/users/xuid(123)?maxItems=100",
         "1"),
        ("GET", "https://peoplehub.xboxlive.com/users/me/people/search?q=Salty&maxItems=15", "5"),
        ("GET", "https://peoplehub.xboxlive.com/users/me/people/xuids(1,2)/decoration/"
                "detail,presenceDetail,multiplayerSummary", "5"),
        ("GET", "https://userpresence.xboxlive.com/users/xuid(1)?level=all", "3"),
        ("GET", "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag,GameDisplayPicRaw", "2")]
    assert seen[4][3] == {"header": {"recipients": [{"xuid": 123}]}, "messageText": "ahoy"}
    with pytest.raises(ProviderError):
        providers.openxbl_json("https://api.xbl.io/v2/clubs", None, {"X-Authorization": key})


def test_openxbl_keys_never_go_to_xbox_directly(monkeypatch):
    monkeypatch.setattr(direct, "call", lambda *a, **k: pytest.fail("an OpenXBL key must not be routed to Xbox"))
    monkeypatch.setattr(providers, "http_json", lambda url, data, headers, form: {"content": {"people": []}})
    assert providers.openxbl_json(providers.OPENXBL_URL, None, {"X-Authorization": "plain-openxbl-key"})
    assert not direct.is_ms_key("plain-openxbl-key") and direct.is_ms_key(msauth.MARK + "1")


class FakeTokens:
    """Microsoft + Xbox token endpoints."""

    def __init__(self, xsts_error=None):
        self.calls, self.refreshes, self.xsts_error = [], 0, xsts_error

    def __call__(self, url, data=None, headers=None, form=False):
        self.calls.append((url, dict(data or {})))
        if url == msauth.TOKEN_URL:
            if data["grant_type"] == "refresh_token":
                if data["refresh_token"] == "revoked":
                    err = ProviderError("HTTP 400 invalid_grant")
                    err.code = 400
                    raise err
                self.refreshes += 1
                return {"access_token": f"access{self.refreshes}", "refresh_token": f"refresh{self.refreshes}"}
            return {"access_token": "access0", "refresh_token": "refresh0"}
        if url == msauth.USER_AUTH_URL:
            assert data["Properties"]["RpsTicket"].startswith("d=access")
            return {"Token": "user-token"}
        if url == msauth.XSTS_URL:
            if self.xsts_error:
                raise ProviderError(f"HTTP 401 from xsts. {{\"XErr\":{self.xsts_error}}}")
            return {"Token": "xsts", "NotAfter": "2099-01-01T00:00:00Z",
                    "DisplayClaims": {"xui": [{"uhs": "uhs1", "xid": XUID, "gtg": "Dolly"}]}}
        raise AssertionError(url)


def fake_protect(s):
    return "enc:" + s[::-1]


def fake_unprotect(s):
    if not s.startswith("enc:"):
        raise ValueError("not ours")
    return s[4:][::-1]


def test_saved_sign_in_is_encrypted_cached_and_refreshed(tmp_path):
    tokens = FakeTokens()
    accs = msauth.MsAccounts(tmp_path / "ms.json", http=tokens, protect_fn=fake_protect, unprotect_fn=fake_unprotect)
    assert accs.add_from_ms_token({"access_token": "access0", "refresh_token": "refresh0"}) == (XUID, "Dolly")
    raw = (tmp_path / "ms.json").read_text(encoding="utf-8")
    assert "refresh0" not in raw and "enc:" in raw and "xsts" not in raw     # only the encrypted refresh token
    assert accs.auth(XUID)["header"] == "XBL3.0 x=uhs1;xsts" and tokens.refreshes == 0   # cached
    accs.cache[XUID]["expires"] = time.time() + 60                                       # about to expire
    accs.auth(XUID)
    assert tokens.refreshes == 1 and "refresh1"[::-1] in (tmp_path / "ms.json").read_text(encoding="utf-8")
    accs.remove(XUID)
    with pytest.raises(ProviderError) as e:
        accs.auth(XUID)
    assert e.value.reauth and accs.xuids() == []


def test_revoked_or_unreadable_sign_in_asks_to_sign_in_again(tmp_path):
    p = tmp_path / "ms.json"
    p.write_text(json.dumps({"accounts": {XUID: {"gamertag": "Dolly", "refresh": "enc:" + "revoked"[::-1]},
                                          "2": {"gamertag": "Other", "refresh": "from another PC"}}}),
                 encoding="utf-8")
    accs = msauth.MsAccounts(p, http=FakeTokens(), protect_fn=fake_protect, unprotect_fn=fake_unprotect)
    for x in (XUID, "2"):
        with pytest.raises(ProviderError) as e:
            accs.auth(x)
        assert e.value.reauth and "Sign in again" in str(e.value)


def test_xbox_account_problems_are_explained(tmp_path):
    accs = msauth.MsAccounts(tmp_path / "ms.json", http=FakeTokens(xsts_error=2148916238),
                             protect_fn=fake_protect, unprotect_fn=fake_unprotect)
    with pytest.raises(ProviderError) as e:
        accs.add_from_ms_token({"access_token": "access0", "refresh_token": "refresh0"})
    assert "child account" in str(e.value) and accs.xuids() == []


def test_browser_sign_in_uses_pkce_and_the_local_redirect():
    tokens = FakeTokens()

    def fake_browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["scope"] == [msauth.SCOPE]
        back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "the-code", "state": q["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()

    res = msauth.browser_sign_in("client", fake_browser, timeout=10, post=tokens)
    assert res["refresh_token"] == "refresh0"
    url, body = tokens.calls[-1]
    assert body["code"] == "the-code" and body["grant_type"] == "authorization_code" and len(body["code_verifier"]) > 40
    assert body["redirect_uri"].startswith("http://localhost:") and body["redirect_uri"].endswith("/auth/callback")


def test_browser_sign_in_reports_a_cancelled_login():
    def cancel(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(
            {"error": "access_denied", "error_description": "The user cancelled", "state": q["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()
    with pytest.raises(ProviderError) as e:
        msauth.browser_sign_in("client", cancel, timeout=10, post=FakeTokens())
    assert "cancelled" in str(e.value)


def test_dpapi_round_trip():
    pytest.importorskip("ctypes")
    try:
        blob = msauth.protect("secret refresh token")
    except (AttributeError, OSError):
        pytest.skip("Windows only")
    assert "secret" not in blob and msauth.unprotect(blob) == "secret refresh token"


def test_inbox_and_names_go_to_xbox_messaging_and_peoplehub(routed):
    seen, _ = routed
    key = msauth.MARK + XUID
    social.openxbl_call("GET", social.API + "conversations", key)
    assert seen[-1][:3] == ("GET", "https://xblmessaging.xboxlive.com/network/Xbox/users/me/inbox", "1")


def test_own_presence_goes_to_userpresence_me(routed):
    seen, _ = routed
    social.openxbl_call("GET", social.API + "presence", msauth.MARK + XUID)
    assert seen[-1][:3] == ("GET", "https://userpresence.xboxlive.com/users/me?level=all", "3")


def _id_token(claims):
    import base64
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "head." + body + ".sig"


def test_the_email_comes_from_a_new_sign_in_and_is_saved_encrypted(tmp_path):
    assert msauth.email_from({"id_token": _id_token({"email": "pirate@example.com"})}) == "pirate@example.com"
    assert msauth.email_from({"id_token": _id_token({"preferred_username": "p@example.com"})}) == "p@example.com"
    assert msauth.email_from({}) == "" and msauth.email_from({"id_token": "garbage"}) == ""
    tokens = FakeTokens()
    accs = msauth.MsAccounts(tmp_path / "ms.json", http=tokens, protect_fn=fake_protect, unprotect_fn=fake_unprotect)
    accs.add_from_ms_token({"access_token": "access0", "refresh_token": "refresh0",
                            "id_token": _id_token({"email": "pirate@example.com"})})
    assert accs.email(XUID) == "pirate@example.com"
    assert "pirate@example.com" not in (tmp_path / "ms.json").read_text(encoding="utf-8")       # encrypted on disk
    assert accs.email("nobody") == ""
    accs.cache[XUID]["expires"] = 0
    accs.auth(XUID)                                     # a refresh keeps the old scope (more fails for old sign-ins)
    refresh = [body for url, body in tokens.calls if body.get("grant_type") == "refresh_token"][-1]
    assert refresh["scope"] == msauth.REFRESH_SCOPE and "email" not in refresh["scope"]
    assert "openid email" in msauth.SCOPE                                    # a new sign-in asks for the email


def test_conversation_actions_map_to_xbox_messaging(routed):
    seen, _ = routed
    key = msauth.MARK + XUID
    social.mute_chat(key, "c-1", True)
    social.mute_chat(key, "c-1", False)
    social.delete_message(key, "c-1", "55")
    social.delete_chat(key, "c-1", "55")
    M = "https://xblmessaging.xboxlive.com/network/Xbox/users/me/conversations/"
    assert [(m, u, v) for m, u, v, d in seen] == [
        ("PUT", M + "c-1/mute", "1"), ("DELETE", M + "c-1/mute", "1"), ("DELETE", M + "c-1/messages/55", "1"),
        ("PUT", M + "horizon", "2")]


def test_hours_played_go_to_xbox_userstats(routed):
    seen, _ = routed
    social.sot_minutes(msauth.MARK + XUID, ["5", "6"])
    m, u, v, d = seen[-1]
    assert (m, u, v) == ("POST", "https://userstats.xboxlive.com/batch", "2")
    assert d == {"arrangebyfield": "xuid", "xuids": ["5", "6"],
                 "stats": [{"name": "MinutesPlayed", "titleid": "1717113201"}]}


def test_friend_requests_map_to_peoplehub_and_the_social_service(routed):
    seen, _ = routed
    key = msauth.MARK + XUID
    social.friend_requests(key)
    social.accept_friend(key, "77")
    social.ignore_friend(key, "78")
    PH = "https://peoplehub.xboxlive.com/users/me/people/friendrequests"
    assert [(m, u, v) for m, u, v, d in seen] == [
        ("GET", PH + "(received)/decoration/presencedetail", "7"), ("GET", PH + "(sent)/decoration/presencedetail", "7"),
        ("POST", "https://social.xboxlive.com/bulk/users/me/people/friends/v2?method=add", "3"),
        ("DELETE", f"https://social.xboxlive.com/users/xuid({XUID})/people/xuid(78)", "2")]
    assert seen[2][3] == {"xuids": ["77"]}
    with pytest.raises(ProviderError):                       # OpenXBL has no friend requests
        social.friend_requests("an-openxbl-key")


def test_friend_requests_follow_xboxs_own_flags():
    def person(x, **flags):
        return dict({"xuid": x, "gamertag": "P" + x, "presenceState": "Online", "addedDateTimeUtc": "2026-11-20T10:00:00Z"},
                    **flags)
    res = social.parse_requests({"content": {
        "received": {"people": [person("1", isFriendRequestReceived=True),
                                person("2", isFriendRequestSent=True),        # Xbox lists sent ones here too
                                person("3", isFriendRequestReceived=True, isFriend=True)]},
        "sent": {"people": [person("2", isFriendRequestSent=True)]}}})
    assert [f["xuid"] for f in res["received"]] == ["1"] and [f["xuid"] for f in res["sent"]] == ["2"]
    assert res["received"][0]["relation"] == "Wants to be friends" and res["sent"][0]["relation"] == "Request sent"
    assert res["received"][0]["at"]
    mixed = social.parse_requests({"content": {                              # Xbox listing a sent one as received
        "received": {"people": [person("5", isFriendRequestReceived=True), person("1", isFriendRequestReceived=True)]},
        "sent": {"people": [person("5", isFriendRequestSent=True)]}}})
    assert [f["xuid"] for f in mixed["received"]] == ["1"] and [f["xuid"] for f in mixed["sent"]] == ["5"]


def _handle(owner, hid, members=1, most=3, closed=False):
    return {"type": "activity", "id": hid, "ownerXuid": owner, "titleId": "1717113201",
            "sessionRef": {"scid": social.SOT_SCID, "templateName": "MediumShipSessionTemplate", "name": "s-" + hid},
            "relatedInfo": {"membersCount": members, "maxMembersCount": most, "closed": closed,
                            "joinRestriction": "followed"}}


def test_ships_come_from_xbox_sessions(monkeypatch):
    seen = []

    def fake_request(method, url, auth, version, data=None, timeout=20):
        seen.append((method, url, version, data))
        return {"results": [_handle("77", "h-1"), _handle("78", "h-2", members=4, most=4),
                            dict(_handle("79", "h-3"), titleId="123")]}               # another game: ignored
    monkeypatch.setattr(direct, "_request", fake_request)
    direct.configure(FakeAccounts())
    try:
        s = social.ships(msauth.MARK + XUID)
        assert set(s) == {"77", "78"} and social.ship_label(s["77"]) == "on a brigantine · 1/3"
        assert social.ship_label(s["78"]) == "on a galleon · 4/4 · full"
        queries = [d for m, u, v, d in seen if u.endswith("/handles/query?include=relatedInfo")]
        assert all(d["scid"] == social.SOT_SCID for d in queries)               # Xbox needs the game's config
        assert {d["owners"]["people"]["moniker"] for d in queries} == {"friends", "people"}
        with pytest.raises(ProviderError):                                     # OpenXBL keys can't
            social.ships("an-openxbl-key")
    finally:
        direct.configure(None)


def test_your_crew_comes_from_your_ship_session(monkeypatch):
    seen = []
    doc = {"members": {"0": {"gamertag": "Me", "constants": {"system": {"xuid": XUID}}},
                       "1": {"gamertag": "Salty", "constants": {"system": {"xuid": "77"}}},
                       "2": {"gamertag": "Kraken", "constants": {"system": {"xuid": "78"}}}}}

    def fake_request(method, url, auth, version, data=None, timeout=20):
        seen.append((method, url, version))
        if "/sessions/" in url:
            return doc
        return {"results": [dict(_handle(XUID, "mine", members=3, most=3))]}
    monkeypatch.setattr(direct, "_request", fake_request)
    direct.configure(FakeAccounts())
    try:
        crew = social.my_crew(msauth.MARK + XUID)
        assert crew["ship"] == "brigantine" and crew["session"] == "s-mine"
        assert crew["members"] == [{"xuid": "77", "gamertag": "Salty"}, {"xuid": "78", "gamertag": "Kraken"}]
        assert seen[-1] == ("GET", "https://sessiondirectory.xboxlive.com/serviceconfigs/" + social.SOT_SCID +
                            "/sessionTemplates/MediumShipSessionTemplate/sessions/s-mine", "107")
        monkeypatch.setattr(direct, "_request", lambda *a, **k: {"results": []})
        assert social.my_crew(msauth.MARK + XUID) is None                       # not on a ship
    finally:
        direct.configure(None)
