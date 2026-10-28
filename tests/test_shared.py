import itertools
import urllib.parse

import pytest

from brpw.db import Database, Player
from brpw.providers import ProviderError
from brpw.shared import SharedClient, SharedConfig, new_sender_id

PUB, SECRET = "sb_publishable_test", "sb_secret_test"


class FakeSupabase:
    """Just enough PostgREST for SharedClient, with the rights and trigger rules of docs/shared_list_setup.sql."""

    def __init__(self):
        self.master, self.subs, self.blocked, self.calls = {}, [], {}, []
        self.ids = itertools.count(1)

    def _err(self, code, msg):
        e = ProviderError(f"Shared list: HTTP {code}. {msg}")
        e.code = code
        raise e

    def __call__(self, method, url, headers, body=None):
        u = urllib.parse.urlparse(url)
        table = u.path.rsplit("/", 1)[-1]
        q = urllib.parse.parse_qs(u.query)
        admin = headers.get("apikey") == SECRET
        assert headers.get("apikey") in (PUB, SECRET) and "Authorization" not in headers   # sb_ keys: apikey only
        self.calls.append((method, table, admin))
        if not admin and not (method == "GET" and table == "master_links" or method == "POST" and table == "submissions"):
            self._err(401, "permission denied")
        rows = {"master_links": list(self.master.values()), "submissions": self.subs,
                "blocked_submitters": list(self.blocked.values())}[table]

        def match(r):
            for k, v in q.items():
                if k in ("select", "order", "limit", "offset", "on_conflict"):
                    continue
                op, _, val = v[0].partition(".")
                if op == "eq" and str(r.get(k)) != val:
                    return False
                if op == "in" and str(r.get(k)) not in val.strip("()").split(","):
                    return False
            return True

        if method == "GET":
            out = [r for r in rows if match(r)]
            off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["1000"])[0])
            return [dict(r) for r in out[off:off + lim]]
        if method == "POST" and table == "submissions":
            for r in body:
                if not admin and set(r) - {"submitter", "submitter_name", "xuid", "gamertag", "twitch_id",
                                           "twitch_login", "twitch_name"}:
                    self._err(401, "permission denied for column")
                if r["submitter"] in self.blocked:
                    self._err(400, "This copy of the app is blocked from sending suggestions.")
                if any(s["submitter"] == r["submitter"] and s["xuid"] == r["xuid"] and s["twitch_id"] == r["twitch_id"]
                       for s in self.subs) or (self.master.get(r["xuid"], {}).get("twitch_id") == r["twitch_id"]):
                    continue
                self.subs.append({**r, "id": next(self.ids), "status": "pending", "created_at": "now"})
            return None
        if method == "POST" and table == "master_links":
            for r in body:
                self.master[r["xuid"]] = dict(r)
            return None
        if method == "POST" and table == "blocked_submitters":
            for r in body:
                self.blocked[r["submitter"]] = dict(r)
            return None
        if method == "PATCH":
            for r in rows:
                if match(r):
                    r.update(body)
            return None
        if method == "DELETE":
            if table == "master_links":
                for r in [r for r in rows if match(r)]:
                    del self.master[r["xuid"]]
            else:
                for r in [r for r in rows if match(r)]:
                    del self.blocked[r["submitter"]]
            return None
        raise AssertionError((method, table))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "s.db")
    d.merge_players([Player("Salty Pirate", "2535400000000001"), Player("Other Guy", "2535400000000002"),
                     Player("Cheaty One", "2535400000000003"), Player("Mine", "2535400000000004")],
                    "openxbl", True, now=100)
    yield d
    d.close()


def ids(db):
    return {r["gamertag"]: r["id"] for r in db.list_accounts()}


def entry(xuid, gt, tid, login):
    return {"xuid": xuid, "gamertag": gt, "twitch_id": tid, "twitch_login": login, "twitch_name": login.title()}


def test_master_list_fills_history_but_never_overrides_the_user(db):
    i = ids(db)
    db.link_twitch(i["Mine"], "40", "my_pick", "My_Pick")
    db.set_twitch_never([i["Cheaty One"]], "cheater")
    db.replace_shared([entry("2535400000000001", "Salty Pirate", "10", "salty"),
                       entry("2535400000000003", "Cheaty One", "30", "cheaty"),
                       entry("2535400000000004", "Mine", "41", "someone_else"),
                       entry("2535400000099999", "Never Met", "50", "never_met")])
    links = {l["account_id"]: l for l in db.twitch_links()}
    assert links[i["Salty Pirate"]]["twitch_id"] == "10" and links[i["Salty Pirate"]]["source"] == "shared"
    assert i["Cheaty One"] not in links                                   # never link wins
    assert links[i["Mine"]]["twitch_id"] == "40"                           # own link wins
    assert len(db.list_accounts()) == 4                                    # master entries don't add players
    db.unlink_twitch([i["Salty Pirate"]])                                  # removed by the user: stays removed
    db.apply_shared()
    assert not db.twitch_links([i["Salty Pirate"]])
    db.merge_players([Player("Never Met", "2535400000099999")], "openxbl", True, now=200)
    assert db.apply_shared() == 1                                          # met later: gets the channel then
    assert db.shared_by_twitch() == {"41": "Mine", "50": "Never Met"}      # not removed (Salty) or never (Cheaty)
    db.replace_shared([])                                                  # gone from the master list
    assert [l["account_id"] for l in db.twitch_links()] == [i["Mine"]]


def test_links_to_share_only_own_new_links_once(db):
    i = ids(db)
    db.link_twitch(i["Mine"], "40", "my_pick", "My_Pick")
    db.link_twitch(i["Other Guy"], "20", "other", "Other")
    db.replace_shared([entry("2535400000000002", "Other Guy", "20", "other"),
                       entry("2535400000000001", "Salty Pirate", "10", "salty")])
    rows = db.links_to_share()
    assert [r["gamertag"] for r in rows] == ["Mine"]                       # shared and already-known are skipped
    db.mark_sent(rows)
    assert db.links_to_share() == [] and len(db.links_to_share(include_sent=True)) == 1


def test_master_list_follows_renamed_players_and_channels(db):
    i = ids(db)
    db.link_twitch(i["Mine"], "40", "my_pick", "My_Pick")
    db.replace_shared([entry("2535400000000004", "Mine", "40", "my_pick"),
                       entry("2535400000000001", "Salty Pirate", "10", "salty")])
    assert db.master_corrections() == []
    db.merge_players([Player("Mine Renamed", "2535400000000004"),                # Xbox rename: same XUID
                      Player("Salty Pirate", "2535400000000001")], "openxbl", True, now=200)
    assert {l["account_id"]: l["twitch_id"] for l in db.twitch_links()} == {i["Mine"]: "40", i["Salty Pirate"]: "10"}
    db.update_twitch_names({"10": ("salty_new", "Salty_New")})                   # Twitch rename: same channel ID
    fixes = {r["xuid"]: (r["gamertag"], r["twitch_login"]) for r in db.master_corrections()}
    assert fixes == {"2535400000000004": ("Mine Renamed", "my_pick"), "2535400000000001": ("Salty Pirate", "salty_new")}
    assert db.links_to_share(include_sent=True) == []                            # renames are not new links
    db.replace_shared([entry("2535400000000004", "Mine Renamed", "40", "my_pick"),   # published, downloaded again
                       entry("2535400000000001", "Salty Pirate", "10", "salty_new")])
    assert db.master_corrections() == []
    assert [l["login"] for l in db.twitch_links([i["Salty Pirate"]])] == ["salty_new"]


def test_public_key_can_only_read_master_and_suggest():
    fake = FakeSupabase()
    pub, owner = SharedClient("https://x.supabase.co", PUB, http=fake), SharedClient("https://x.supabase.co/", SECRET, http=fake)
    owner.publish([entry("2535400000000001", "Salty Pirate", "10", "salty")])
    assert [m["twitch_id"] for m in pub.master()] == ["10"]
    for bad in (pub.pending, lambda: pub.publish([entry("2535400000000009", "X", "9", "x")]),
                lambda: pub.remove(["1"]), lambda: pub.block("a")):
        with pytest.raises(ProviderError):
            bad()
    sid = new_sender_id()
    assert len(sid) == 32
    pub.submit([entry("2535400000000002", "Other Guy", "20", "other"),
                entry("2535400000000001", "Salty Pirate", "10", "salty")], sid, "Bob")
    pub.submit([entry("2535400000000002", "Other Guy", "20", "other")], sid, "Bob")
    pending = owner.pending()
    assert [(p["gamertag"], p["submitter_name"]) for p in pending] == [("Other Guy", "Bob")]   # known/dupes dropped
    owner.approve(pending)
    assert set(fake.master) == {"2535400000000001", "2535400000000002"}
    assert owner.pending() == [] and owner.sender_stats()[sid]["approved"] == 1


def test_blocking_a_sender_rejects_waiting_and_refuses_new():
    fake = FakeSupabase()
    pub, owner = SharedClient("https://x", PUB, http=fake), SharedClient("https://x", SECRET, http=fake)
    bad = new_sender_id()
    pub.submit([entry("2535400000000002", "Other Guy", "66", "troll")], bad)
    owner.block(bad, "spam")
    assert owner.pending() == [] and owner.sender_stats()[bad]["rejected"] == 1
    with pytest.raises(ProviderError):
        pub.submit([entry("2535400000000003", "Cheaty One", "67", "troll2")], bad)
    owner.unblock(bad)
    pub.submit([entry("2535400000000003", "Cheaty One", "67", "troll2")], bad)
    assert len(owner.pending()) == 1


def test_config_files(tmp_path):
    bundled = tmp_path / "bundled.json"
    cfg = SharedConfig(tmp_path / "data", bundled)
    (tmp_path / "data").mkdir()
    assert cfg.service() is None and cfg.admin_key() is None
    bundled.write_text('{"url": "https://b.supabase.co/", "key": "sb_publishable_b"}')
    assert cfg.service() == {"url": "https://b.supabase.co", "key": "sb_publishable_b"}
    assert cfg.own_service() is None                                        # the bundled one is never "own"
    cfg.save_service("https://own.supabase.co", "sb_publishable_own")         # data dir wins over the bundled one
    assert cfg.service()["url"] == "https://own.supabase.co" == cfg.own_service()["url"]
    cfg.save_admin_key("sb_secret_x")
    assert cfg.admin_key() == "sb_secret_x"
    cfg.save_admin_key(None)
    assert cfg.admin_key() is None


def test_shared_ui_send_review_approve(tmp_path, monkeypatch):
    import time as _t
    import tkinter as tk
    from brpw import ui
    for attempt in range(5):        # a second Tk in one process sometimes fails to find init.tcl on Windows
        try:
            root = tk.Tk()
            break
        except tk.TclError as e:
            if attempt == 4:
                pytest.skip(f"no display available: {e}")
            _t.sleep(0.2)
    root.withdraw()
    monkeypatch.setenv("BRPW_DATA_DIR", str(tmp_path))
    d = Database(tmp_path / "u.db")
    d.set_setting("auto_update_check", "0")
    d.merge_players([Player("Salty Pirate", "2535400000000001"), Player("Mine", "2535400000000004")],
                    "openxbl", True, now=100)
    i = ids(d)
    d.link_twitch(i["Mine"], "40", "my_pick", "My_Pick")
    app = ui.App(root, d)
    fake = FakeSupabase()
    app.shared_http = fake
    app.shared_cfg.bundled = tmp_path / "none.json"

    def wait(name):
        end = _t.time() + 5
        while name in app.busy and _t.time() < end:
            root.update()
            _t.sleep(0.02)
        root.update()

    win = app.shared_dialog()
    assert "no shared list" in win.state_var.get() and str(win.send_btn["state"]) == "disabled"
    win.destroy()
    app.shared_cfg.bundled.write_text('{"url": "https://x.supabase.co", "key": "%s"}' % PUB)   # a normal user's copy
    fake.master["2535400000000001"] = entry("2535400000000001", "Salty Pirate", "10", "salty")
    fake.master["2535400000099999"] = entry("2535400000099999", "Never Met", "50", "never_met")
    app.shared_sync(quiet=False)
    wait("shared")
    assert app.tree.set(str(i["Salty Pirate"]), "twitch") == "Salty" + ui.SHARED_MARK
    # Live SoT streams: master-list players count as linked, also ones this PC has never met
    app._on_sot_streams([{"id": "50", "login": "never_met", "name": "Never_Met", "title": "t", "viewers": 3,
                          "started": None, "language": "en", "tags": []}], None)
    tv = app.sot_win.streams_tv
    assert tv.item(tv.get_children()[0], "values")[0] == "linked: Never Met" + ui.SHARED_MARK
    tv.selection_set(tv.get_children()[0])
    root.update()
    assert app.sot_win.player_var.get() == "Never Met"
    app.sot_win.destroy()
    win = app.shared_dialog()
    assert "Send my links (1)" == win.send_btn["text"] and not win.review_btn.winfo_ismapped()
    assert not win.setup_btn.winfo_manager()                                 # Owner setup hidden from users
    sw = app.shared_setup_dialog()                                           # (Ctrl+Shift+O) built-in address not shown
    assert sw.url_var.get() == "" and sw.key_var.get() == ""
    sw.save()                                                                # empty = test the built-in list, save nothing
    wait("sharesetup")
    assert sw.state_var.get().startswith("Saved") and app.shared_cfg.own_service() is None
    wait("shared")
    sw.destroy()
    assert all(lbl != "Remove from master list" for lbl, *_ in app._row_menu_items())
    win.name_var.set("Bob")
    app.send_links(confirm=False)
    wait("sharesend")
    assert [(s["gamertag"], s["submitter_name"]) for s in fake.subs] == [("Mine", "Bob")]
    assert win.send_btn["text"] == "Send my links (0)"
    win.destroy()
    app.shared_cfg.save_admin_key(SECRET)                                    # the owner's copy
    win = app.shared_dialog()
    assert win.setup_btn.winfo_manager() == "pack" and win.review_btn.winfo_manager() == "pack"
    win.destroy()
    app.tree.selection_set(str(i["Salty Pirate"]))
    assert ("Remove from master list", app.remove_from_master, True) in app._row_menu_items()
    rw = app.review_dialog()
    wait("review")
    rows = [rw.review_tv.item(x, "values") for x in rw.review_tv.get_children()]
    assert [r[0] for r in rows] == ["Mine"] and r"same as your link" in rows[0][5] and rows[0][3] == "Bob"
    rw.review_tv.selection_set(rw.review_tv.get_children())
    rw.approve()
    wait("review")
    wait("shared")
    wait("review")
    assert "2535400000000004" in fake.master and fake.subs[0]["status"] == "approved"
    assert rw.review_tv.get_children() == ()
    rw.destroy()
    app.on_close()
