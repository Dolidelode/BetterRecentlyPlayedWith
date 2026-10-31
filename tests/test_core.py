import json
import sqlite3
import tkinter as tk
from tkinter import ttk

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


def test_tags_are_manual_searchable_and_keep_history(db):
    db.merge_players([Player("Main", "1"), Player("Alt", "2"), Player("Other", "3")], "xbox-live", True, now=100)
    ids = {r["gamertag"]: r["id"] for r in db.list_accounts()}
    db.add_tag([ids["Main"], ids["Alt"]], "  Dave,  crew ")          # cleaned: no commas, single spaces
    db.add_tag([ids["Main"]], "dave crew")                             # same tag, different case/spacing rules: no duplicate
    rows = {r["gamertag"]: r["tags"] for r in db.list_accounts()}
    assert rows["Main"] == "Dave crew" and rows["Alt"] == "Dave crew" and rows["Other"] is None
    assert {r["gamertag"] for r in db.list_accounts("DAVE")} == {"Main", "Alt"}   # search matches tags, any case
    assert db.all_tags() == ["Dave crew"] and db.tags_for([ids["Other"]]) == []
    assert len(db.encounters_for(ids["Main"])) == 1                    # accounts keep separate histories
    db.add_tag([ids["Alt"]], "friend of mine")
    assert db.tags_for([ids["Alt"]]) == ["Dave crew", "friend of mine"]
    db.remove_tag([ids["Alt"]], "Dave crew")
    assert db.tags_for([ids["Alt"]]) == ["friend of mine"]
    with pytest.raises(ValueError):
        db.add_tag([ids["Main"]], " , ")


def test_encounters_for_many_combines_accounts(db):
    db.merge_players([Player("A", "1", observed_at=100), Player("B", "2", observed_at=300)], "import", False)
    db.merge_players([Player("A", "1", observed_at=200)], "import", False)
    ids = [r["id"] for r in db.list_accounts()]
    rows = db.encounters_for_many(ids)
    assert [(r["gamertag"], r["observed_at"]) for r in rows] == [("B", 300), ("A", 200), ("A", 100)]
    assert db.encounters_for_many([]) == []


def test_existing_groups_become_tags_on_migration(tmp_path):
    from brpw.db import MIGRATIONS
    d = Database(tmp_path / "p.db", migrations=MIGRATIONS[:4])        # an older install (schema 4)
    for i, n in enumerate("AB"):                                      # raw rows: the old schema has no newer tables
        d.conn.execute("INSERT INTO accounts(xuid,gamertag,gamertag_key,first_seen,last_seen,source) "
                       "VALUES(?,?,?,1,1,'import')", (str(i + 1), n, n.casefold()))
    d.conn.execute("INSERT INTO people(id,name,created_at) VALUES(1,'Old group',0)")
    d.conn.execute("UPDATE accounts SET person_id=1")
    d.conn.commit()
    d.close()
    d2 = Database(tmp_path / "p.db")
    assert d2.schema_version() == len(MIGRATIONS) and d2.all_tags() == ["Old group"]
    assert {r["tags"] for r in d2.list_accounts()} == {"Old group"}


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
    assert sotrep.twitch_search_url(" Example_Streamer ") == "https://www.sotrep.com/search/twitch-example-streamer"


# ---- providers
def test_find_gamertag_exact_only(tmp_path):
    from brpw.providers import OPENXBL_SEARCH_URL, find_gamertag
    seen = []

    def http(url, data, headers):
        seen.append((url, headers["X-Authorization"]))
        return {"content": {"people": [{"xuid": "1", "gamertag": "Salty Pirate2"},
                                       {"xuid": "2", "gamertag": "Salty  Pirate", "modernGamertag": "Salty Pirate"}]}}
    p, near = find_gamertag(" key ", "salty pirate", http)
    assert p == Player("Salty  Pirate", "2") and near == []
    assert seen[0] == (OPENXBL_SEARCH_URL + "salty%20pirate", "key")
    p, near = find_gamertag("key", "Salty Pirat", http)
    assert p is None and near == ["Salty Pirate2", "Salty  Pirate"]              # never picks a near match
    assert find_gamertag("key", "  ", http) == (None, [])
    d = Database(tmp_path / "e.db")
    d.merge_players([Player("Imported Name")], "import", False)
    aid = d.ensure_account("Imported Name", "77")                               # imported name gets its XUID
    assert d.ensure_account("Imported Name", "77") == aid and d.count_accounts() == 1
    assert d.ensure_account("New One", "78") != aid and d.count_accounts() == 2
    d.close()


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


def test_in_app_update_download_verify_install(tmp_path):
    import hashlib
    from brpw import REPO
    base = f"https://github.com/{REPO}/releases/download/v9.0.0/"
    good = {"tag_name": "v9.0.0", "html_url": f"https://github.com/{REPO}/releases/tag/v9.0.0", "assets": [
        {"name": "BetterRecentlyPlayedWith.exe", "browser_download_url": base + "BetterRecentlyPlayedWith.exe"},
        {"name": "SHA256SUMS.txt", "browser_download_url": base + "SHA256SUMS.txt"},
        {"name": "other.zip", "browser_download_url": base + "other.zip"}]}
    info = updater.parse_release(good, "0.1.0")
    assert info.exe_url.endswith(".exe") and info.sums_url.endswith("SUMS.txt")
    evil = {**good, "assets": [{"name": "BetterRecentlyPlayedWith.exe", "browser_download_url": "https://evil.example/x.exe"}]}
    assert updater.parse_release(evil, "0.1.0").exe_url == ""               # only files from the official repository
    new_exe = b"MZ new program"
    sums = f"{hashlib.sha256(new_exe).hexdigest().upper()}  BetterRecentlyPlayedWith.exe\n".encode()
    files = {info.exe_url: new_exe, info.sums_url: sums}
    exe = tmp_path / "BetterRecentlyPlayedWith.exe"
    exe.write_bytes(b"MZ old program")
    new = updater.download(info, exe, fetch=lambda u, limit: files[u])
    updater.install(new, exe)
    assert exe.read_bytes() == new_exe and (tmp_path / "BetterRecentlyPlayedWith.exe.old").read_bytes() == b"MZ old program"
    updater.cleanup(exe)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["BetterRecentlyPlayedWith.exe"]
    for bad in ({**files, info.exe_url: b"MZ tampered"},                     # checksum differs
                {**files, info.sums_url: b"nothing useful"},                   # no checksum for the exe
                {info.exe_url: b"not MZ", info.sums_url: f"{hashlib.sha256(b'not MZ').hexdigest()} *BetterRecentlyPlayedWith.exe".encode()}):
        with pytest.raises(updater.UpdateError):
            updater.download(info, exe, fetch=lambda u, limit, f=bad: f[u])
    assert exe.read_bytes() == new_exe and not (tmp_path / "BetterRecentlyPlayedWith.exe.new").exists()
    with pytest.raises(updater.UpdateError):
        updater.download(updater.UpdateInfo("9.0.0", "", info.url), exe)          # release without program file
    env = updater.restart_env()
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and not any(k.startswith("_MEI") for k in env)


# ---- app startup / shutdown
def test_app_startup_shutdown(tmp_path, monkeypatch):
    import time
    from brpw import ui
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    d = Database(tmp_path / "p.db")
    d.set_setting("auto_update_check", "0")
    monkeypatch.setenv("BRPW_DATA_DIR", str(tmp_path))
    d.merge_players([Player("Old", "1")], "openxbl", True, now=100, owner="Main")
    d.merge_players([Player("New", "2"), Player("New2", "3")], "openxbl", True, now=200, owner="Main")
    from brpw.providers import OpenXblKey
    OpenXblKey(tmp_path / "openxbl_keys.json").add("Main", "k")
    d.replace_friends("Main", {"2": "Friend", "3": "You follow"})
    new_id = {r["gamertag"]: r["id"] for r in d.list_accounts()}["New2"]
    now = int(time.time())                                              # New2 is confirmed live on Twitch by Xbox
    d.conn.execute("INSERT INTO live_status(account_id,checked_at,state,twitch,viewers,started,broadcast_id,broadcasting) "
                   "VALUES(?,?,?,?,?,?,?,?)", (new_id, now, "Online", 0, 5, now - 60, "b", 1))
    d.conn.commit()
    app = ui.App(root, d)
    root.update()
    tags = {app.tree.set(i, "gamertag"): app.tree.item(i, "tags") for i in app.tree.get_children()}
    assert "friend" in tags["New"] and "friend" not in tags["New2"] and "friend" not in tags["Old"]
    assert str(app.tree.tag_configure("friend", "background")) == app.pal["friend"]
    icons = {app.tree.set(i, "gamertag"): app.tree.item(i, "image") for i in app.tree.get_children()}
    assert icons["New2"] and not icons["New"] and not icons["Old"]    # Twitch icon only for the confirmed-live row
    names = lambda: [app.tree.set(i, "gamertag") for i in app.tree.get_children()]
    assert names() == ["New", "New2", "Old"]          # Xbox list order first, then older history
    app.sort_by("last_seen")
    assert names() == ["New", "New2", "Old"]          # newest first
    app.sort_by("last_seen")
    assert names() == ["Old", "New2", "New"]          # toggled: oldest first
    app.sort_by("gamertag")
    assert names() == ["New", "New2", "Old"]          # A-Z
    d.add_tag([new_id], "crew")
    app.reload()
    assert [app.tree.set(i, "tags") for i in app.tree.get_children() if app.tree.set(i, "gamertag") == "New2"] == ["crew"]
    app.search.set("crew")                              # searching finds tags
    assert names() == ["New2"]
    app.search.set("")
    # spinner: runs while a job is busy, stops when the last one finishes
    app._set_busy("sync", True)
    app._set_busy("live", True)
    assert app.spinner.job is not None
    app._set_busy("sync", False)
    assert app.spinner.job is not None
    app._set_busy("live", False)
    assert app.spinner.job is None and not app.busy
    # Encounters view: a summary row per selected player plus the recorded encounters
    app.tree.selection_set(app.tree.get_children())
    app.encounters_dialog()
    dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    assert len(dlg.summary_tv.get_children()) == 3 and len(dlg.log_tv.get_children()) == 3
    dlg.destroy()
    # dialogs must open centred over the app window (not at the screen's top-left) and must never grab input
    seen = {}

    def fake_wait(win):
        win.update()
        seen["geo"] = (win.winfo_x(), win.winfo_y(), win.winfo_width(), win.winfo_height())
        seen["grab"] = root.grab_current()
        win.destroy()
    app.root.wait_window = fake_wait
    root.deiconify()
    root.geometry("820x520+120+90")
    root.update()
    app.tree.selection_set(app.tree.get_children()[0])
    app.tag_selected()
    rx, ry, rw, rh = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    x, y, w, h = seen["geo"]
    assert rx <= x and ry <= y and x + w <= rx + rw + 20 and y + h <= ry + rh + 20, (seen, (rx, ry, rw, rh))
    assert seen["grab"] is None
    app.refresh_now = lambda *a, **k: None            # no network in tests
    app.tree.selection_set(app.tree.get_children()[0])
    app.acct_var.set("Main")
    app.switch_account()                               # switching accounts: back to the top, selection cleared
    assert app.to_top_after_sync and not app.tree.selection() and app.tree.yview()[0] == 0.0
    app.set_theme(3)
    assert str(app.tree.tag_configure("friend", "background")) == app.pal["friend"]
    assert d.get_setting("theme") == "3" and app.pal["accent"] == "#e0a040" and app.pal["bg"] == "#2a1d12"
    app.set_theme(99)                                  # out of range falls back to the default
    assert app.theme == 8
    # ---- optional Twitch section ----
    from brpw import twitch as tw_mod
    by = {r["gamertag"]: r["id"] for r in d.list_accounts()}
    d.link_twitch(by["Old"], "100200300", "example_streamer", "Example_Streamer")
    d.link_twitch(by["New"], "777", "quiet_one", "Quiet_One")
    t_now = int(time.time())
    d.store_twitch_status([(by["Old"], t_now, 1, "Evening Stream", "Supermarket Together", 7, t_now - 60),
                           (by["New"], t_now, 0, None, None, None, None)])
    app.reload()
    twc = {app.tree.set(i, "gamertag"): (app.tree.set(i, "twitch"), app.tree.item(i, "image")) for i in app.tree.get_children()}
    assert twc["Old"][0] == "● 7  Example_Streamer" and twc["Old"][1]        # Twitch says live: text and icon
    assert twc["New"][0] == "Quiet_One" and not twc["New"][1]                 # linked but offline: name only, no icon
    assert twc["New2"][0] == ""                                               # nothing saved: empty
    app.sort_by("twitch")
    assert names()[:2] == ["Old", "New"]                                      # live first, then linked, then the rest
    app.sort_by("gamertag")
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url, *a, **k: opened.append(url))
    app.tree.selection_set(str(by["Old"]))
    app.open_twitch_selected()
    assert opened == ["https://www.twitch.tv/example_streamer"]
    # clicking the Twitch column or the icon opens Twitch; a double-click elsewhere still opens SOTREP
    root.update()
    app.tree.see(str(by["Old"]))
    root.update()
    ev = lambda col: type("E", (), {"x": app.tree.bbox(str(by["Old"]), col)[0] + app.tree.bbox(str(by["Old"]), col)[2] // 2,
                                    "y": app.tree.bbox(str(by["Old"]), col)[1] + 3})()
    opened.clear()
    app._on_tree_click(ev("twitch"))
    app._on_tree_click(ev("#0"))
    app._on_tree_click(ev("gamertag"))
    assert opened == ["https://www.twitch.tv/example_streamer"] * 2
    assert app._twitch_cell(ev("twitch")) and not app._twitch_cell(ev("gamertag"))
    # right-click menu: every per-player action, enabled only when it applies
    app.tree.selection_set(str(by["Old"]))
    items = {label: enabled for label, cmd, enabled in app._row_menu_items() if label}
    assert items["Open Twitch"] and items["Link Twitch channel..."] and "Never link..." in items
    assert set(items) >= {"Open SOTREP", "Check live", "Encounters", "Tag...", "Untag", "Guess Twitch"}
    app.tree.selection_set([str(by["Old"]), str(by["New2"])])
    items = {label: enabled for label, cmd, enabled in app._row_menu_items() if label}
    assert not items["Link Twitch channel..."]                       # needs exactly one player
    opened.clear()
    app.twitch_check_now = lambda *a, **k: None                                # no network in these steps
    app._on_twitch_link(by["New2"], tw_mod.TwitchUser("55", "new_two", "New_Two"), None)
    assert d.twitch_links([by["New2"]])[0]["twitch_id"] == "55"
    app._on_twitch_link(by["Old"], None, "Twitch has no channel named 'x'.")  # a failed lookup changes nothing
    assert app.status_error and d.twitch_links([by["Old"]])[0]["twitch_id"] == "100200300"
    app.twitch_setup_dialog()                                                  # setup window: builds, reports, saves
    setup = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    assert "Not set up yet" in setup.state_var.get()
    app._on_twitch_setup("cid", "sec", "Twitch rejected the client ID or secret.")
    assert "Not saved" in setup.state_var.get() and not app.tcreds.configured()
    app._on_twitch_setup("cid", "sec", None)
    assert app.tcreds.configured() and not setup.winfo_exists()
    app.tclient.http = lambda url, data, headers, form=False: (
        {"access_token": "t", "expires_in": 5000} if url.startswith(tw_mod.TOKEN_URL) else
        {"data": [{"user_id": "100200300", "user_login": "example_streamer", "type": "live", "viewer_count": 7,
                   "game_name": "Supermarket Together", "title": "Evening Stream", "started_at": "2026-10-26T18:59:15Z"}]})
    # "Check live" on selected players: asks Xbox, shows the Twitch icon and a results window
    import time as _t
    app.live.http = lambda url, data, headers, form=False: {"content": {"people": [
        {"xuid": x, "presenceState": "Online", "presenceText": "Sea of Thieves", "isBroadcasting": True,
         "broadcast": {"provider": "Twitch", "viewerCount": 9}} for x in url.rsplit("/", 1)[1].split(",")]}}
    app.tree.selection_set([i for i in app.tree.get_children() if app.tree.set(i, "gamertag") in ("New", "Old")])
    app.check_live_selected()
    end = _t.time() + 5
    while "livecheck" in app.busy and _t.time() < end:
        root.update()
        _t.sleep(0.05)
    assert "livecheck" not in app.busy
    dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    values = dlg.results_tv.item(dlg.results_tv.get_children()[0], "values")
    assert values[0] == "New" and "LIVE on Twitch" in values[2] and "9 viewers" in values[2]
    assert values[3] == "Quiet_One: offline"                         # Twitch column for a linked, offline player
    rows = {dlg.results_tv.item(i, "values")[0]: dlg.results_tv.item(i, "values") for i in dlg.results_tv.get_children()}
    assert rows["Old"][3] == "LIVE: Supermarket Together - Evening Stream (7 viewers)"
    icons = {app.tree.set(i, "gamertag"): app.tree.item(i, "image") for i in app.tree.get_children()}
    assert icons["New"]                                              # the icon appeared for the confirmed player
    dlg.destroy()
    # "Guess Twitch": suggestions are only linked after the user confirms them
    app._on_twitch_guess([{"account_id": by["New2"], "gamertag": "New2", "id": "321", "login": "new2", "name": "New2",
                           "live": True, "game": "Sea of Thieves", "title": "t", "viewers": 4, "sot": True}], None)
    guess = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    assert d.twitch_links([by["New2"]])[0]["twitch_id"] == "55"               # nothing changed before confirming
    guess.guess_tv.selection_set(guess.guess_tv.get_children()[0])
    guess.link_selected()
    assert d.twitch_links([by["New2"]])[0]["twitch_id"] == "321" and not guess.winfo_exists()
    # "Never link": marked players show it, lose their link, and Guess Twitch (no selection = everyone) skips them
    app._ask_tag = lambda *a, **k: "cheater"
    app.tree.selection_set(str(by["New2"]))
    app.never_link_selected()
    assert app.tree.set(str(by["New2"]), "twitch") == "⛔ cheater" and not d.twitch_links([by["New2"]])
    d.merge_players([Player("Unlinked Pirate", "9876543210")], "import", False)
    asked = []
    app.tclient.suggest = lambda players: asked.append(players) or []
    app.tree.selection_set(())
    app.guess_twitch()
    end = _t.time() + 5
    while "twitchguess" in app.busy and _t.time() < end:
        root.update()
        _t.sleep(0.05)
    assert asked, app.status.get()
    guessed = {aid for aid, _ in asked[0]}
    assert by["New2"] not in guessed and by["Old"] not in guessed and by["New"] not in guessed   # never / linked
    assert guessed == {r["id"] for r in d.list_accounts() if not r["twitch_id"] and r["twitch_never"] is None}
    app.tree.selection_set(str(by["New2"]))
    app.never_link_selected()                                         # pressing it again removes the mark
    assert app.tree.set(str(by["New2"]), "twitch") == ""
    # "Live SoT streams": most viewers first (linked or not), linking uses the exact Twitch ID of the stream
    st = lambda i, login, v, lang="en", tags=(): {
        "id": i, "login": login, "name": login.title(), "title": "Sailing", "viewers": v, "started": _t.time() - 3700,
        "language": lang, "tags": list(tags)}
    app.tree.selection_set(())
    streams = [st("900", "big_one", 500, "de", ("Hourglass",)), st("100200300", "example_streamer", 7),
               st("901", "unlinkedpirate", 3), st("902", "stranger", 1, "nl")]
    app._on_sot_streams(streams, None)
    sw = app.sot_win
    sw.geometry("900x320")                                             # small window: buttons must stay visible
    root.update()
    btn = [w for f in sw.winfo_children() for w in f.winfo_children()
           if isinstance(w, ttk.Button) and w.cget("text") == "Find on SOTREP"][0]
    assert btn.winfo_ismapped() and btn.winfo_rooty() + btn.winfo_height() <= sw.winfo_rooty() + sw.winfo_height()
    count = lambda: len(sw.streams_tv.get_children())
    vals = [sw.streams_tv.item(i, "values") for i in sw.streams_tv.get_children()]
    assert [v[1] for v in vals] == ["Big_One", "Example_Streamer", "Unlinkedpirate", "Stranger"]
    assert vals[1][0] == "linked: Old" and vals[2][0] == "same name: Unlinked Pirate"
    assert vals[1][3] == "1h 01m" and vals[0][6] == "Hourglass"
    sw.filter_var.set("strang")
    assert count() == 1
    sw.filter_var.set("hourglass")                                     # search also looks at tags
    assert count() == 1
    sw.filter_var.set("")
    sw.lang_var.set("nl (1)")
    assert count() == 1
    sw.lang_var.set("All languages")
    sw.vmin.set("5")
    sw.vmax.set("abc")                                                 # not a number: ignored
    assert count() == 2
    sw.vmax.set("100")
    assert count() == 1
    sw.vmin.set("")
    sw.vmax.set("")
    sw.mine.set(True)
    assert count() == 2
    sw.mine.set(False)
    sw.lang_var.set("en (2)")
    assert count() == 2
    sw.sort_by("viewers")                                              # first click on viewers: most first
    assert sw.streams_tv.item(sw.streams_tv.get_children()[0], "values")[1] == "Example_Streamer"
    sw.sort_by("viewers")
    assert sw.streams_tv.item(sw.streams_tv.get_children()[0], "values")[1] == "Unlinkedpirate"
    app._on_sot_streams(streams, None)                                 # Refresh keeps the filters and sorting
    sw = app.sot_win
    assert sw.lang_var.get() == "en (2)" and count() == 2
    assert sw.streams_tv.item(sw.streams_tv.get_children()[0], "values")[1] == "Unlinkedpirate"
    sw.mine.set(True)
    sw.reset()                                                         # Reset table: no filters, most viewers first
    assert count() == 4 and sw.lang_var.get() == "All languages" and not sw.mine.get()
    assert sw.streams_tv.item(sw.streams_tv.get_children()[0], "values")[1] == "Big_One"
    app._on_sot_streams(streams, None)
    sw = app.sot_win
    assert count() == 4                                                # the reset is remembered
    sw.streams_tv.selection_set(sw.streams_tv.get_children()[2])
    root.update()
    assert sw.player_var.get() == "Unlinked Pirate"                    # same-name stream pre-fills the player
    sw.link()
    unl = [r for r in d.list_accounts() if r["gamertag"] == "Unlinked Pirate"][0]
    assert unl["twitch_id"] == "901" and sw.player_var.get() == ""   # Player box cleared after linking
    opened.clear()
    sw.streams_tv.selection_set([i for i in sw.streams_tv.get_children()
                                 if sw.streams_tv.item(i, "values")[1] == "Unlinkedpirate"])
    sw.find_on_sotrep()
    assert opened == ["https://www.sotrep.com/search/twitch-unlinkedpirate"]
    # a gamertag that is not in the history: looked up on Xbox first, added (without an encounter) and linked
    monkeypatch.setattr(ui, "find_gamertag", lambda key, tag: (Player("Far Away Pirate", "2535400000000001"), [])
                        if tag.casefold() == "far away pirate" else (None, ["Far Away Pirat3"]))
    stranger = [i for i in sw.streams_tv.get_children() if sw.streams_tv.item(i, "values")[1] == "Stranger"]
    sw.streams_tv.selection_set(stranger)
    sw.player_var.set("far away pirat")
    sw.link()
    end = _t.time() + 5
    while "xboxlookup" in app.busy and _t.time() < end:
        root.update()
        _t.sleep(0.05)
    assert app.status_error and "Similar: Far Away Pirat3" in app.status.get()
    assert sw.player_var.get() == "far away pirat"                     # a failed lookup keeps what was typed
    sw.player_var.set("far away pirate")
    sw.link()
    end = _t.time() + 5
    while "xboxlookup" in app.busy and _t.time() < end:
        root.update()
        _t.sleep(0.05)
    far = [r for r in d.list_accounts() if r["xuid"] == "2535400000000001"][0]
    assert far["gamertag"] == "Far Away Pirate" and far["twitch_id"] == "902" and far["encounters"] == 0
    assert sw.player_var.get() == ""                                  # cleared after linking
    sw.destroy()
    # links made earlier by the (now removed) auto-matcher keep a small mark
    d.unlink_twitch([by["Old"]])
    d.link_twitch(by["Old"], "100200300", "example_streamer", "Example_Streamer", source="auto")
    app.reload()
    assert app.tree.set(str(by["Old"]), "twitch") == "Example_Streamer ⚙"
    # columns can be hidden from the header menu, and that is remembered
    app.toggle_column("xuid")
    assert "xuid" not in app.tree["displaycolumns"] and d.get_setting("hidden_columns") == "xuid"
    app._apply_hidden_columns()
    assert "xuid" not in app.tree["displaycolumns"]
    app.toggle_column("xuid")
    assert "xuid" in app.tree["displaycolumns"]
    # Reset table: Xbox list order, no search, every column back at its normal width
    app.toggle_column("tags")
    app.sort_by("gamertag")
    app.search.set("zzz-nothing")
    app.tree.column("gamertag", width=400)
    app.reset_table()
    assert (app.sort_col, app.sort_desc, app.search.get()) == ("current", False, "")
    assert "tags" in app.tree["displaycolumns"] and d.get_setting("hidden_columns") == ""
    assert app.tree.column("gamertag", "width") < 400 and app.tree.get_children()   # stretch columns fit the window
    real_refresh, refreshed, game = app.refresh_now, [], [False]
    app.refresh_now = lambda manual=True: refreshed.append(manual)
    app.game_check = lambda: game[0]
    assert app.mode_var.get() == ui.REFRESH_MODES[0]   # 'only while SoT is open' by default
    app._tick()
    assert refreshed == [] and "not open" in app.status.get()
    game[0] = True
    app._tick()
    game[0] = False
    app._tick()                                        # one last round right after the game closes
    app._tick()
    assert refreshed == [False, False]
    app.set_refresh_mode("Always")
    app._tick()
    assert refreshed == [False, False, False] and d.get_setting("only_with_game") == "0"
    app.set_refresh_mode("Off")
    assert app.paused and app.after_id is None and d.get_setting("paused") == "1"
    app.set_refresh_mode("Only while SoT is open")
    assert not app.paused and app.after_id and d.get_setting("only_with_game") == "1"
    app.refresh_now = real_refresh
    app.tcreds.clear()
    app.xkey.remove("Main")                            # no key left: Live checks must not start a worker
    app.live_check_now()
    assert not app.live_busy
    app.on_close()
    assert app.closing and app.after_id is None and app.live_after_id is None


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


# ---- friends
FRIENDS = {"content": {"people": [
    {"xuid": "1", "isFollowedByCaller": True, "isFollowingCaller": True},
    {"xuid": "2", "isFollowedByCaller": True, "isFollowingCaller": False},
    {"xuid": "3", "isFollowedByCaller": False, "isFollowingCaller": True},
    {"xuid": "4"},
    {"gamertag": "no xuid"}]}}


def test_parse_friends_relations():
    from brpw.providers import parse_friends
    assert parse_friends(FRIENDS) == {"1": "Friend", "2": "You follow", "3": "Follows you", "4": "In your list"}
    with pytest.raises(ProviderError):
        parse_friends({"content": {}})


def test_friend_column_is_exact_and_per_owner(db):
    db.merge_players([Player("Exact", "1"), Player("Stranger", "9"), Player("Exact", None)], "openxbl", True, now=100)
    db.replace_friends("Main", {"1": "Friend"})
    db.replace_friends("Alt", {"9": "You follow"})
    main = {r["gamertag"] + str(r["xuid"]): r["relation"] for r in db.list_accounts(owner="Main")}
    assert main["Exact1"] == "Friend" and main["Stranger9"] is None
    assert main["ExactNone"] is None                       # same name but no XUID: never matched by name
    alt = {r["gamertag"]: r["relation"] for r in db.list_accounts(owner="Alt")}
    assert alt["Stranger"] == "You follow" and alt["Exact"] is None
    assert db.list_accounts(owner=None)[0]["relation"] is None
    assert [r["gamertag"] for r in db.list_accounts("exa", owner="Main")].count("Exact") == 2   # search still works


def test_friends_refresh_is_throttled_and_never_fails_the_sync(tmp_path):
    from brpw.providers import OpenXblKey, OpenXblProvider
    d = Database(tmp_path / "p.db")
    keys = OpenXblKey(tmp_path / "k.json")
    keys.add("Main", "key")
    calls = []

    def http(url, data, headers, form=False):
        calls.append(url)
        if url.endswith("/friends"):
            return FRIENDS
        return {"content": {"recentPlayers": [{"xuid": "1", "gamertag": "A"}]}}
    svc, prov = SyncService(d), OpenXblProvider(keys, http=http)
    first, second = svc.run(prov), svc.run(prov)
    assert first.ok and second.ok
    assert "friends loaded 4 (1 mutual)" in first.message and "friends loaded" not in second.message
    assert sum(u.endswith("/friends") for u in calls) == 1          # second sync within 30 min reuses it
    assert d.friend_relations("Main")["1"] == "Friend"

    def broken(url, data, headers, form=False):
        if url.endswith("/friends"):
            raise ProviderError("friends down")
        return {"content": {"recentPlayers": [{"xuid": "2", "gamertag": "B"}]}}
    d.set_setting("friends_at:Main", "0")
    out = SyncService(d).run(OpenXblProvider(keys, http=broken))
    assert out.ok and "friends list not updated" in out.message and d.friend_relations("Main")["1"] == "Friend"


def test_parse_friends_tolerates_other_shapes_and_explains_failures():
    from brpw.providers import parse_friends
    assert parse_friends({"content": {"people": []}}) == {}                         # genuinely empty
    nested = {"content": {"data": {"friends": [{"id": "5", "gamertag": "X", "isFollowedByCaller": True,
                                                  "isFollowingCaller": True}]}}}
    assert parse_friends(nested) == {"5": "Friend"}
    with pytest.raises(ProviderError, match="no XUIDs"):
        parse_friends({"content": {"people": [{"gamertag": "NoId"}]}})
    with pytest.raises(ProviderError, match="Structure"):
        parse_friends({"content": {"weird": 1}})


def test_manual_refresh_bypasses_friends_throttle_and_empty_result_retries_soon(tmp_path):
    import time
    from brpw.providers import FRIENDS_REFRESH, OpenXblKey, OpenXblProvider
    d = Database(tmp_path / "p.db")
    keys = OpenXblKey(tmp_path / "k.json")
    keys.add("Main", "key")
    calls = []

    def http(url, data, headers, form=False):
        calls.append(url)
        return {"content": {"people": []}}
    prov = OpenXblProvider(keys, http=http)
    prov.notes = ""
    prov.post_sync(d)
    assert "friends loaded 0" in prov.notes and "answered" in prov.notes
    stamp = int(d.get_setting("friends_at:Main"))
    assert 250 < stamp + FRIENDS_REFRESH - time.time() <= 305                       # an empty list is retried in ~5 min
    n = len(calls)
    prov.post_sync(d)                                   # within 5 min and not forced: no new request
    assert len(calls) == n
    prov.force_friends = True
    prov.post_sync(d)                                   # manual refresh asks again
    assert len(calls) == n + 1


def test_switching_xbox_accounts_does_not_create_fake_encounters(db):
    a = [Player("P1", "1"), Player("P2", "2")]
    b = [Player("P2", "2"), Player("P3", "3")]
    db.merge_players(a, "openxbl", True, now=100, owner="Main")      # first sync of Main: baseline, everyone is new
    db.merge_players(b, "openxbl", True, now=200, owner="Alt")       # first sync of Alt: only P3 is new
    counts = lambda: {r["gamertag"]: r["encounters"] for r in db.list_accounts()}
    assert counts() == {"P1": 1, "P2": 1, "P3": 1}
    for t in range(300, 700, 100):                                   # flip between the accounts and refresh a lot
        db.merge_players(a, "openxbl", True, now=t, owner="Main")
        db.merge_players(b, "openxbl", True, now=t + 50, owner="Alt")
    assert counts() == {"P1": 1, "P2": 1, "P3": 1}                   # still no extra encounters
    db.merge_players([Player("P1", "1")], "openxbl", True, now=800, owner="Main")   # P2 leaves Main's list ...
    db.merge_players(a, "openxbl", True, now=900, owner="Main")                       # ... and really comes back
    assert counts()["P2"] == 2
    # each account sees its own list positions
    assert {r["gamertag"]: r["pos"] for r in db.list_accounts(owner="Alt")} == {"P1": None, "P2": 0, "P3": 1}
    assert {r["gamertag"]: r["pos"] for r in db.list_accounts(owner="Main")}["P1"] == 0


def test_account_details_summary(db):
    db.merge_players([Player("OldName", "1")], "openxbl", True, now=100, owner="Main")
    db.merge_players([Player("NewName", "1")], "openxbl", True, now=200, owner="Main")
    ids = [r["id"] for r in db.list_accounts()]
    db.add_tag(ids, "crew")
    db.replace_friends("Main", {"1": "Friend"})
    d = db.account_details(ids, "Main")[0]
    assert d["gamertag"] == "NewName" and d["old_names"] == "OldName" and d["tags"] == "crew"
    assert d["relation"] == "Friend" and d["times"] == 1 and d["pos"] == 0 and d["state"] is None


# ---- OpenXBL puts the real status in the body
def test_openxbl_body_level_errors(monkeypatch, tmp_path):
    from brpw import providers
    limited = {"content": {"currentRequests": 128, "maxRequests": 60, "periodInSeconds": 300, "limitType": "Rate"}, "code": 429}
    monkeypatch.setattr(providers, "http_json", lambda *a, **k: limited)
    with pytest.raises(ProviderError) as ei:
        providers.openxbl_json("https://x")
    assert ei.value.code == 429 and "rate limiting" in str(ei.value) and "60 requests per 300 s" in str(ei.value)
    for code in (401, 403):
        monkeypatch.setattr(providers, "http_json", lambda *a, code=code, **k: {"content": {}, "code": code})
        with pytest.raises(ProviderError) as ei:
            providers.openxbl_json("https://x")
        assert ei.value.reauth and ei.value.code == code
    ok = {"content": {"people": []}, "code": 200}
    monkeypatch.setattr(providers, "http_json", lambda *a, **k: ok)
    assert providers.openxbl_json("https://x") is ok
    # the whole chain: a rate-limited recent-players refresh says so, instead of "unexpected response"
    monkeypatch.setattr(providers, "http_json", lambda *a, **k: limited)
    keys = providers.OpenXblKey(tmp_path / "k.json")
    keys.add("Main", "key")
    with pytest.raises(ProviderError, match="rate limiting OpenXBL"):
        providers.OpenXblProvider(keys).fetch()
    # and the live check pauses instead of treating it as data
    d = Database(tmp_path / "p.db")
    d.merge_players([Player("A", "1")], "openxbl", True, now=100, owner="Main")
    from brpw.live import LiveChecker
    ck = LiveChecker(d, keys)
    assert "pause" in ck.run_cycle() and ck.budget_left() == 0


def test_game_detection():
    from brpw.game import game_running, running_processes
    assert game_running({"explorer.exe", "sotgame.exe"}) and not game_running({"explorer.exe"})
    assert isinstance(running_processes(), set)
