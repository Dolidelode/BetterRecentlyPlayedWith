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


def test_history_oldest_first_without_deleted_and_with_more_flag():
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
    assert [m["text"] for m in h["messages"]] == ["first", "second", "(not text: picture or other content)"]
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


def test_xbox_sign_in_screen_link():
    started = []
    assert social.open_xbox_sign_in(start=started.append, installed=lambda: True)
    assert started == ["msxbox://signIn/"]
    assert not social.open_xbox_sign_in(start=started.append, installed=lambda: False) and len(started) == 1

    def no_sign_in_link(link):
        if link.startswith("msxbox:"):
            raise OSError("no handler")
        started.append(link)
    assert social.open_xbox_sign_in(start=no_sign_in_link, installed=lambda: True)     # falls back to a normal start
    assert started[-1] == r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"


def test_search_players_shows_how_each_relates_to_you():
    c = Calls({"content": {"people": [
        {"xuid": "1", "gamertag": "Salty Sam", "isFollowedByCaller": False, "isFollowingCaller": False},
        {"xuid": "2", "gamertag": "Salty Mate", "isFollowedByCaller": True, "isFollowingCaller": True}]}})
    res = social.search_players("k", " salty sam ", call=c)
    assert c.seen[0][:2] == ("GET", "https://api.xbl.io/v2/search/salty%20sam")
    assert {r["gamertag"]: r["relation"] for r in res} == {"Salty Sam": "Not in your list", "Salty Mate": "Friend"}
    assert social.search_players("k", "   ", call=c) == [] and len(c.seen) == 1
