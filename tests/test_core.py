import json
import sqlite3
import tkinter as tk

import pytest

from brpw import sotrep, updater
from brpw.db import Database, Player
from brpw.providers import ManualImportProvider, ProviderError, RecentPlayersProvider, parse_import
from brpw.sync import SyncService


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p.db")
    yield d
    try:
        d.close()
    except sqlite3.ProgrammingError:
        pass


def test_creation_and_persistence(tmp_path):
    d = Database(tmp_path / "p.db")
    d.merge_players([Player("Alice", "1")], "xbox-live", True, now=100)
    d.set_setting("interval", "90")
    d.close()
    d2 = Database(tmp_path / "p.db")
    assert d2.count_accounts() == 1 and d2.get_setting("interval") == "90"
    assert d2.schema_version() >= 1


def test_duplicates_and_new_vs_known(db):
    r1 = db.merge_players([Player("Alice", "1"), Player("Alice", "1"), Player("Bob", "2")], "xbox-live", True, now=100)
    assert (r1.new, r1.known) == (2, 0)
    r2 = db.merge_players([Player("Alice", "1"), Player("Carl", "3")], "xbox-live", True, now=200)
    assert (r2.new, r2.known) == (1, 1)
    assert db.count_accounts() == 3


def test_live_refresh_does_not_spam_encounters(db):
    for t in (100, 160, 220):
        db.merge_players([Player("Alice", "1")], "xbox-live", True, now=t)
    acc = db.list_accounts()[0]
    assert len(db.encounters_for(acc["id"])) == 1
    db.merge_players([], "xbox-live", True, now=300)          # Alice leaves the list
    db.merge_players([Player("Alice", "1")], "xbox-live", True, now=400)   # and returns
    assert len(db.encounters_for(acc["id"])) == 2


def test_history_kept_outside_latest_list(db):
    db.merge_players([Player("Alice", "1")], "xbox-live", True, now=100)
    db.merge_players([Player("Bob", "2")], "xbox-live", True, now=200)
    rows = {r["gamertag"]: r for r in db.list_accounts()}
    assert rows["Alice"]["in_latest"] == 0 and rows["Bob"]["in_latest"] == 1


def test_gamertag_change_with_xuid(db):
    db.merge_players([Player("OldTag", "42")], "xbox-live", True, now=100)
    r = db.merge_players([Player("NewTag", "42")], "xbox-live", True, now=200)
    assert (r.new, r.known) == (0, 1)
    rows = db.list_accounts()
    assert len(rows) == 1 and rows[0]["gamertag"] == "NewTag"
    hist = db.conn.execute("SELECT gamertag FROM gamertag_history").fetchall()
    assert {h[0] for h in hist} == {"OldTag", "NewTag"}


def test_similar_gamertags_never_merged(db):
    db.merge_players([Player("Pirate One", "1"), Player("PirateOne", "2"), Player("pirate one")], "import", False)
    assert db.count_accounts() == 3


def test_group_accounts_keeps_separate_history(db):
    db.merge_players([Player("Main", "1"), Player("Alt", "2")], "xbox-live", True, now=100)
    ids = [r["id"] for r in db.list_accounts()]
    db.group_accounts(ids, "Dave")
    rows = db.list_accounts()
    assert {r["person"] for r in rows} == {"Dave"} and len(rows) == 2
    assert len(db.encounters_for(ids[0])) == 1
    db.ungroup_accounts([ids[0]])
    assert sum(1 for r in db.list_accounts() if r["person"]) == 1
    db.ungroup_accounts([ids[1]])
    assert db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


def test_provided_timestamps_dedupe(db):
    p = [Player("A", "1", observed_at=500)]
    db.merge_players(p, "import", False, now=900)
    db.merge_players(p, "import", False, now=950)
    acc = db.list_accounts()[0]
    assert len(db.encounters_for(acc["id"])) == 1 and acc["last_seen"] == 500


def test_search_and_sort(db):
    db.merge_players([Player("Zed", "1")], "xbox-live", True, now=100)
    db.merge_players([Player("Amy", "2")], "xbox-live", True, now=200)
    assert [r["gamertag"] for r in db.list_accounts()] == ["Amy", "Zed"]
    assert [r["gamertag"] for r in db.list_accounts("ze")] == ["Zed"]


def test_migration_preserves_data_and_backs_up(tmp_path):
    def m2(c):
        c.execute("ALTER TABLE accounts ADD COLUMN note TEXT")
    from brpw.db import MIGRATIONS
    d = Database(tmp_path / "p.db")
    d.merge_players([Player("A", "1")], "import", False)
    d.close()
    d2 = Database(tmp_path / "p.db", migrations=MIGRATIONS + [m2])
    assert d2.schema_version() == len(MIGRATIONS) + 1 and d2.count_accounts() == 1
    assert list((tmp_path / "backups").glob("*premigrate*"))


def test_failed_migration_rolls_back(tmp_path):
    from brpw.db import MIGRATIONS

    def bad(c):
        c.execute("ALTER TABLE accounts ADD COLUMN x TEXT")
        raise RuntimeError("boom")
    Database(tmp_path / "p.db").close()
    with pytest.raises(RuntimeError):
        Database(tmp_path / "p.db", migrations=MIGRATIONS + [bad])
    assert Database(tmp_path / "p.db").schema_version() == len(MIGRATIONS)


def test_newer_schema_refused(tmp_path):
    Database(tmp_path / "p.db").close()
    c = sqlite3.connect(tmp_path / "p.db")
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(RuntimeError):
        Database(tmp_path / "p.db")


def test_corrupt_db_recovers_from_backup(tmp_path):
    d = Database(tmp_path / "p.db")
    d.merge_players([Player("A", "1")], "import", False)
    d.backup()
    d.close()
    (tmp_path / "p.db").write_bytes(b"this is not sqlite" * 100)
    d2 = Database(tmp_path / "p.db")
    assert d2.recovered_from and d2.count_accounts() == 1


def test_corrupt_db_without_backup_starts_fresh(tmp_path):
    (tmp_path / "p.db").write_bytes(b"garbage" * 100)
    d = Database(tmp_path / "p.db")
    assert d.recovered_from and d.count_accounts() == 0


def test_export(db, tmp_path):
    db.merge_players([Player("A", "1")], "import", False)
    db.export(tmp_path / "o.csv")
    db.export(tmp_path / "o.json")
    assert "A" in (tmp_path / "o.csv").read_text()
    assert json.loads((tmp_path / "o.json").read_text())[0]["gamertag"] == "A"


# ---- sotrep
def test_sotrep_links():
    assert sotrep.profile_url("Cool Pirate") == "https://www.sotrep.com/search/xbox-cool-pirate"
    assert sotrep.slug("Weird_Name#1234!") == "xbox-weirdname1234"


# ---- providers
def test_parse_import_formats():
    assert [p.gamertag for p in parse_import("gamertag,xuid\nAlice,123\nBob")] == ["Alice", "Bob"]
    assert parse_import("Alice,123")[0].xuid == "123"
    assert parse_import('["A", {"gamertag": "B", "xuid": "9"}]')[1].xuid == "9"
    with pytest.raises(ProviderError):
        parse_import("[oops")
    with pytest.raises(ProviderError):
        ManualImportProvider("   ").fetch()


# ---- sync failures / recovery
class Flaky(RecentPlayersProvider):
    name, live = "xbox-live", True

    def __init__(self):
        self.fail = True

    def fetch(self):
        if self.fail:
            raise ProviderError("offline")
        return [Player("A", "1")]


def test_sync_failure_keeps_data_then_recovers(db):
    db.merge_players([Player("Old", "9")], "xbox-live", True, now=1)
    svc, prov = SyncService(db), Flaky()
    out = svc.run(prov)
    assert not out.ok and "offline" in out.message
    assert db.count_accounts() == 1 and db.get_setting("last_sync") is None
    prov.fail = False
    out = svc.run(prov)
    assert out.ok and out.result.new == 1 and db.get_setting("last_sync")


def test_no_overlapping_sync(db):
    svc = SyncService(db)
    svc._lock.acquire()
    assert svc.run(Flaky()) is None


def test_unexpected_exception_is_failure(db):
    class Boom(Flaky):
        def fetch(self):
            raise ValueError("bad")
    assert not SyncService(db).run(Boom()).ok


# ---- updater
def test_version_compare():
    assert updater.is_newer("v0.2.0", "0.1.9")
    assert updater.is_newer("1.0", "0.9.9.9")
    assert not updater.is_newer("0.1.0", "0.1.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("latest")


def test_parse_release():
    from brpw import REPO
    good = {"tag_name": "v9.0.0", "body": "notes", "html_url": f"https://github.com/{REPO}/releases/tag/v9.0.0"}
    assert updater.parse_release(good, "0.1.0").version == "9.0.0"
    assert updater.parse_release({**good, "prerelease": True}, "0.1.0") is None
    assert updater.parse_release(good, "9.0.0") is None
    with pytest.raises(updater.UpdateError):
        updater.parse_release({**good, "html_url": "https://evil.example/x"}, "0.1.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_release({"nope": 1})
    with pytest.raises(updater.UpdateError):
        updater.parse_release({**good, "tag_name": "garbage"}, "0.1.0")


# ---- app startup / shutdown
def test_app_startup_shutdown(tmp_path, monkeypatch):
    from brpw import ui
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    d = Database(tmp_path / "p.db")
    d.set_setting("auto_update_check", "0")
    monkeypatch.setenv("BRPW_DATA_DIR", str(tmp_path))
    d.merge_players([Player("Old", "1")], "openxbl", True, now=100)
    d.merge_players([Player("New", "2"), Player("New2", "3")], "openxbl", True, now=200)
    app = ui.App(root, d)
    root.update()
    names = lambda: [app.tree.set(i, "gamertag") for i in app.tree.get_children()]
    assert names() == ["New", "New2", "Old"]          # Xbox list order first, then older history
    app.sort_by("last_seen")
    assert names() == ["New", "New2", "Old"]          # newest first
    app.sort_by("last_seen")
    assert names() == ["Old", "New2", "New"]          # toggled: oldest first
    app.sort_by("gamertag")
    assert names() == ["New", "New2", "Old"]          # A-Z
    app.set_theme(3)
    assert d.get_setting("theme") == "3" and app.pal["accent"] == "#e0a040" and app.pal["bg"] == "#2a1d12"
    app.set_theme(99)                                  # out of range falls back to the default
    assert app.theme == 8
    app.on_close()
    assert app.closing and app.after_id is None


# ---- OpenXBL
def test_parse_openxbl_filters_and_keeps_timestamps():
    from brpw.providers import parse_openxbl
    payload = {"content": {"recentPlayers": [
        {"xuid": "1", "gamertag": "A", "titleName": "Sea of Thieves", "timestamp": "2024-01-15T18:30:00.0000000Z"},
        {"xuid": "2", "gamertag": "B", "titleName": "Halo Infinite", "timestamp": "2024-01-15T18:30:00Z"}]}}
    out = parse_openxbl(payload)
    assert [p.gamertag for p in out] == ["A"] and out[0].observed_at == 1705343400
    with pytest.raises(ProviderError):
        parse_openxbl({"content": {}})


def test_openxbl_provider_key_and_errors(tmp_path):
    from brpw.providers import OpenXblKey, OpenXblProvider
    key = OpenXblKey(tmp_path / "k.json")
    with pytest.raises(ProviderError) as ei:
        OpenXblProvider(key, http=lambda *a, **k: {}).fetch()
    assert ei.value.reauth
    key.add("A", " abc ")
    seen = {}

    def http(url, data, headers, form=False):
        seen.update(headers)
        return {"content": {"recentPlayers": [{"xuid": "1", "gamertag": "A"}]}}
    assert OpenXblProvider(key, http=http).fetch()[0].gamertag == "A" and seen["X-Authorization"] == "abc"

    def rate(url, data, headers, form=False):
        e = ProviderError("HTTP 429")
        e.code = 429
        raise e
    with pytest.raises(ProviderError, match="rate limit"):
        OpenXblProvider(key, http=rate).fetch()


def test_parse_openxbl_finds_list_anywhere_and_reports_shape():
    from brpw.providers import parse_openxbl
    assert parse_openxbl({"people": [{"xuid": "1", "gamertag": "A"}]})[0].gamertag == "A"
    assert parse_openxbl({"content": {"data": {"x": [{"gamertag": "B"}]}}})[0].gamertag == "B"
    with pytest.raises(ProviderError) as ei:
        parse_openxbl({"code": 200, "content": {"people": []}, "secret": "SHOULD_NOT_APPEAR"})
    assert "people: list[0]" in str(ei.value) and "SHOULD_NOT_APPEAR" not in str(ei.value)


# ---- multiple OpenXBL accounts
def test_openxbl_multiple_accounts(tmp_path):
    from brpw.providers import OpenXblKey, parse_account_gamertag
    ks = OpenXblKey(tmp_path / "k.json")
    assert ks.get() == "" and ks.active() is None
    assert ks.add("Main", " key1 ") == "Main"
    assert ks.add("Main", "key2") == "Main (2)"
    assert ks.add("X", "key1") == "Main"          # same key: switch, no duplicate
    assert ks.names() == ["Main", "Main (2)"] and ks.get() == "key1"
    ks.set_active("Main (2)")
    assert OpenXblKey(tmp_path / "k.json").get() == "key2"   # persisted
    ks.remove("Main (2)")
    assert ks.active() == "Main" and ks.get() == "key1"


def test_openxbl_legacy_key_migrates(tmp_path):
    from brpw.providers import OpenXblKey
    (tmp_path / "openxbl.key").write_text("oldkey")
    ks = OpenXblKey(tmp_path / "openxbl_keys.json")
    assert ks.get() == "oldkey" and ks.names() == ["Account 1"]


def test_parse_account_gamertag():
    from brpw.providers import parse_account_gamertag
    ok = {"content": {"profileUsers": [{"settings": [{"id": "Gamertag", "value": "Me"}]}]}}
    assert parse_account_gamertag(ok) == "Me"
    assert parse_account_gamertag({"nope": 1}) is None


def test_openxbl_stats_counts_only():
    from brpw.providers import openxbl_stats
    s = openxbl_stats({"content": {"recentPlayers": [
        {"gamertag": "SECRETNAME", "titleName": "Sea of Thieves", "timestamp": "2024-01-15T18:30:00Z"},
        {"gamertag": "B", "titleName": "Halo"}]}})
    assert s == "received 2, with game name 2, with time 1, Sea of Thieves 1" and "SECRETNAME" not in s


def test_list_position_orders_ties(db):
    db.merge_players([Player("First", "1"), Player("Second", "2"), Player("Third", "3")], "openxbl", True, now=100)
    rows = db.list_accounts()
    assert [r["gamertag"] for r in rows] == ["First", "Second", "Third"] and rows[0]["pos"] == 0
    db.merge_players([Player("Second", "2")], "openxbl", True, now=200)
    assert {r["gamertag"]: r["pos"] for r in db.list_accounts()}["First"] is None



def test_theme_assets_exist_and_are_png():
    from brpw.themes import THEMES, icon_path, clamp
    assert len(THEMES) == 9 and clamp("x") == 8 and clamp("0") == 8 and clamp("9") == 9
    for n in range(1, 10):
        assert icon_path(n).read_bytes()[1:4] == b"PNG"


def test_palettes_are_valid_hex():
    import re
    from brpw.themes import palette
    for n in range(1, 10):
        for k, v in palette(n).items():
            if k != "name":
                assert re.fullmatch(r"#[0-9a-f]{6}", v), (n, k, v)
