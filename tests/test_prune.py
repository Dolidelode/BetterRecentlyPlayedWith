import os
import time

from brpw import chat_ui
from brpw.db import Database, Player

DAY = 86400
NOW = 1_800_000_000


def ids(db):
    return {r[1]: r[0] for r in db.conn.execute("SELECT id, gamertag FROM accounts")}


def test_old_players_are_forgotten_but_tags_own_links_and_the_master_list_stay(tmp_path):
    d = Database(tmp_path / "p.db")
    old = NOW - 30 * DAY
    names = ["Gone", "Tagged", "OwnLink", "SharedOwn", "Never", "Recent", "WithProfile"]
    d.merge_players([Player(n, str(2535400000000000 + i), old) for i, n in enumerate(names)], "import", False, now=old)
    d.merge_players([Player("Current", "2535400000000099")], "openxbl", True, now=old, owner="Main")  # in a list now
    d.merge_players([Player("Recent", "2535400000000005", NOW - 2 * DAY)], "import", False, now=NOW - 2 * DAY)
    i = ids(d)
    d.add_tag([i["Tagged"]], "crew")
    d.link_twitch(i["OwnLink"], "40", "own_pick", "Own_Pick")                 # not on the master list yet
    d.link_twitch(i["SharedOwn"], "41", "shared_pick", "Shared_Pick")         # already on the master list
    d.replace_shared([{"xuid": "2535400000000003", "gamertag": "SharedOwn", "twitch_id": "41",
                       "twitch_login": "shared_pick"}])
    d.set_twitch_never([i["Never"]], "cheater")
    d.save_minutes(["2535400000000006"], {"2535400000000006": 600}, now=old)
    gone = d.prune(14, NOW)
    left = set(ids(d))
    assert left == {"Tagged", "OwnLink", "Never", "Recent", "Current"} and gone == 3
    assert d.conn.execute("SELECT COUNT(*) FROM shared_links").fetchone()[0] == 1     # the master list is kept
    assert d.conn.execute("SELECT COUNT(*) FROM xbox_profiles").fetchone()[0] == 0    # with their players
    assert d.conn.execute("SELECT COUNT(*) FROM encounters e WHERE NOT EXISTS "
                          "(SELECT 1 FROM accounts a WHERE a.id = e.account_id)").fetchone()[0] == 0
    assert d.prune(0, NOW) == 0                                                     # "Never": nothing forgotten
    d.merge_players([Player("SharedOwn", "2535400000000003")], "import", False, now=NOW)   # seen again:
    d.apply_shared()                                                                # as after every sync
    row = d.conn.execute("SELECT l.login FROM twitch_links l JOIN accounts a ON a.id = l.account_id "
                         "WHERE a.gamertag = 'SharedOwn'").fetchone()
    assert tuple(row) == ("shared_pick",)                                                # the channel comes back
    d.close()


def test_unused_pictures_are_deleted(tmp_path):
    pics = chat_ui.Pictures(tmp_path, lambda fn: fn(), fetch=lambda u: b"")
    old, new = tmp_path / "old.png", tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 20 * DAY, time.time() - 20 * DAY))
    assert pics.prune(14) == 1 and not old.exists() and new.exists()
    pics.pool.shutdown()


def test_crewmates_are_tagged_after_two_sessions_once(tmp_path):
    d = Database(tmp_path / "c.db")
    salty, kraken = {"xuid": "77", "gamertag": "Salty"}, {"xuid": "78", "gamertag": "Kraken"}
    assert d.note_crew("Main", "s1", [salty, kraken], NOW) == []                # one session: not yet
    assert d.note_crew("Main", "s1", [salty], NOW + 60) == []                  # the same session again: no count
    assert d.note_crew("Main", "s2", [salty], NOW + 3600) == ["Salty"]         # a second session: crew
    tags = {a["gamertag"]: a["tags"] for a in d.list_accounts()}
    assert tags["Salty"] == "crew" and "Kraken" not in tags
    sid = [a["id"] for a in d.list_accounts() if a["gamertag"] == "Salty"]
    d.remove_tag(sid, "crew")                                                  # untagged by you: stays untagged
    assert d.note_crew("Main", "s3", [salty], NOW + 7200) == []
    d.close()
