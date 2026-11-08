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
