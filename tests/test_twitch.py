import json
import time
import urllib.parse

import pytest

from brpw import twitch
from brpw.db import Database, Player
from brpw.providers import ProviderError
from brpw.twitch import (CHUNK, STATUS_MAX_AGE, TOKEN_URL, TwitchChecker, TwitchClient, TwitchCreds, TwitchUser,
                         parse_channel_input)


class Mem:
    """In-memory credentials."""

    def __init__(self, c=("cid", "secret")):
        self.c = c

    def load(self):
        return self.c


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


def err(code):
    e = ProviderError(f"HTTP {code}")
    e.code = code
    return e


def fake(users=None, live=None, calls=None, token_error=None, stream_errors=None):
    """Fake Twitch. users: login -> (id, display); live: id -> dict(title, game, viewers); records every request."""
    users, live = users or {}, live or {}
    stream_errors = list(stream_errors or [])
    state = {"tokens": 0}

    def http(url, data, headers, form=False):
        if calls is not None:
            calls.append(url)
        if url.startswith(TOKEN_URL):
            if token_error:
                raise token_error
            state["tokens"] += 1
            assert data["grant_type"] == "client_credentials" and form
            return {"access_token": f"tok{state['tokens']}", "expires_in": 5000000}
        assert headers["Client-Id"] == "cid" and headers["Authorization"].startswith("Bearer tok")
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/users"):
            if "login" in q:
                hits = [(users[l.lower()], l.lower()) for l in q["login"] if l.lower() in users]
            else:
                hits = [((i, d), d.lower()) for (i, d) in users.values() if i == q["id"][0]]
            return {"data": [{"id": h[0][0], "login": h[1], "display_name": h[0][1]} for h in hits]}
        if u.path.endswith("/streams"):
            if stream_errors:
                raise stream_errors.pop(0)
            ids = q["user_id"]
            return {"data": [{"user_id": i, "user_login": "x", "type": "live", "viewer_count": live[i]["viewers"],
                              "game_name": live[i]["game"], "title": live[i]["title"],
                              "started_at": "2026-10-26T18:59:15Z"} for i in ids if i in live]}
        raise AssertionError(url)
    http.state = state
    return http


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p.db")
    yield d
    d.close()


def test_parse_channel_input():
    for text in ("example_streamer", "@Example_Streamer", " twitch.tv/example_streamer ", "https://www.twitch.tv/Example_Streamer/",
                 "https://m.twitch.tv/example_streamer?sr=a", "http://twitch.tv/example_streamer/videos"):
        assert parse_channel_input(text).lower() == "example_streamer"
    for bad in ("", "  ", "ab", "two words", "https://www.twitch.tv/directory", "https://example.com/example", "a" * 26):
        with pytest.raises(ValueError):
            parse_channel_input(bad)


def test_creds_store(tmp_path):
    c = TwitchCreds(tmp_path / "twitch.json")
    assert not c.configured() and c.load() is None
    with pytest.raises(ValueError):
        c.save("id", " ")
    c.save(" id ", " secret ")
    assert c.load() == ("id", "secret") and c.configured()
    (tmp_path / "twitch.json").write_text("not json")
    assert c.load() is None
    c.clear()
    assert not (tmp_path / "twitch.json").exists()


def test_token_cached_refreshed_and_errors():
    calls = []
    cl = TwitchClient(Mem(), http=fake({"a_user": ("1", "A_User")}, calls=calls), now=Clock())
    cl.resolve("a_user")
    cl.resolve("a_user")
    assert sum(1 for u in calls if u.startswith(TOKEN_URL)) == 1                   # one token for many requests
    with pytest.raises(ProviderError) as ei:
        TwitchClient(Mem(), http=fake(token_error=err(401))).token()
    assert ei.value.reauth and "client ID or secret" in str(ei.value)
    with pytest.raises(ProviderError) as ei:
        TwitchClient(Mem(None)).token()
    assert ei.value.reauth and "not set up" in str(ei.value)
    http = fake({"a_user": ("1", "A_User")})
    cl = TwitchClient(Mem(), http=http, now=Clock())
    first = {"n": 0}
    real = http

    def expired_once(url, data, headers, form=False):
        if "/users" in url and first["n"] == 0:
            first["n"] = 1
            raise err(401)
        return real(url, data, headers, form)
    cl.http = expired_once
    assert cl.resolve("a_user").id == "1" and http.state["tokens"] == 2           # 401 -> new token -> retried once


def test_resolve_exact_ids_and_failures():
    http = fake({"example_streamer": ("100200300", "Example_Streamer"), "12345678": ("99", "NumericName")})
    cl = TwitchClient(Mem(), http=http, now=Clock())
    u = cl.resolve("https://www.twitch.tv/Example_Streamer")
    assert u == TwitchUser("100200300", "example_streamer", "Example_Streamer")
    assert cl.resolve("12345678").id == "99"                                       # digits: login first
    calls = []
    cl = TwitchClient(Mem(), http=fake({"someone": ("5500055", "Someone")}, calls=calls), now=Clock())
    assert cl.resolve("5500055").id == "5500055"                                   # digits: then Twitch user ID
    with pytest.raises(ProviderError, match="no channel named"):
        cl.resolve("nobodyherexyz")
    with pytest.raises(ValueError):
        cl.resolve("not a name!")


def test_429_message_and_server_errors_propagate():
    cl = TwitchClient(Mem(), http=fake(stream_errors=[err(429)]), now=Clock())
    with pytest.raises(ProviderError, match="rate limit"):
        cl.streams(["1"])
    cl = TwitchClient(Mem(), http=fake(stream_errors=[err(500)]), now=Clock())
    with pytest.raises(ProviderError):
        cl.streams(["1"])


def test_streams_batches_100_and_returns_only_live():
    calls = []
    live = {str(i): {"title": f"t{i}", "game": "Sea of Thieves", "viewers": i} for i in (3, 150, 249)}
    cl = TwitchClient(Mem(), http=fake(live=live, calls=calls), now=Clock())
    out = cl.streams([str(i) for i in range(250)] + ["3"])                          # duplicates are ignored
    stream_calls = [u for u in calls if "/streams" in u]
    assert len(stream_calls) == 3 and CHUNK == 100                                  # 250 ids = 3 requests
    assert set(out) == {"3", "150", "249"} and out["150"]["viewers"] == 150 and out["3"]["started"]
    assert cl.streams([]) == {}


def seed_links(db, now_clock=None):
    db.merge_players([Player("Live", "1"), Player("Off", "2"), Player("NoLink", "3")], "openxbl", True, now=100)
    ids = {r["gamertag"]: r["id"] for r in db.list_accounts()}
    db.link_twitch(ids["Live"], "100200300", "example_streamer", "Example_Streamer")
    db.link_twitch(ids["Off"], "777", "quiet_one", "Quiet_One")
    return ids


def test_checker_stores_live_and_offline_and_skips_unlinked(db):
    ids = seed_links(db)
    calls = []
    cl = TwitchClient(Mem(), http=fake(live={"100200300": {"title": "Evening Stream", "game": "Supermarket Together",
                                                           "viewers": 7}}, calls=calls), now=Clock())
    ck = TwitchChecker(db, cl, now=lambda: time.time())
    out = ck.check_accounts()
    assert [u for u in calls if "/streams" in u] and out[ids["Live"]]["live"] and not out[ids["Off"]]["live"]
    assert ids["NoLink"] not in out
    rows = {r["gamertag"]: r for r in db.list_accounts()}
    assert rows["Live"]["twitch_live"] == 1 and rows["Live"]["twitch_viewers"] == 7
    assert rows["Live"]["twitch_game"] == "Supermarket Together" and rows["Live"]["twitch_name"] == "Example_Streamer"
    assert rows["Off"]["twitch_live"] == 0 and rows["Off"]["twitch_name"] == "Quiet_One"
    assert rows["NoLink"]["twitch_name"] is None and rows["NoLink"]["twitch_live"] is None
    only = ck.check_accounts([ids["Off"]])                                         # checking a subset
    assert set(only) == {ids["Off"]}


def test_checker_without_links_makes_no_request(db):
    calls = []
    cl = TwitchClient(Mem(), http=fake(calls=calls), now=Clock())
    assert TwitchChecker(db, cl).check_accounts() == {} and calls == []


def test_checker_error_keeps_old_status(db):
    ids = seed_links(db)
    ok = TwitchClient(Mem(), http=fake(live={"100200300": {"title": "t", "game": "g", "viewers": 1}}), now=Clock())
    TwitchChecker(db, ok).check_accounts()
    bad = TwitchClient(Mem(), http=fake(stream_errors=[err(500)]), now=Clock())
    with pytest.raises(ProviderError):
        TwitchChecker(db, bad).check_accounts()
    assert {r["gamertag"]: r["twitch_live"] for r in db.list_accounts()}["Live"] == 1   # untouched by the failure


def test_link_unlink_relink_and_cascade(db):
    ids = seed_links(db)
    db.store_twitch_status([(ids["Live"], 5, 1, "t", "g", 3, 4)])
    db.link_twitch(ids["Live"], "888", "new_name", "New_Name")                      # relink: old status is dropped
    row = {r["gamertag"]: r for r in db.list_accounts()}["Live"]
    assert row["twitch_id"] == "888" and row["twitch_live"] is None
    db.unlink_twitch([ids["Live"], ids["Off"]])
    assert db.twitch_links() == [] and db.twitch_links([]) == []
    db.link_twitch(ids["Live"], "1", "a", "A")
    db.conn.execute("DELETE FROM accounts WHERE id=?", (ids["Live"],))
    db.conn.commit()
    assert db.twitch_links() == []                                                  # deleted with the account


def test_twitch_tables_added_by_migration(tmp_path):
    from brpw.db import MIGRATIONS
    d = Database(tmp_path / "p.db", migrations=MIGRATIONS[:7])
    d.close()
    d2 = Database(tmp_path / "p.db")
    assert d2.schema_version() == len(MIGRATIONS)
    assert d2.conn.execute("SELECT COUNT(*) FROM twitch_links").fetchone()[0] == 0


def test_stale_status_is_not_trusted():
    from brpw.ui import twitch_live
    now = 10_000
    row = lambda live, at: {"twitch_live": live, "twitch_checked": at}
    assert twitch_live(row(1, now - 5), now) and not twitch_live(row(1, now - STATUS_MAX_AGE - 1), now)
    assert not twitch_live(row(0, now), now) and not twitch_live(row(None, None), now)


def test_login_candidates():
    from brpw.twitch import login_candidates
    assert login_candidates("Salty Pirate") == ["saltypirate", "salty_pirate"]
    assert login_candidates("Cool_Pirate") == ["cool_pirate"]
    assert login_candidates("Weird!Name#1234") == ["weirdname1234"]
    assert login_candidates("ab") == [] and login_candidates("") == []


def test_suggest_finds_same_name_channels_and_live_status():
    users = {"examplestreamer": ("100200300", "ExampleStreamer"), "other_one": ("555", "Other_One")}
    live = {"100200300": {"title": "t", "game": "Sea of Thieves", "viewers": 12}}
    calls = []
    cl = TwitchClient(Mem(), http=fake(users, live, calls), now=Clock())
    out = cl.suggest([(1, "Example Streamer"), (2, "Other One"), (3, "Nobody Here Zz")])
    by = {o["account_id"]: o for o in out}
    assert set(by) == {1, 2}
    assert by[1]["live"] and by[1]["sot"] and by[1]["viewers"] == 12 and by[1]["login"] == "examplestreamer"
    assert not by[2]["live"] and by[2]["name"] == "Other_One"
    assert sum(1 for u in calls if "/users" in u) == 1                        # all candidates in one request
