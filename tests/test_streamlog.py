import time

import pytest

from brpw.providers import Player, ProviderError
from brpw.shared import SharedConfig
from brpw.streamlog import StreamLog
from test_shared import PUB, SECRET, FakeSupabase, entry


def st(i, login, viewers, title="Sailing", lang="en"):
    return {"id": i, "login": login, "name": login.title(), "viewers": viewers, "title": title, "language": lang,
            "started": None, "tags": []}


@pytest.fixture
def log(tmp_path):
    lg = StreamLog(tmp_path / "stream_log.db")
    yield lg
    lg.close()


def test_rounds_remember_every_channel(log):
    assert log.record([st("1", "salty", 10), st("2", "other", 5)], now=1000) == 2
    assert log.record([st("1", "salty", 30, "New title")], now=1300) == 0           # known channel: updated
    assert log.record([st("1", "salty", 20), st("3", "late", 1)], now=1600) == 1
    rows = {r["twitch_id"]: r for r in log.channels()}
    assert (rows["1"]["first_seen"], rows["1"]["last_seen"], rows["1"]["seen"], rows["1"]["peak"]) == (1000, 1600, 3, 30)
    assert rows["2"]["last_seen"] == 1000 and rows["2"]["status"] == "new"
    assert [r["twitch_id"] for r in log.channels()] == ["1", "3", "2"]              # last live first
    log.record_error("Twitch rate limit", now=1900)
    assert log.last_round()["error"] and log.first_round() == 1000
    log.set_status(["2"], "checked", now=2000)
    log.set_status(["3"], "linked", "Late Pirate", "2535400000000009", now=2000)
    rows = {r["twitch_id"]: r for r in log.channels()}
    assert rows["2"]["status"] == "checked" and rows["3"]["gamertag"] == "Late Pirate" and rows["3"]["checked_at"] == 2000
    c = log.counts({"1": "Salty Pirate"})
    assert (c["total"], c["todo"], c["master"], c["checked"], c["linked"]) == (3, 0, 1, 1, 1)
    log.set_status(["2"], "new")
    assert log.counts({})["todo"] == 2
    with pytest.raises(ValueError):
        log.set_status(["2"], "whatever")


class FakeTwitch:
    def __init__(self, streams):
        self.streams, self.creds = streams, self

    def load(self):
        return ("cid", "secret")

    def game_streams(self):
        if isinstance(self.streams, Exception):
            raise self.streams
        return self.streams


class Keys:
    def get(self):
        return "openxbl-key"


def test_stream_log_window(tmp_path, monkeypatch):
    import tkinter as tk
    from brpw import streamlog_ui as ui
    for attempt in range(5):        # a second Tk in one process sometimes fails to find init.tcl on Windows
        try:
            root = tk.Tk()
            break
        except tk.TclError as e:
            if attempt == 4:
                pytest.skip(f"no display available: {e}")
            time.sleep(0.2)
    root.withdraw()
    monkeypatch.setattr(ui, "keep_awake", lambda on: True)
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", opened.append)
    said = []                                                                  # no real message boxes in tests
    for kind in ("showinfo", "showerror"):
        monkeypatch.setattr(ui.messagebox, kind, lambda title, text, **kw: said.append(text))
    fake = FakeSupabase()
    fake.master["2535400000000001"] = entry("2535400000000001", "Salty Pirate", "1", "salty")
    cfg = SharedConfig(tmp_path, tmp_path / "none.json")
    cfg.save_service("https://x.supabase.co", PUB)
    cfg.save_admin_key(SECRET)
    lookups = []

    def find(key, gamertag):
        lookups.append((key, gamertag))
        return (Player("Other Guy", "2535400000000002"), []) if gamertag == "other guy" else (None, ["Close One"])

    lg = StreamLog(tmp_path / "stream_log.db")
    tw = FakeTwitch([st("1", "salty", 40), st("2", "other", 5), st("3", "third", 2)])
    app = ui.StreamLogApp(root, lg, tw, cfg, Keys(), shared_http=fake, find=find)

    def settle():
        end = time.time() + 5
        while app.busy and time.time() < end:
            root.update()
            time.sleep(0.02)
        root.update()
    settle()
    shown = lambda: [app.tv.set(i, "name") for i in app.tv.get_children()]
    assert shown() == ["Other", "Third"]                                       # to do: Salty is in the master list
    app.view.set("All")
    assert app.tv.set("1", "status") == "◆ Salty Pirate" and "3 channels" in app.counts.get()
    app.view.set("To do (new, not in master list)")
    app.tv.selection_set("3")
    app.mark("checked")                                                        # already checked: off the to-do list
    assert shown() == ["Other"]
    app.tv.selection_set("2")
    app.open_sotrep()
    assert opened and "sotrep" in opened[-1] and "other" in opened[-1]
    app.add_to_master(gamertag="nobody", confirm=False)                        # no exact Xbox match: nothing added
    settle()
    assert "2535400000000002" not in fake.master and "Close One" in said[-1]
    app.add_to_master(gamertag="other guy", confirm=False)
    settle()
    assert lookups[-1] == ("openxbl-key", "other guy")
    assert fake.master["2535400000000002"]["twitch_id"] == "2" and fake.master["2535400000000002"]["twitch_login"] == "other"
    assert shown() == [] and {r["twitch_id"]: r["status"] for r in lg.channels()}["2"] == "linked"
    app.view.set("All")
    assert app.tv.set("2", "status") == "◆ Other Guy"
    tw.streams = ProviderError("Twitch rate limit reached")                   # a failed round is shown, data kept
    app.round_now()
    settle()
    assert "failed" in app.state.get() and len(lg.channels()) == 3
    app.toggle()
    assert "PAUSED" in app.state.get()
    app.on_close()
    lg.close()
