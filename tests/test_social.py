import pytest

from brpw import social
from brpw.providers import ProviderError


class Calls:
    def __init__(self, answer=None):
        self.seen, self.answer = [], answer or {}

    def __call__(self, method, url, key, data=None):
        self.seen.append((method, url, key, data))
        return self.answer


def test_friend_actions_use_the_documented_endpoints():
    c = Calls()
    social.add_friend("k", "2535400000000001", call=c)
    social.remove_friend("k", "2535400000000001", call=c)
    social.send_message("k", "2535400000000001", "  ahoy, sail with us?  ", call=c)
    assert c.seen == [("PUT", "https://api.xbl.io/v2/friends/2535400000000001", "k", None),
                      ("DELETE", "https://api.xbl.io/v2/friends/2535400000000001", "k", None),
                      ("POST", "https://api.xbl.io/v2/conversations", "k",
                       {"xuid": "2535400000000001", "message": "ahoy, sail with us?"})]


def test_bad_input_is_refused_before_any_request():
    c = Calls()
    for bad in (lambda: social.add_friend("k", "", call=c), lambda: social.add_friend("k", "../x", call=c),
                lambda: social.send_message("k", "1", "   ", call=c),
                lambda: social.send_message("k", "1", "x" * (social.MAX_MESSAGE + 1), call=c)):
        with pytest.raises(ProviderError):
            bad()
    assert c.seen == []


def test_friend_list_online_and_favorites_first():
    c = Calls({"content": {"people": [
        {"xuid": "1", "gamertag": "Zed", "presenceState": "Offline", "isFollowedByCaller": True,
         "isFollowingCaller": True, "lastSeenDateTimeUtc": "2026-11-01T10:00:00Z"},
        {"xuid": "2", "gamertag": "Amy", "presenceState": "Online", "presenceText": "Sea of Thieves",
         "isFollowedByCaller": True, "isFollowingCaller": False},
        {"xuid": "3", "gamertag": "Fav", "presenceState": "Online", "isFavorite": True,
         "isFollowedByCaller": True, "isFollowingCaller": True},
        {"gamertag": "no xuid"}]}})
    fl = social.friend_list("k", call=c)
    assert [f["gamertag"] for f in fl] == ["Fav", "Amy", "Zed"]
    assert fl[1]["relation"] == "You follow" and fl[1]["text"] == "Sea of Thieves" and fl[2]["last_seen"]
    with pytest.raises(ProviderError):
        social.parse_friend_list({"content": "nope"})


def test_history_oldest_first_with_deleted_marked_and_more_flag():
    c = Calls({"content": {"continuationToken": "abc", "messages": [
        {"sender": "2", "timestamp": "2026-11-07T21:00:00Z",
         "contentPayload": {"content": {"parts": [{"contentType": "text", "text": "second"}]}}},
        {"sender": "1", "timestamp": "2026-11-07T20:00:00Z",
         "contentPayload": {"content": {"parts": [{"contentType": "text", "text": "first"}]}}},
        {"sender": "1", "timestamp": "2026-11-07T20:30:00Z", "isDeleted": True, "contentPayload": {}},
        {"sender": "2", "timestamp": "2026-11-07T22:00:00Z",
         "contentPayload": {"content": {"parts": [{"contentType": "image"}]}}}]}})
    h = social.history("k", "2", call=c)
    assert c.seen[0][:2] == ("GET", "https://api.xbl.io/v2/messages/2")
    assert [(m["text"], m["deleted"]) for m in h["messages"]] == [
        ("first", False), ("", True), ("second", False), ("(not text: picture or other content)", False)]
    assert h["more"] and h["messages"][0]["sender"] == "1" and h["messages"][0]["at"]
    with pytest.raises(ProviderError):
        social.parse_history({"content": {}})
    assert social.profile_url(" Salty Pirate ") == "https://www.xbox.com/play/user/Salty%20Pirate"


def test_profile_opens_in_the_xbox_app_when_installed_else_on_the_web():
    started, web = [], []
    assert social.open_profile("Salty Pirate", "2535400000000001", start=started.append, web=web.append,
                               installed=lambda: True) == "app"
    assert started == ["msxbox://profile?xuid=2535400000000001"] and web == []

    def broken(link):
        raise OSError("no handler")
    assert social.open_profile("Salty Pirate", "2535400000000001", start=broken, web=web.append,
                               installed=lambda: True) == "web"
    assert social.open_profile("Salty Pirate", "", start=started.append, web=web.append,
                               installed=lambda: True) == "web"              # no XUID: the web page by gamertag
    assert social.open_profile("Salty Pirate", "1", start=started.append, web=web.append,
                               installed=lambda: False) == "web"
    assert web == ["https://www.xbox.com/play/user/Salty%20Pirate"] * 3 and len(started) == 1


def test_game_bar_social_widget_link():
    started = []
    assert social.open_gamebar_social(start=started.append, installed=lambda: True)
    assert started == ["ms-gamebar://launchForeground/activate/"
                       "Microsoft.GamingApp_8wekyb3d8bbwe_Microsoft.Xbox.WidgetsApp_SocialWidget"]
    assert not social.open_gamebar_social(start=started.append, installed=lambda: False) and len(started) == 1

    def broken(link):
        raise OSError("no handler")
    assert not social.open_gamebar_social(start=broken, installed=lambda: True)


def test_xbox_app_is_started_normally():
    started = []
    assert social.open_xbox_app(start=started.append, installed=lambda: True)
    assert started == [r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"]   # not the signIn link
    assert not social.open_xbox_app(start=started.append, installed=lambda: False) and len(started) == 1

    def broken(link):
        raise OSError("no handler")
    assert not social.open_xbox_app(start=broken, installed=lambda: True)


def test_search_players_shows_how_each_relates_to_you():
    c = Calls({"content": {"people": [
        {"xuid": "1", "gamertag": "Salty Sam", "isFollowedByCaller": False, "isFollowingCaller": False},
        {"xuid": "2", "gamertag": "Salty Mate", "isFollowedByCaller": True, "isFollowingCaller": True}]}})
    res = social.search_players("k", " salty sam ", call=c)
    assert c.seen[0][:2] == ("GET", "https://api.xbl.io/v2/search/salty%20sam")
    assert {r["gamertag"]: r["relation"] for r in res} == {"Salty Sam": "Not in your list", "Salty Mate": "Friend"}
    assert social.search_players("k", "   ", call=c) == [] and len(c.seen) == 1


def msg(sender, text, ts):
    return {"sender": sender, "timestamp": ts, "contentPayload": {"content": {"parts": [{"contentType": "text",
                                                                                         "text": text}]}}}


def test_inbox_lists_chats_newest_first_with_names(monkeypatch):
    from brpw import direct
    from brpw.msauth import MARK
    me = "2535400000000001"
    inbox = {"primary": {"conversations": [
        {"type": "OneToOne", "participants": [me, "11"], "isRead": True,
         "lastMessage": msg(me, "see you at the fort", "2026-11-10T20:00:00Z")},
        {"type": "OneToOne", "participants": ["12", me], "isRead": False,
         "lastMessage": msg("12", "ahoy!", "2026-11-14T20:00:00Z")},
        {"type": "OneToOne", "participants": [me, "13"], "isRead": False, "muted": True,
         "lastMessage": msg("13", "muted spam", "2026-11-01T20:00:00Z")},
        {"type": "Group", "participants": [me, "11", "12"]}]}}
    seen = []

    def call(method, url, key, data=None):
        seen.append(url.rsplit("/v2/", 1)[1])
        if url.endswith("/conversations"):
            return {"content": inbox}
        ids = url.rsplit("/", 1)[1].split(",")
        return {"content": {"people": [{"xuid": x, "gamertag": f"Player{x}", "presenceState": "Online"} for x in ids
                                       if x != "11"]}}
    res = social.inbox(MARK + me, call=call)
    assert [(c["gamertag"], c["preview"], c["mine"], c["unread"], c["muted"]) for c in res["chats"]] == [
        ("Player12", "ahoy!", False, True, False), ("11", "see you at the fort", True, False, False),   # 11: no name
        ("Player13", "muted spam", False, True, True)]                   # muted on Xbox: still listed
    assert res["groups"] == 1 and res["chats"][0]["state"] == "Online"
    assert seen == ["conversations", "player/summary/12,11,13"]
    with pytest.raises(ProviderError) as e:
        social.inbox("plain-openxbl-key", call=call)                    # OpenXBL's inbox answers 405
    assert "signed in with Microsoft" in str(e.value)
    seen.clear()
    unread = social.unread_chats(MARK + me, call=call)                  # theirs, unread, not muted; names for those
    assert [(c["gamertag"], c["preview"]) for c in unread] == [("Player12", "ahoy!")]
    assert seen == ["conversations", "player/summary/12"]
    assert social.unread_chats("plain-openxbl-key", call=call) == []    # OpenXBL: never asked


def test_profiles_ask_ten_players_per_request():
    asked = []

    def call(method, url, key, data=None):
        ids = url.rsplit("/", 1)[1].split(",")
        asked.append(len(ids))
        return {"content": {"people": [{"xuid": x, "gamertag": "G" + x} for x in ids]}}
    names = social.profiles("k", [str(i) for i in range(1, 24)] + ["1", "bad"], call=call)
    assert asked == [10, 10, 3] and len(names) == 23 and names["5"]["gamertag"] == "G5"


def test_playing_sea_of_thieves_from_your_own_presence():
    online_sot = {"xuid": "1", "state": "Online", "devices": [
        {"type": "Scarlett", "titles": [{"id": "750323071", "name": "Home"},
                                        {"id": "1717113201", "name": "Sea of Thieves", "placement": "Full"}]}]}
    assert social.playing_sot({"content": online_sot})                            # Microsoft sign-in shape
    assert social.playing_sot([online_sot])                                       # a list, as some APIs answer
    steam = {"state": "Online", "devices": [{"titles": [{"id": "999", "name": "Sea of Thieves: 2026 Edition"}]}]}
    assert social.playing_sot(steam)                                              # matched by name too
    assert not social.playing_sot({"content": {"state": "Online", "devices": [{"titles": [{"id": "1", "name": "Halo"}]}]}})
    offline = {"xuid": "1", "state": "Offline", "lastSeen": {"titleId": "1717113201", "titleName": "Sea of Thieves"}}
    assert not social.playing_sot({"content": offline})                           # played earlier doesn't count
    assert not social.playing_sot({}) and not social.playing_sot(None)
    c = Calls({"content": online_sot})
    assert social.am_i_playing_sot("k", call=c) and c.seen[0][:2] == ("GET", "https://api.xbl.io/v2/presence")


def test_mute_and_delete_go_to_the_conversation():
    from brpw.msauth import MARK
    c = Calls({})
    key = MARK + "1"
    social.mute_chat(key, "e9b8feac-01fa-0009", True, call=c)
    social.mute_chat(key, "e9b8feac-01fa-0009", False, call=c)
    social.delete_message(key, "e9b8feac-01fa-0009", "46978766227842037", call=c)
    social.delete_chat(key, "e9b8feac-01fa-0009", "46978766227842037", call=c)
    assert [(m, u.rsplit("/v2/", 1)[1]) for m, u, *_ in c.seen] == [
        ("PUT", "conversations/e9b8feac-01fa-0009/mute"), ("DELETE", "conversations/e9b8feac-01fa-0009/mute"),
        ("DELETE", "conversations/e9b8feac-01fa-0009/messages/46978766227842037"), ("PUT", "conversations/horizon")]
    assert c.seen[-1][3] == {"conversations": [{"conversationId": "e9b8feac-01fa-0009", "conversationType": "OneToOne",
                                                "horizonType": "Delete", "horizon": "46978766227842037"}]}
    for bad in (lambda: social.mute_chat("plain-openxbl-key", "abc", call=c),          # Microsoft sign-in only
                lambda: social.mute_chat(key, "../inbox", call=c),                      # no odd conversation ids
                lambda: social.delete_message(key, "abc", "x1", call=c),
                lambda: social.delete_chat(key, "abc", "", call=c)):
        with pytest.raises(ProviderError):
            bad()
    assert len(c.seen) == 4


def test_names_for_many_chats_are_asked_at_the_same_time():
    import threading
    import time as _time
    active, peak, lock = [0], [0], threading.Lock()

    def call(method, url, key, data=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _time.sleep(0.05)
        with lock:
            active[0] -= 1
        ids = url.rsplit("/", 1)[1].split(",")
        return {"content": {"people": [{"xuid": x, "gamertag": "G" + x} for x in ids]}}
    names = social.profiles("k", [str(i) for i in range(1, 46)], call=call)
    assert len(names) == 45 and peak[0] > 1                                  # 5 requests, not one after another


def test_links_and_deleted_last_messages_in_the_inbox():
    assert social.message_text({"contentPayload": {"content": {"parts": [
        {"contentType": "text", "text": "look"}, {"contentType": "weblink", "url": "https://sotrep.com/x"}]}}}) == \
        "look https://sotrep.com/x"
    me = "2535400000000001"
    res = social.parse_inbox({"content": {"primary": {"conversations": [
        {"type": "OneToOne", "participants": [me, "11"], "isRead": True, "conversationId": "c1",
         "lastMessage": {"sender": "11", "isDeleted": True, "clock": "123", "timestamp": "2026-11-10T20:00:00Z"}}]}}}, me)
    assert res["chats"][0]["text"] == "(message deleted)" and res["chats"][0]["conversation"] == "c1"
    assert res["chats"][0]["horizon"] == "123"


def test_sea_of_thieves_hours_only_for_microsoft_sign_in_and_per_50():
    from brpw.msauth import MARK
    asked = []

    def call(method, url, key, data=None):
        asked.append((method, url.rsplit("/v2/", 1)[1], len(data["xuids"])))
        return {"content": {"statlistscollection": [
            {"arrangebyfieldid": x, "stats": [{"name": "MinutesPlayed", "titleid": "1717113201", "value": "600"}]}
            for x in data["xuids"] if x != "3"]}}                              # 3 keeps it private: no answer
    mins = social.sot_minutes(MARK + "1", [str(i) for i in range(1, 61)] + ["bad"], call=call)
    assert asked == [("POST", "stats/sot-minutes", 50), ("POST", "stats/sot-minutes", 10)]
    assert len(mins) == 59 and mins["1"] == 600 and "3" not in mins
    assert social.sot_minutes("plain-openxbl-key", ["1"], call=call) == {} and len(asked) == 2
