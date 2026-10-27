import pytest

from brpw.automatch import HITS_NEEDED, MISS_AFTER, PRESENCE_MAX_AGE, AutoMatcher, core, similar
from brpw.db import Database, Player


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "m.db")
    d.merge_players([Player("Salty Pirate", "1"), Player("Other Guy", "2"), Player("Cheaty One", "3")],
                    "openxbl", True, now=100)
    yield d
    d.close()


def ids(db):
    return {r["gamertag"]: r["id"] for r in db.list_accounts()}


def presence(db, aid, state, title, at):
    with db.lock, db.conn:
        db.conn.execute("INSERT OR REPLACE INTO live_status(account_id,checked_at,state,title) VALUES(?,?,?,?)",
                        (aid, at, state, title))


def stream(i, login, started):
    return {"id": i, "login": login, "name": login.title(), "started": started, "title": "", "viewers": 1,
            "language": "en"}


def test_names():
    assert core("xXSaltyPirateXx") == "saltypirate" and core("ttv_saltypirate") == "saltypirate"
    assert core("SaltyPirate_live") == "saltypirate" and core("itsSaltyPirate") == "saltypirate"
    for gt, tw in (("Salty Pirate", "saltypirate"), ("xXSaltyPirateXx", "saltypirate_ttv"),
                   ("Salty Pirate", "saltypirate2"), ("SaltyPirate", "saltypirat3")):
        assert similar(gt, tw), (gt, tw)
    for gt, tw in (("Salty Pirate", "pirate"), ("Bob", "bob"), ("Salty Pirate", "grumpysailor"),
                   ("Pirate", "saltypiratekingofthewaves")):
        assert not similar(gt, tw), (gt, tw)


def run(db, clock, streams):
    return AutoMatcher(db, client=None, now=clock).run(streams)


def test_links_only_after_enough_different_streams(db):
    i, c = ids(db), Clock()
    for n in range(HITS_NEEDED):
        started = c.t
        c.t += 15 * 60
        presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t)
        made = run(db, c, [stream("77", "saltypirate", started)])
        run(db, c, [stream("77", "saltypirate", started)])            # same stream twice still counts once
        assert bool(made) == (n == HITS_NEEDED - 1)
        c.t += 3600
    link = db.twitch_links([i["Salty Pirate"]])[0]
    assert link["twitch_id"] == "77" and link["source"] == "auto"
    row = {r["gamertag"]: r for r in db.list_accounts()}["Salty Pirate"]
    assert row["twitch_source"] == "auto"


def test_same_name_different_person_is_ruled_out(db):
    """The channel streams Sea of Thieves while this player is offline: not the same person, never linked."""
    i, c = ids(db), Clock()
    for n in range(HITS_NEEDED + 2):
        started = c.t
        c.t += 15 * 60
        state = ("Offline", "") if n == 1 else ("Online", "Sea of Thieves")
        presence(db, i["Salty Pirate"], *state, c.t)
        run(db, c, [stream("77", "saltypirate", started)])
        c.t += 3600
    assert db.twitch_links([i["Salty Pirate"]]) == []
    m = db.match_summary()[0]
    assert m["hits"] == HITS_NEEDED + 1 and m["misses"] == 1


def test_playing_another_game_counts_against(db):
    i, c = ids(db), Clock()
    started = c.t
    c.t += MISS_AFTER + 60
    presence(db, i["Salty Pirate"], "Online", "Minecraft", c.t)
    run(db, c, [stream("77", "saltypirate", started)])
    assert db.match_summary()[0]["misses"] == 1


def test_stream_start_and_end_are_forgiven(db):
    i, c = ids(db), Clock()
    started = c.t
    c.t += 60                                                         # stream just started, still loading in
    presence(db, i["Salty Pirate"], "Online", "Home", c.t)
    run(db, c, [stream("77", "saltypirate", started)])
    assert db.match_summary() == []                                   # too early to count against them
    c.t += 30 * 60
    presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t)
    run(db, c, [stream("77", "saltypirate", started)])
    c.t += 30 * 60
    presence(db, i["Salty Pirate"], "Offline", "", c.t)               # left the game, stream still running
    run(db, c, [stream("77", "saltypirate", started)])
    m = db.match_summary()[0]
    assert m["hits"] == 1 and m["misses"] == 0                        # in the game during that stream: a hit


def test_stale_presence_away_never_and_unrelated_names_are_ignored(db):
    i, c = ids(db), Clock()
    started = c.t
    c.t += 15 * 60
    presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t - PRESENCE_MAX_AGE - 1)
    presence(db, i["Other Guy"], "Away", "Sea of Thieves", c.t)
    presence(db, i["Cheaty One"], "Online", "Sea of Thieves", c.t)
    db.set_twitch_never([i["Cheaty One"]], "cheater")
    run(db, c, [stream("77", "saltypirate", started), stream("78", "otherguy", started),
                stream("79", "cheatyone", started), stream("80", "someone_else", started)])
    assert db.match_summary() == []


def test_removing_an_auto_link_rules_it_out(db):
    i, c = ids(db), Clock()
    for _ in range(HITS_NEEDED):
        started = c.t
        c.t += 15 * 60
        presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t)
        run(db, c, [stream("77", "saltypirate", started)])
        c.t += 3600
    assert db.twitch_links([i["Salty Pirate"]])
    db.unlink_twitch([i["Salty Pirate"]])
    for _ in range(HITS_NEEDED):
        started = c.t
        c.t += 15 * 60
        presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t)
        assert run(db, c, [stream("77", "saltypirate", started)]) == []
        c.t += 3600
    m = db.match_summary()[0]
    assert m["rejected"] == 1 and db.twitch_links([i["Salty Pirate"]]) == []
    db.link_twitch(i["Other Guy"], "90", "x", "X")                   # manual links never record a rejection
    db.unlink_twitch([i["Other Guy"]])
    assert all(r["account_id"] != i["Other Guy"] for r in db.match_summary())


def test_channel_already_linked_to_someone_else_is_not_reused(db):
    i, c = ids(db), Clock()
    db.link_twitch(i["Other Guy"], "77", "saltypirate", "SaltyPirate")
    for _ in range(HITS_NEEDED):
        started = c.t
        c.t += 15 * 60
        presence(db, i["Salty Pirate"], "Online", "Sea of Thieves", c.t)
        run(db, c, [stream("77", "saltypirate", started)])
        c.t += 3600
    assert db.twitch_links([i["Salty Pirate"]]) == []


def test_no_players_means_no_twitch_request(db):
    class NoCalls:
        def game_streams(self, **k):
            raise AssertionError("should not ask Twitch")
    assert AutoMatcher(db, NoCalls(), now=Clock()).run() == []
