"""Better Recently Played With in a terminal (Windows cmd / PowerShell / Terminal, Linux, macOS).

    brpw-cli                       refresh from Xbox and show your recently-played list
    brpw-cli list --all -n 50      the older history too
    brpw-cli watch -i 60           refresh and show again every 60 s (Ctrl+C stops)
    brpw-cli accounts | add | remove NAME | use NAME
    brpw-cli sotrep GAMERTAG [--open]
    brpw-cli discord set URL | post | auto on/off | off

It uses the same data folder as the app (accounts, history, tags, Twitch links), so both can be used side by side.
Twitch channels come from your own links and the shared master list (downloaded every few hours)."""
import argparse
import json
import os
import shutil
import sys
import time
import webbrowser
from typing import List, Optional

from . import __version__, direct, discord, report, social, sotrep
from .db import Database
from .msauth import MARK, MsAccounts
from .paths import data_dir
from .providers import OpenXblKey, OpenXblProvider, ProviderError, lookup_gamertag
from .shared import SharedClient, SharedConfig
from .sync import SyncService
from .twitch import TwitchChecker, TwitchClient, TwitchCreds

SHARED_EVERY = 6 * 3600          # the master list is downloaded at most this often


class Out:
    """Colours and clickable links only on a real terminal (and never with NO_COLOR)."""

    def __init__(self, stream=None, color: Optional[bool] = None, links: bool = False):
        self.s = stream or sys.stdout
        tty = hasattr(self.s, "isatty") and self.s.isatty()
        self.color = (tty and not os.environ.get("NO_COLOR")) if color is None else color
        self.links = links
        if self.color and os.name == "nt":
            os.system("")                                 # turns on colour codes in the Windows console

    def c(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else str(text)

    def link(self, text, url):
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\" if self.links and url else str(text)

    def __call__(self, text=""):
        try:
            print(text, file=self.s)
        except UnicodeEncodeError:                        # an old console code page: replace what it can't show
            print(str(text).encode(self.s.encoding or "ascii", "replace").decode(self.s.encoding or "ascii"),
                  file=self.s)


class Cli:
    def __init__(self, data=None, out: Optional[Out] = None):
        d = data or data_dir()
        self.dir = d
        self.db = Database(d / "players.db")
        self.xkey = OpenXblKey(d / "openxbl_keys.json")
        self.msa = MsAccounts(d / "ms_accounts.json")
        direct.configure(self.msa)
        self.shared = SharedConfig(d)
        self.tcreds = TwitchCreds(d / "twitch.json")
        self.discord = discord.DiscordConfig(d / "discord.json")
        self.out = out or Out()

    # ---- accounts
    def accounts(self) -> int:
        names, active = self.xkey.names(), self.xkey.active()
        if not names:
            self.out("No accounts yet. Add one:  brpw-cli add   (Microsoft sign-in)  or  brpw-cli add --key KEY")
            return 1
        for n in names:
            kind = "Microsoft sign-in" if direct.is_ms_key(self.xkey.get(n)) else "OpenXBL key"
            self.out(("* " if n == active else "  ") + self.out.c(n, "1") + self.out.c(f"   ({kind})", "2"))
        return 0

    def add(self, key: Optional[str] = None) -> int:
        if key:
            name = lookup_gamertag(key) or "Account"
            name = self.xkey.add(name, key)
        else:
            def show(url):
                self.out("Sign in on Microsoft's page (the app never sees your password). If no browser opens, "
                         "open this address on this computer:")
                self.out(url)
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            self.out("Waiting for the sign-in (5 minutes at most)...")
            xuid, gamertag = self.msa.sign_in(open_url=show)
            name = self.xkey.add(gamertag or "Account", MARK + xuid)
        self.out(f"Added {self.out.c(name, '1')} (now the active account).")
        return 0

    def remove(self, name: str) -> int:
        if name not in self.xkey.names():
            self.out(f"No account called '{name}'. Accounts: {', '.join(self.xkey.names()) or 'none'}")
            return 1
        key = self.xkey.get(name)
        if direct.is_ms_key(key):
            self.msa.remove(key[len(MARK):])
        self.xkey.remove(name)
        self.out(f"Removed {name}. Your player history is kept.")
        return 0

    def use(self, name: str) -> int:
        if name not in self.xkey.names():
            self.out(f"No account called '{name}'. Accounts: {', '.join(self.xkey.names()) or 'none'}")
            return 1
        self.xkey.set_active(name)
        self.out(f"Active account: {name}")
        return 0

    # ---- refreshing
    def sync(self) -> Optional[str]:
        """Recently played from Xbox, hours played for the new ones (Microsoft sign-in) and the Twitch links. Returns
        an error text, or None."""
        if not self.xkey.get():
            return "No account yet. Add one:  brpw-cli add"
        out = SyncService(self.db).run(OpenXblProvider(self.xkey))
        if out is None or not out.ok:
            return out.message if out else "A refresh is already running."
        key = self.xkey.get()
        if direct.is_ms_key(key):                         # hours in Sea of Thieves for players not asked today
            try:
                xuids = [p["xuid"] for p in report.recent(self.db, self.xkey.active(), limit=None) if p["xuid"]]
                _, want = self.db.profiles_due(xuids, stats_age=86400)
                if want:
                    want = want[:150]
                    self.db.save_minutes(want, social.sot_minutes(key, want))
            except (ProviderError, ValueError):
                pass
        self.shared_sync()
        self.db.apply_shared()
        if self.tcreds.configured():                      # live status of the channels (100 per request)
            try:
                TwitchChecker(self.db, TwitchClient(self.tcreds)).check_accounts()
            except ProviderError:
                pass
        return None

    def shared_sync(self, force: bool = False) -> None:
        svc = self.shared.service()
        if not svc:
            return
        if not force and time.time() - int(self.db.get_setting("shared_synced", "0") or 0) < SHARED_EVERY:
            return
        try:
            self.db.replace_shared(SharedClient(svc["url"], svc["key"]).master())
            self.db.set_setting("shared_synced", str(int(time.time())))
        except ProviderError:
            pass

    # ---- the list
    def show(self, players: List[dict], title: str, width: Optional[int] = None) -> None:
        """The list as a table that fits the window: narrow columns, and when a line would still be too wide the
        SOTREP link goes on its own line underneath (whole, so it stays clickable)."""
        o = self.out
        width = width or shutil.get_terminal_size((120, 30)).columns
        o(o.c(title, "1;36"))
        if not players:
            o("  Nobody in the list yet.")
            return
        gt_w = min(18, max(8, max(len(p["gamertag"]) for p in players)))
        tw_w = min(16, max([6] + [len(p["twitch"]) + (6 if p["twitch_live"] else 0) for p in players]))
        seen_w = max(4, max(len(p["seen_text"]) for p in players))
        fixed = 3 + 2 + gt_w + 2 + seen_w + 1 + 8 + 2 + tw_w + 2
        longest = max(len(p["sotrep"]) for p in players)
        beside = o.links or fixed + longest <= width - 1          # the link fits on the same line
        head = f"{'#':>3}  {'Gamertag':<{gt_w}}  {'Seen':<{seen_w}} {'Hours':>8}  {'Twitch':<{tw_w}}"
        o(o.c(head + ("  SOTREP" if beside and not o.links else ""), "2"))
        for p in players:
            gt = p["gamertag"] if len(p["gamertag"]) <= gt_w else p["gamertag"][:gt_w - 1] + "…"
            name = o.link(gt, p["sotrep"]) + " " * (gt_w - len(gt))
            name = o.c(name, "1;32") if p["friend"] else name
            tw = p["twitch"] + (" ●LIVE" if p["twitch_live"] else "")
            tw = tw if len(tw) <= tw_w else tw[:tw_w - 1] + "…"
            tw_cell = o.link(tw, p["twitch_url"]) + " " * (tw_w - len(tw))
            tw_cell = o.c(tw_cell, "1;35") if p["twitch_live"] else tw_cell
            pos = str(p["pos"]) if p["pos"] else "-"
            line = f"{pos:>3}  {name}  {p['seen_text']:<{seen_w}} {p['hours']:>8}  {tw_cell}"
            if o.links:
                o(line)
            elif beside:
                o(line + "  " + p["sotrep"])
            else:                                               # a link is never cut off (it wouldn't work)
                o(line)
                o(o.c("     ↳ ", "2") + p["sotrep"])
        legend = ["Green: friend · ●LIVE: on Twitch now · SOTREP: open the link (Ctrl+click in most terminals)",
                  "All commands: brpw-cli help"]
        joined = " · ".join(legend)
        o(o.c(joined if len(joined) < width else legend[0], "2"))
        if len(joined) >= width:
            o(o.c(legend[1], "2"))

    def list(self, n: int = 30, all_: bool = False, sync: bool = True, as_json: bool = False) -> int:
        err = self.sync() if sync else None
        account = self.xkey.active() or ""
        players = report.recent(self.db, account, limit=n, current_only=not all_)
        if as_json:
            self.out(json.dumps(players, indent=1))
        else:
            when = time.strftime("%H:%M")
            self.show(players, f"Recently played with — {account or 'no account'}  ({len(players)} shown, {when})")
            if err:
                self.out(self.out.c(f"Not refreshed: {err}", "31"))
        return 0 if not err else 2

    def watch(self, interval: int = 60, n: int = 30, post: bool = False) -> int:
        interval = max(30, int(interval))
        try:
            while True:
                if self.out.color:
                    self.out("\033[2J\033[H")                 # clear the screen
                self.list(n=n)
                if post or self.discord["auto"]:
                    self.discord_post(quiet=True)
                self.out(self.out.c(f"Next refresh in {interval} s (Ctrl+C stops)", "2"))
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0

    def sotrep(self, gamertag: str, open_it: bool = False) -> int:
        url = sotrep.profile_url(gamertag)
        self.out(url)
        if open_it:
            webbrowser.open(url)
        return 0

    # ---- Discord
    def discord_post(self, force: bool = False, quiet: bool = False) -> int:
        account = self.xkey.active() or ""
        players = report.recent(self.db, account, limit=int(self.discord["limit"]))
        try:
            what = discord.post_list(self.discord, account, players, force=force)
        except ProviderError as e:
            self.out(self.out.c(f"Discord: {e}", "31"))
            return 2
        if not quiet or what != "unchanged":
            self.out({"sent": "Posted the list to Discord.", "edited": "Updated the list in Discord.",
                      "unchanged": "Discord: the list hasn't changed, nothing sent."}[what])
        return 0

    def discord_cmd(self, action: str, value: Optional[str]) -> int:
        if action == "set":
            try:
                self.discord.update(url=discord.check_url(value or ""))
            except ProviderError as e:
                self.out(str(e))
                return 1
            self.out("Webhook saved. Post now:  brpw-cli discord post    After every refresh:  brpw-cli discord auto on")
            return 0
        if action == "off":
            self.discord.update(url="", auto=False)
            self.out("Discord webhook removed.")
            return 0
        if action == "auto":
            on = (value or "").lower() in ("on", "1", "yes", "true")
            self.discord.update(auto=on)
            self.out("Discord: posted after every refresh (watch) and in the app." if on else "Discord: no automatic posts.")
            return 0
        if action == "post":
            return self.discord_post(force=True)
        self.out(f"Webhook: {'set' if self.discord['url'] else 'not set'} · after every refresh: "
                 f"{'on' if self.discord['auto'] else 'off'} · one message kept updated: "
                 f"{'yes' if self.discord['keep_one'] else 'no'} · players: {self.discord['limit']}")
        return 0


HELP = """Better Recently Played With - terminal version {version}

  brpw-cli                    refresh from Xbox and show your recently-played list
  brpw-cli list -n 50 --all   more players, also ones no longer in Xbox's list
  brpw-cli list --no-sync     show what's saved, without asking Xbox
  brpw-cli watch -i 60        refresh and show again every 60 s (Ctrl+C stops)
  brpw-cli menu               the menu (also what you get by double-clicking brpw-cli.exe)
  brpw-cli setup              first-time setup: add an account, optionally Discord

  brpw-cli accounts           your accounts (* = active)
  brpw-cli add                add an account: Microsoft sign-in in your browser
  brpw-cli add --key KEY      add an account with an OpenXBL key instead
  brpw-cli use NAME           switch to another account
  brpw-cli remove NAME        forget an account (the player history stays)

  brpw-cli sotrep GAMERTAG    the SOTREP link (--open opens it in your browser)
  brpw-cli discord            Discord status;  discord set URL | post | auto on | auto off | off

  Options anywhere: --no-color (plain text), --links (clickable names), --json (list as JSON)
  More about one command:  brpw-cli list -h   (or any other command with -h)
  The app and this terminal version share the same accounts and history."""


def started_by_double_click() -> bool:
    """Windows: true when this program got a console of its own (double-clicked in Explorer) instead of running in
    an open cmd / PowerShell / Terminal window. Such a window closes the moment the program ends, so the menu is shown
    and the window stays open. The one-file exe runs as two processes (a starter and the program) in one console."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ids = (ctypes.c_uint32 * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(ids, 8)
    except (AttributeError, OSError):
        return False
    return 0 < n <= (2 if getattr(sys, "frozen", False) else 1)


def size_console(cols: int = 160, rows: int = 45) -> bool:
    """Windows' classic console: make the window (and its line width) big enough for the list, keeping the scroll
    history; smaller when the screen can't fit it. Windows Terminal doesn't let programs resize it (the list then
    fits itself to the window instead). Returns True when resized."""
    if os.name != "nt" or os.environ.get("WT_SESSION"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT), ("Right", wintypes.SHORT),
                        ("Bottom", wintypes.SHORT)]

        class INFO(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                        ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]
        k32 = ctypes.windll.kernel32
        k32.GetLargestConsoleWindowSize.restype = COORD
        h = k32.GetStdHandle(-11)
        info = INFO()
        if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return False
        big = k32.GetLargestConsoleWindowSize(h)
        cols, rows = max(80, min(cols, big.X or cols)), max(20, min(rows, big.Y or rows))
        k32.SetConsoleScreenBufferSize(h, COORD(max(cols, info.dwSize.X), max(3000, info.dwSize.Y)))
        return bool(k32.SetConsoleWindowInfo(h, True, ctypes.byref(SMALL_RECT(0, 0, cols - 1, rows - 1))))
    except (AttributeError, OSError, ValueError):
        return False


class Menu:
    """The interactive version: a numbered menu, and a short setup on first use."""

    def __init__(self, cli: Cli, ask=input):
        self.cli, self.out, self.ask = cli, cli.out, ask

    def prompt(self, text: str) -> str:
        try:
            return self.ask(text).strip()
        except EOFError:
            return "q"

    def setup(self) -> None:
        o = self.out
        o(o.c("Welcome! Let's add your Xbox account.", "1;36"))
        o("  1  Sign in with Microsoft (recommended: your browser opens Microsoft's own page; this program never "
          "sees your password)")
        o("  2  Use an OpenXBL key (from xbl.io)")
        o("  s  Skip for now")
        while True:
            c = self.prompt("Choose 1, 2 or s: ").lower()
            try:
                if c == "1":
                    self.cli.add()
                    break
                if c == "2":
                    webbrowser.open("https://xbl.io/dashboard")
                    key = self.prompt("Paste your OpenXBL key (the dashboard opened in your browser): ")
                    if key and key != "q":
                        self.cli.add(key)
                        break
                if c in ("s", "q", ""):
                    return
            except ProviderError as e:
                o(o.c(f"That didn't work: {e}", "31"))
        if not self.cli.discord["url"]:
            url = self.prompt("Optional: a Discord webhook address to post your list to (Enter to skip): ")
            if url and url != "q":
                self.cli.discord_cmd("set", url)
        o(o.c("Setup done. Here's your list:", "1;36"))
        self.cli.list()

    def accounts(self) -> None:
        o = self.out
        self.cli.accounts()
        o("  a  add an account    u  switch account    r  remove an account    Enter  back")
        c = self.prompt("> ").lower()
        try:
            if c == "a":
                k = self.prompt("Enter for Microsoft sign-in, or paste an OpenXBL key: ")
                self.cli.add(k or None)
            elif c in ("u", "r"):
                names = self.cli.xkey.names()
                for i, n in enumerate(names, 1):
                    o(f"  {i}  {n}")
                pick = self.prompt("Number: ")
                if pick.isdigit() and 1 <= int(pick) <= len(names):
                    name = names[int(pick) - 1]
                    if c == "u":
                        self.cli.use(name)
                    elif self.prompt(f"Remove {name}? (y/n): ").lower() == "y":
                        self.cli.remove(name)
        except ProviderError as e:
            o(o.c(f"That didn't work: {e}", "31"))

    def discord(self) -> None:
        o = self.out
        self.cli.discord_cmd("status", None)
        o("  s  set the webhook address    p  post now    a  post after every refresh on/off    "
          "x  remove    Enter  back")
        c = self.prompt("> ").lower()
        if c == "s":
            self.cli.discord_cmd("set", self.prompt("Webhook address (Discord: channel settings > Integrations > "
                                                    "Webhooks > Copy Webhook URL): "))
        elif c == "p":
            self.cli.discord_cmd("post", None)
        elif c == "a":
            self.cli.discord_cmd("auto", "off" if self.cli.discord["auto"] else "on")
        elif c == "x":
            self.cli.discord_cmd("off", None)

    def run(self) -> int:
        o = self.out
        if not self.cli.xkey.names():
            self.setup()
        else:
            self.cli.list()
        while True:
            o("")
            o(o.c(f"Better Recently Played With  ·  {self.cli.xkey.active() or 'no account'}", "1;36"))
            o("  1  Refresh and show the list         2  Keep refreshing (every 60 s, Ctrl+C stops)")
            o("  3  Show older players too            4  Accounts (add, switch, remove)")
            o("  5  SOTREP link for a gamertag         6  Discord")
            o("  h  Help: all commands                q  Quit")
            c = self.prompt("Choose: ").lower()
            try:
                if c == "1":
                    self.cli.list()
                elif c == "2":
                    self.cli.watch()
                elif c == "3":
                    self.cli.list(n=100, all_=True, sync=False)
                elif c == "4":
                    self.accounts()
                elif c == "5":
                    gt = self.prompt("Gamertag: ")
                    if gt and gt != "q":
                        self.cli.sotrep(gt, open_it=self.prompt("Open it in your browser? (y/n): ").lower() == "y")
                elif c == "6":
                    self.discord()
                elif c in ("h", "help", "?"):
                    o(HELP.format(version=__version__))
                elif c in ("q", "quit", "exit"):
                    return 0
            except ProviderError as e:
                o(o.c(f"Error: {e}", "31"))
            except KeyboardInterrupt:
                o("")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)          # these work before or after the command
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="plain text, no colours")
    common.add_argument("--links", action="store_true", default=argparse.SUPPRESS,
                        help="clickable names instead of long links (modern terminals)")
    p = argparse.ArgumentParser(prog="brpw-cli", parents=[common], description="Your Xbox Recently played with "
                                "list (Sea of Thieves) with Twitch channels and SOTREP links, in a terminal.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)
    ls = sub.add_parser("list", help="refresh and show the list (the default)")
    ls.add_argument("-n", type=int, default=30, help="how many players (default 30)")
    ls.add_argument("--all", action="store_true", help="also players no longer in Xbox's list")
    ls.add_argument("--no-sync", action="store_true", help="don't ask Xbox, show what's saved")
    ls.add_argument("--json", action="store_true", help="the list as JSON")
    w = sub.add_parser("watch", help="refresh and show again every interval")
    w.add_argument("-i", "--interval", type=int, default=60, help="seconds between refreshes (at least 30)")
    w.add_argument("-n", type=int, default=30)
    w.add_argument("--discord", action="store_true", help="also post to Discord when the list changes")
    sub.add_parser("accounts", help="your accounts (* = active)")
    a = sub.add_parser("add", help="add an account: Microsoft sign-in in your browser, or --key KEY (OpenXBL)")
    a.add_argument("--key", help="an OpenXBL API key instead of Microsoft sign-in")
    r = sub.add_parser("remove", help="remove an account (the history stays)")
    r.add_argument("name")
    u = sub.add_parser("use", help="make an account the active one")
    u.add_argument("name")
    s = sub.add_parser("sotrep", help="the SOTREP link of a gamertag")
    s.add_argument("gamertag")
    s.add_argument("--open", action="store_true", help="open it in your browser")
    sub.add_parser("help", help="all commands, explained")
    sub.add_parser("menu", help="the interactive menu (also when you double-click the program)")
    sub.add_parser("setup", help="first-time setup: add an account, optionally a Discord webhook")
    d = sub.add_parser("discord", help="post the list to a Discord channel: set URL | post | auto on|off | off")
    d.add_argument("action", nargs="?", default="status", choices=["status", "set", "post", "auto", "off"])
    d.add_argument("value", nargs="?")
    return p


def main(argv=None) -> int:
    clicked = argv is None and len(sys.argv) <= 1 and started_by_double_click()
    if clicked:                                             # a window of its own: a title, and room for the list
        os.system("title Better Recently Played With")
        size_console()
    code = _main(argv, clicked)
    if clicked:                                             # a window of its own: let the last lines be read
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def _main(argv, clicked: bool) -> int:
    args = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # piped or redirected: UTF-8
    out = Out(color=False if getattr(args, "no_color", False) else None, links=getattr(args, "links", False))
    try:
        cli = Cli(out=out)
    except RuntimeError as e:                               # e.g. a database from a newer version
        out(str(e))
        return 1
    try:
        if args.cmd == "help":
            out(HELP.format(version=__version__))
            return 0
        if args.cmd == "menu" or clicked:
            return Menu(cli).run()
        if args.cmd == "setup" or (args.cmd is None and not cli.xkey.names() and sys.stdin.isatty()):
            Menu(cli).setup()
            return 0
        if args.cmd in (None, "list"):
            return cli.list(n=getattr(args, "n", 30), all_=getattr(args, "all", False),
                            sync=not getattr(args, "no_sync", False), as_json=getattr(args, "json", False))
        if args.cmd == "watch":
            return cli.watch(args.interval, args.n, args.discord)
        if args.cmd == "accounts":
            return cli.accounts()
        if args.cmd == "add":
            return cli.add(args.key)
        if args.cmd == "remove":
            return cli.remove(args.name)
        if args.cmd == "use":
            return cli.use(args.name)
        if args.cmd == "sotrep":
            return cli.sotrep(args.gamertag, args.open)
        if args.cmd == "discord":
            return cli.discord_cmd(args.action, args.value)
    except ProviderError as e:
        out(f"Error: {e}")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        cli.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
