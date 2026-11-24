import io
import json

import pytest

from brpw import cli, discord, report
from brpw.db import Database, Player
from brpw.providers import ProviderError

HOOK = "https://discord.com/api/webhooks/123456789/abcDEF_-x"


def history(tmp_path):
    d = Database(tmp_path / "players.db")
    d.merge_players([Player("Salty Pirate", "2535400000000001"), Player("Brine*Witch", "2535400000000002"),
                     Player("Crewmate", "2535400000000003")], "xbox-live", True, now=1_800_000_000, owner="Main")
    ids = {a["gamertag"]: a["id"] for a in d.list_accounts()}
    d.replace_shared([{"xuid": "2535400000000001", "gamertag": "Salty Pirate", "twitch_id": "9",
                       "twitch_login": "salty_tv"}])
    d.add_tag([ids["Crewmate"]], "crew")
    d.save_minutes(["2535400000000001"], {"2535400000000001": 6000}, now=1_800_000_000)
    return d


def test_report_lists_xbox_order_with_twitch_sotrep_and_without_crew(tmp_path):
    d = history(tmp_path)
    rows = report.recent(d, "Main", now=1_800_000_030)
    assert [r["gamertag"] for r in rows] == ["Salty Pirate", "Brine*Witch"]           # crew hidden (the default)
    assert rows[0]["pos"] == 1 and rows[0]["twitch"] == "salty_tv" and rows[0]["hours"] == "100 h"
    assert rows[0]["sotrep"] == "https://www.sotrep.com/search/xbox-salty-pirate"
    assert rows[0]["seen_text"] == "now" and rows[0]["twitch_shared"]
    assert len(report.recent(d, "Main", hide_crew=False)) == 3 and len(report.recent(d, "Main", limit=1)) == 1
    d.close()


def test_discord_webhook_address_is_checked():
    assert discord.check_url(" " + HOOK + "/ ") == HOOK
    assert discord.check_url("https://discordapp.com/api/v10/webhooks/1/tok") == "https://discord.com/api/webhooks/1/tok"
    for bad in ("http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/x",
                "https://discord.com/api/webhooks/abc/x", ""):
        with pytest.raises(ProviderError):
            discord.check_url(bad)


def test_discord_posts_once_then_edits_and_skips_unchanged(tmp_path):
    d = history(tmp_path)
    cfg = discord.DiscordConfig(tmp_path / "discord.json")
    cfg.update(url=HOOK)
    calls = []

    def call(method, url, body):
        calls.append((method, url))
        return {"id": "555"}
    players = report.recent(d, "Main")
    assert discord.post_list(cfg, "Main", players, call=call) == "sent"
    assert calls[-1] == ("POST", HOOK + "?wait=true") and cfg["message_id"] == "555"
    body = discord.payload("Main", players)
    text = body["embeds"][0]["description"]
    assert "[Salty Pirate](https://www.sotrep.com/search/xbox-salty-pirate)" in text and r"twitch.tv/salty\_tv" in text
    assert r"Brine\*Witch" in text and body["allowed_mentions"] == {"parse": []}         # no formatting, no pings
    assert discord.post_list(cfg, "Main", players, call=call) == "unchanged" and len(calls) == 1
    assert discord.post_list(cfg, "Main", players, force=True, call=call) == "edited"
    assert calls[-1] == ("PATCH", HOOK + "/messages/555")

    def gone(method, url, body):                                      # the message was deleted in Discord
        calls.append((method, url))
        if method == "PATCH":
            e = ProviderError("gone")
            e.code = 404
            raise e
        return {"id": "777"}
    assert discord.post_list(cfg, "Main", players, force=True, call=gone) == "sent" and cfg["message_id"] == "777"
    saved = json.loads((tmp_path / "discord.json").read_text())
    assert saved["url"] == HOOK and saved["message_id"] == "777"
    d.close()


def test_terminal_version_lists_accounts_and_discord(tmp_path, monkeypatch):
    history(tmp_path).close()
    (tmp_path / "openxbl_keys.json").write_text(json.dumps({"active": "Main", "accounts": [
        {"name": "Main", "key": "k1"}, {"name": "Alt", "key": "k2"}]}))
    monkeypatch.setenv("BRPW_DATA_DIR", str(tmp_path))
    buf = io.StringIO()
    c = cli.Cli(data=tmp_path, out=cli.Out(buf, color=False))
    assert c.accounts() == 0 and "* Main" in buf.getvalue() and "  Alt" in buf.getvalue()
    assert c.list(sync=False) == 0
    out = buf.getvalue()
    assert "Salty Pirate" in out and "salty_tv" in out and "https://www.sotrep.com/search/xbox-salty-pirate" in out
    assert "Crewmate" not in out
    assert c.use("Alt") == 0 and c.xkey.active() == "Alt" and c.use("Nobody") == 1
    assert c.discord_cmd("set", "not a hook") == 1 and c.discord_cmd("set", HOOK) == 0
    posted = []
    monkeypatch.setattr(discord, "_call", lambda m, u, b: posted.append(m) or {"id": "9"})
    c.xkey.set_active("Main")
    assert c.discord_cmd("post", None) == 0 and posted == ["POST"]
    assert c.remove("Alt") == 0 and c.xkey.names() == ["Main"]
    buf.truncate(0)
    assert c.list(sync=False, as_json=True) == 0 and json.loads(buf.getvalue().strip("\x00"))[0]["gamertag"] == "Salty Pirate"
    c.db.close()


def test_terminal_flags_work_before_or_after_the_command():
    p = cli.parser()
    assert p.parse_args(["--no-color", "list"]).no_color and p.parse_args(["list", "--no-color", "-n", "5"]).n == 5
    assert p.parse_args([]).cmd is None


def test_discord_tab_in_the_app(tmp_path, monkeypatch):
    from brpw import ui
    from conftest import new_root
    monkeypatch.setenv("BRPW_DATA_DIR", str(tmp_path))
    root = new_root()
    root.withdraw()
    d = history(tmp_path)
    d.set_setting("auto_update_check", "0")
    app = ui.App(root, d)
    try:
        win = app.discord_dialog()
        assert app.page_now == "discord"
        win.url.set("https://example.com/not-a-hook")
        win.save()
        assert "isn't a Discord webhook" in win.said.get() and not app.discord_cfg["url"]
        win.url.set(HOOK)
        win.save()
        assert app.discord_cfg["url"] == HOOK and "saved" in win.said.get()
        sent = []
        app.fetch_discord = lambda cfg, account, players, force: sent.append((account, len(players), force)) or "sent"
        app._discord_round()                                       # automatic posting is off: nothing
        assert sent == []
        app._discord_round(force=True, said=win.said)               # Post now
        import time as _t
        end = _t.time() + 5
        while getattr(app, "_discord_busy", False) and _t.time() < end:
            root.update()
            _t.sleep(0.02)
        root.update()
        assert sent and sent[0][2] is True and "Posted the list" in win.said.get()
        win.remove()
        assert not app.discord_cfg["url"] and not app.discord_cfg["auto"]
    finally:
        app.on_close()


def answers(*replies):
    it = iter(replies)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return ask


def test_first_use_setup_and_the_menu(tmp_path, monkeypatch):
    history(tmp_path).close()
    buf = io.StringIO()
    c = cli.Cli(data=tmp_path, out=cli.Out(buf, color=False))
    monkeypatch.setattr(cli, "lookup_gamertag", lambda key: "Main")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(c, "sync", lambda: None)                        # no Xbox in tests
    menu = cli.Menu(c, ask=answers("2", "my-openxbl-key", HOOK,          # setup: OpenXBL key, then Discord
                                   "h", "5", "Salty Pirate", "n", "4", "", "q"))
    assert menu.run() == 0
    out = buf.getvalue()
    assert "Welcome! Let's add your Xbox account." in out and c.xkey.names() == ["Main"]
    assert c.discord["url"] == HOOK and "Setup done" in out and "Salty Pirate" in out
    assert "brpw-cli add --key KEY" in out                              # h: the help
    assert "https://www.sotrep.com/search/xbox-salty-pirate" in out     # 5: a SOTREP link
    assert "* Main" in out                                             # 4: the accounts
    c.db.close()


def test_menu_survives_the_end_of_input(tmp_path):
    history(tmp_path).close()
    (tmp_path / "openxbl_keys.json").write_text(json.dumps({"active": "Main", "accounts": [{"name": "Main", "key": "k"}]}))
    c = cli.Cli(data=tmp_path, out=cli.Out(io.StringIO(), color=False))
    c.sync = lambda: None
    assert cli.Menu(c, ask=answers()).run() == 0                       # nothing typed (closed input): quits
    c.db.close()


def test_double_click_detection_only_on_windows(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "posix")
    assert cli.started_by_double_click() is False
