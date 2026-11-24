"""Tkinter UI. All network/DB-heavy work runs in worker threads; results return via a queue."""
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, scrolledtext, ttk
from tkinter import font as tkfont

from . import __version__, chat_ui, direct, discord, report, social, sotrep, updater, xboxui
from .db import Database
from .paths import data_dir
from .providers import (ManualImportProvider, OpenXblKey, OpenXblProvider, ProviderError, account_picture,
                        lookup_gamertag)
from .game import game_running
from .msauth import MARK, MsAccounts
from .shared import SharedConfig, http_rest
from .shared_ui import SHARED_MARK, SharedListMixin
from .live import HOURLY_BUDGET, LIVE_SCOPE, LiveChecker
from .sync import SyncOutcome, SyncService
from .themes import THEMES, clamp, icon_path, palette, twitch_icon_path
from .twitch import CHECK_EVERY, CONSOLE_URL, STATUS_MAX_AGE, TwitchChecker, TwitchClient, TwitchCreds

MIN_INTERVAL = 30
REFRESH_MODES = ("Only while SoT is open", "Always", "Off")
XBOX_MODE = "While Xbox says I play SoT"     # your own Xbox presence: also counts Sea of Thieves on a console
PRESENCE_EVERY = 120                         # seconds between those presence checks (OpenXBL's hourly budget)
MESSAGES_EVERY = 60                          # seconds between checks for new Xbox messages (Microsoft sign-in)


class _MemCreds:
    """Credentials held in memory only, used to test a client ID/secret before anything is saved."""

    def __init__(self, cid, secret):
        self._c = (cid, secret)

    def load(self):
        return self._c


def twitch_live(a, now) -> bool:
    return bool(a["twitch_live"]) and a["twitch_checked"] is not None and now - a["twitch_checked"] <= STATUS_MAX_AGE


def fmt_ts(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


DEFAULT_HIDDEN = "xuid"          # card details hidden until the user shows them (Show on cards)


def hours_text(minutes, stats_at) -> str:
    """Hours played for the table: "1,234 h", "private" when Xbox didn't share it, "" when not asked yet."""
    if minutes is not None:
        return f"{int(minutes) // 60:,} h" if minutes >= 60 else f"{int(minutes)} min"
    return "private" if stats_at else ""


def _mix_hex(a, b, t):
    from .chat_ui import _mix
    return _mix(a, b, t)


def theme_titlebar(win, pal) -> bool:
    """Windows 11: title bar, its text and the window border in the theme's colours. Windows still draws the bar,
    so snapping, resizing, Alt-Tab and the taskbar keep working. Does nothing on older Windows or elsewhere."""
    def colorref(hexcol):
        h = hexcol.lstrip("#")
        return int(h[4:6] + h[2:4] + h[0:2], 16)              # 0x00BBGGRR
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)          # dark mode (buttons)
        for attr, col in ((35, pal["head"]), (36, pal["fg"]), (34, pal["head"])):     # caption, text, border
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(colorref(col))), 4)
        return True
    except (AttributeError, OSError, ValueError, tk.TclError):
        return False


class Spinner(tk.Canvas):
    """Small rotating arc shown while something loads (Tk has no native spinner)."""

    def __init__(self, master, size=18, color="#22c7d6", bg="#0b1626"):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0)
        self.size, self.color, self.angle, self.job = size, color, 0, None

    def recolor(self, color, bg):
        self.color = color
        self.configure(bg=bg)

    def start(self):
        if self.job is None:
            self._tick()

    def stop(self):
        if self.job is not None:
            self.after_cancel(self.job)
            self.job = None
        self.delete("all")

    def _tick(self):
        s = self.size
        self.delete("all")
        self.create_arc(2, 2, s - 2, s - 2, start=self.angle, extent=110, style="arc", outline=self.color, width=3)
        self.angle = (self.angle - 28) % 360
        self.job = self.after(55, self._tick)


class _TabCount:
    """The Friends & messages tab as the app's old Messages button: set_text shows the count on the tab,
    set_kind("accent") puts a dot on it."""

    def __init__(self, strip, key, base):
        self.strip, self.key, self.base = strip, key, base

    def set_text(self, text):
        self.strip.set_label(self.key, text)

    def set_kind(self, kind):
        self.strip.mark(self.key, kind == "accent")

    def cget(self, key):
        return self.strip.label(self.key) if key == "text" else None


class App(SharedListMixin):
    # where pictures and hours come from (swapped in tests, so they never reach Xbox)
    fetch_profiles = staticmethod(lambda key, xuids: social.profiles(key, xuids))
    fetch_minutes = staticmethod(lambda key, xuids: social.sot_minutes(key, xuids))
    fetch_account_picture = staticmethod(lambda key: account_picture(key))

    def __init__(self, root: tk.Tk, db: Database):
        self.root, self.db = root, db
        self.sync = SyncService(db)
        self.xkey = OpenXblKey(data_dir() / "openxbl_keys.json")
        self.msa = MsAccounts(data_dir() / "ms_accounts.json")     # Microsoft sign-ins (refresh tokens, DPAPI)
        direct.configure(self.msa)
        self.q: "queue.Queue" = queue.Queue()
        self.closing = False
        self.paused = db.get_setting("paused", "0") == "1"
        self.game_check = game_running     # replaced in tests
        self.game_was_running = False
        self.xbox_playing = None                 # last answer of your own Xbox presence (XBOX_MODE)
        self.xbox_was_playing = False
        self.presence_at = 0.0
        self.presence_busy = False
        self.theme = clamp(db.get_setting("theme"))
        self.pal = palette(self.theme)
        self.status_error = False
        self.after_id = None
        self.live_after_id = None
        self.live_busy = False
        self.busy = set()          # names of running background jobs (drives the spinner)
        self.to_top_after_sync = False
        self.live = LiveChecker(db, self.xkey)
        self.tcreds = TwitchCreds(data_dir() / "twitch.json")
        self.tclient = TwitchClient(self.tcreds)
        self.tcheck = TwitchChecker(db, self.tclient)
        self.twitch_after_id = None
        self.twitch_busy = False
        self.shared_cfg = SharedConfig(data_dir())
        self.shared_http = http_rest       # replaced in tests
        self.shared_after_id = None
        self.shared_busy = False
        self.msg_after_id = None                 # new-message pop-ups (Microsoft sign-in accounts only)
        self.msg_busy = False
        self.msg_seen = {}                       # key -> {xuid: time of the newest message already announced}
        self.msg_opened = {}                     # xuid -> when its chat was last opened here
        self.msg_unread = []                     # unread chats not opened here yet (the Messages button count)
        self.req_waiting = []                    # friend requests waiting for an answer (also counted)
        self.req_seen = {}                       # key -> xuids of requests already announced
        self.msg_rounds = 0
        self.toast = None
        self.inbox_win = None
        self.last_streams = self.last_live = None   # what the Live streams / Live now tabs showed last
        self.inbox_cache = {}                    # key -> last inbox (shown at once next time)
        self.chat_cache = {}                     # (key, xuid) -> last history
        self.pictures = chat_ui.Pictures(data_dir() / "pictures", lambda fn: self.q.put(("call", fn)))
        self.previews = chat_ui.Previews(lambda fn: self.q.put(("call", fn)))     # Twitch's live snapshots
        self.discord_cfg = discord.DiscordConfig(data_dir() / "discord.json")
        self.root.after(20_000, self._prune_round)      # after the first sync, never in its way
        self._badged, self._pic_job = {}, None         # pictures with a Twitch badge; pending redraw
        self.profiles_busy, self.profiles_at = False, 0.0
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()
        self.root.after(200, self._poll)
        self._schedule()
        self._schedule_live(15_000)
        self._schedule_twitch(20_000)
        self._schedule_shared(10_000)
        self._schedule_messages(8_000)
        self.root.after(6_000, lambda: self._profiles_round(force=True))   # pictures and hours without a sync
        if db.recovered_from:
            self.set_status(f"Database was damaged; moved aside to {db.recovered_from}", error=True)
        updater.cleanup(updater.running_exe())                 # the old program file of a finished update
        self.root.after(30_000, lambda: updater.cleanup(updater.running_exe()))   # the old app may still be closing
        if db.get_setting("auto_update_check", "1") == "1":
            threading.Thread(target=self._update_check, args=(False,), daemon=True).start()

    # ---- layout ----
    def _apply_style(self):
        r, P = self.root, self.pal
        acc, BG, FG, PANEL, HEAD = P["accent"], P["bg"], P["fg"], P["panel"], P["head"]
        r.configure(bg=BG)
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL,
                     lightcolor=PANEL, darkcolor=PANEL, troughcolor=BG, font=("Segoe UI", 9))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=HEAD, foreground=FG, relief="flat", padding=6,
                     font=("Segoe UI", 9, "bold"))
        st.map("Treeview.Heading", background=[("active", acc)])
        st.map("Treeview", background=[("selected", acc)], foreground=[("selected", "#ffffff")])
        st.configure("TButton", background=HEAD, foreground=FG, padding=(10, 5), relief="flat")
        st.map("TButton", background=[("active", acc), ("pressed", acc)])
        for w in ("TEntry", "TSpinbox", "TCombobox"):
            st.configure(w, fieldbackground=PANEL, foreground=FG, insertcolor=FG, arrowcolor=FG,
                         selectbackground=PANEL, selectforeground=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)],
               background=[("active", HEAD)])
        st.map("TSpinbox", fieldbackground=[("readonly", PANEL)])
        r.option_add("*TCombobox*Listbox.background", PANEL)
        r.option_add("*TCombobox*Listbox.foreground", FG)
        r.option_add("*TCombobox*Listbox.selectBackground", acc)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(16, 6), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", HEAD), ("!selected", PANEL)],
               foreground=[("selected", "#ffffff"), ("!selected", P["muted"])],
               padding=[("selected", (16, 8)), ("!selected", (16, 6))],
               lightcolor=[("selected", acc), ("!selected", PANEL)], bordercolor=[("selected", acc), ("!selected", PANEL)])
        st.configure("Hint.TLabel", foreground=P["muted"])
        r.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        # top bar: same colour as the title bar, flat buttons, one accent button
        st.configure("Bar.TFrame", background=HEAD)
        st.configure("BarHint.TLabel", background=HEAD, foreground=P["muted"])
        st.configure("Tool.TButton", background=HEAD, foreground=FG, padding=(10, 6), relief="flat", borderwidth=0)
        st.map("Tool.TButton", background=[("active", acc), ("pressed", acc)],
               foreground=[("disabled", P["muted"]), ("active", BG)])
        st.configure("Accent.TButton", background=acc, foreground=BG, padding=(12, 6), relief="flat",
                     font=("Segoe UI", 9, "bold"))
        st.map("Accent.TButton", background=[("disabled", PANEL), ("active", FG), ("pressed", FG)],
               foreground=[("disabled", P["muted"])])
        st.map("TButton", foreground=[("disabled", P["muted"])])
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(sb, background=HEAD, troughcolor=PANEL, bordercolor=PANEL, arrowcolor=FG,
                         lightcolor=HEAD, darkcolor=HEAD, gripcount=0)
            st.map(sb, background=[("disabled", PANEL), ("active", acc), ("pressed", acc)],   # disabled: all fits
                   arrowcolor=[("disabled", P["muted"])])
        # the player list: tall rows with a round profile picture, a calmer header
        st.configure("Players.Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=50,
                     borderwidth=0, font=("Segoe UI", 10))
        st.configure("Players.Treeview.Heading", background=PANEL, foreground=P["muted"], relief="flat",
                     padding=(8, 8), font=("Segoe UI", 8, "bold"))
        st.map("Players.Treeview.Heading", background=[("active", HEAD)], foreground=[("active", FG)])
        st.map("Players.Treeview", background=[("selected", _mix_hex(PANEL, acc, 0.55))],
               foreground=[("selected", "#ffffff")])
        st.layout("Players.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])          # no frame around it
        # the Messages list: a tree (name + preview line) without the expand arrows
        st.configure("Inbox.Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24,
                     borderwidth=0, indent=0)

        def no_arrow(layout):
            return [(n, {**o, "children": no_arrow(o["children"])} if "children" in o else o)
                    for n, o in layout if n != "Treeitem.indicator"]
        st.layout("Inbox.Treeview.Item", no_arrow(st.layout("Treeview.Item")))
        # every window, now and later, gets a title bar in the theme's colours
        for cls in ("Tk", "Toplevel"):                         # the main window and every dialog, when shown
            r.bind_class(cls, "<Map>", lambda e: theme_titlebar(e.widget, P)
                         if isinstance(e.widget, (tk.Tk, tk.Toplevel)) else None)
        for w in [r] + [w for w in r.winfo_children() if isinstance(w, tk.Toplevel)]:
            theme_titlebar(w, P)
        old = getattr(self, "_drawn_pal", None)
        if old and old != P and hasattr(self, "tree"):
            self._recolor(self.root, old, P)
        self._drawn_pal = P
        if hasattr(self, "tree"):
            self.tree.tag_configure("friend", background=P["friend"])
            self.tree.tag_configure("odd", background=P["stripe"])
            self.status_lbl.configure(foreground="#ff7b72" if self.status_error else FG)
            self.legend.configure(bg=chat_ui.friend_shade(chat_ui._mix(P["bg"], P["panel"], 0.85)), fg=FG)
            self.spinner.recolor(acc, BG)

    def _recolor(self, widget, old, new):
        """A theme change: every plain Tk widget in the main window whose colour was one of the old theme's colours
        gets the new theme's colour; the drawn widgets (pills, fields, cards) redraw with the new palette."""
        mapping = {}
        for k in ("bg", "head", "panel", "fg", "muted", "accent", "friend"):
            mapping.setdefault(str(old[k]).lower(), new[k])
        for w in [widget] + self._all_children(widget):
            if isinstance(w, tk.Toplevel):
                continue
            if isinstance(w, (chat_ui.RoundButton, chat_ui.RoundBox, chat_ui.PlayerCards)):
                w.pal = new
            if isinstance(w, chat_ui.RoundBox):
                old_fill = w.fill
                w.fill = chat_ui._mix(new["panel"], new["fg"], 0.07)
                mapping[str(old_fill).lower()] = w.fill
            if isinstance(w, ttk.Widget):
                continue
            for opt in ("bg", "fg", "insertbackground", "activebackground"):
                try:
                    v = str(w.cget(opt)).lower()
                except tk.TclError:
                    continue
                if v in mapping:
                    try:
                        w.configure(**{opt: mapping[v]})
                    except tk.TclError:
                        pass
        for w in [widget] + self._all_children(widget):
            if isinstance(w, chat_ui.RoundButton):
                w._draw()
            elif isinstance(w, chat_ui.RoundBox):
                w._layout()
            elif isinstance(w, chat_ui.PlayerCards):
                w._schedule()

    def _all_children(self, w):
        out = []
        for c in w.winfo_children():
            if not isinstance(c, tk.Toplevel):
                out.append(c)
                out += self._all_children(c)
        return out

    def _apply_icon(self):
        try:
            self._icon = tk.PhotoImage(file=str(icon_path(self.theme)))
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass  # missing icon file must never stop the app

    def set_theme(self, n: int):
        self.theme = clamp(n)
        self.pal = palette(self.theme)
        self.db.set_setting("theme", str(self.theme))
        self._apply_style()
        self._apply_icon()

    def _build(self):
        r = self.root
        r.title("Better Recently Played With")
        r.geometry("1080x600")
        r.minsize(480, 400)                                 # slim works too: rows wrap, Friends becomes one column
        self._apply_style()
        self._apply_icon()

        # ---- top bar: account and refresh on the left, the main actions on the right, the rest in the ☰ menu
        P = self.pal
        self._pills = []                                   # drawn buttons, recoloured when the theme changes

        def pill(parent, text, cmd, kind="soft", **kw):
            b = chat_ui.RoundButton(parent, text, cmd, P, parent.cget("bg"), kind=kind, **kw)
            self._pills.append(b)
            return b

        # ---- top bar: account and refresh on the left, the main actions on the right, the rest in the ☰ menu
        bar = tk.Frame(r, bg=P["head"])
        bar.pack(fill="x")
        self.top_bar = bar
        inner = chat_ui.Flow(bar, P["head"], gap=8)
        inner.pack(fill="x", padx=12, pady=9)
        self.acct_var = tk.StringVar()
        self.acct_box = chat_ui.PillSelect(inner, P, P["head"], self.acct_var, on_change=self.switch_account,
                                           extra=[("Add Xbox account...", self.set_key)],
                                           font=("Segoe UI", 10, "bold"), padx=16, pady=7,
                                           image_for=self._account_image)
        self._pills.append(self.acct_box)
        inner.add(self.acct_box)
        inner.add(pill(inner, "↻  Refresh", self.refresh_now, kind="accent", font=("Segoe UI", 10, "bold"), padx=16,
                       pady=7))
        self.mode_var = tk.StringVar(value=REFRESH_MODES[2] if self.paused else
                                     XBOX_MODE if self.db.get_setting("refresh_xbox", "0") == "1" else
                                     REFRESH_MODES[0] if self.db.get_setting("only_with_game", "1") == "1"
                                     else REFRESH_MODES[1])
        self.mode_box = chat_ui.PillSelect(inner, P, P["head"], self.mode_var,
                                           values=(REFRESH_MODES[0], XBOX_MODE, REFRESH_MODES[1], REFRESH_MODES[2]),
                                           on_change=lambda: self.set_refresh_mode(self.mode_var.get()),
                                           prefix="Auto:  ", padx=14, pady=7)
        self._pills.append(self.mode_box)
        inner.add(self.mode_box, space=8)
        every = tk.Frame(inner, bg=P["head"])                 # "every 60 s" wraps as one piece
        tk.Label(every, text="every", bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left",
                                                                                           padx=(2, 4))
        self.interval = tk.IntVar(value=int(self.db.get_setting("interval", "60")))
        sp = ttk.Spinbox(every, from_=MIN_INTERVAL, to=3600, increment=10, width=4, textvariable=self.interval,
                         command=self._save_interval)
        sp.pack(side="left")
        sp.bind("<FocusOut>", lambda e: self._save_interval())
        tk.Label(every, text="s", bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left", padx=(3, 0))
        inner.add(every)
        self.auto_upd = tk.BooleanVar(value=self.db.get_setting("auto_update_check", "1") == "1")
        self.menu_btn = pill(inner, "☰", self._main_menu, padx=12, pady=7)
        inner.add(self.menu_btn, right=True)
        self.msg_popups = tk.BooleanVar(value=self.db.get_setting("message_popups", "1") == "1")

        # ---- tabs: everything opens inside this window (no separate windows); switching tabs keeps each page as
        # it was, so nothing reloads; only Refresh / Check again asks Xbox or Twitch again
        nav = tk.Frame(r, bg=P["head"])
        nav.pack(fill="x")
        self.tabstrip = chat_ui.TabStrip(nav, P, P["head"], self._tab_clicked, self._tab_close)
        self.tabstrip.pack(fill="x", padx=6, pady=(0, 2))
        self.pages_host = tk.Frame(r, bg=P["bg"])
        self.pages_host.pack(fill="both", expand=True)
        self.pages, self.page_now = {}, None
        for key, label in self.MAIN_TABS:
            self.tabstrip.add(key, label)
        self.msg_btn = _TabCount(self.tabstrip, "social", self.SOCIAL)
        self._refresh_accounts()

        self.players_tab = tk.Frame(self.pages_host, bg=P["bg"])
        self._show_page("players")
        try:
            self.twitch_img = tk.PhotoImage(file=str(twitch_icon_path()))
        except tk.TclError:
            self.twitch_img = tk.PhotoImage(width=1, height=1)   # missing asset must never stop the app

        # ---- search, then how the list is sorted and which details the cards show
        bar2 = tk.Frame(self.players_tab, bg=P["bg"])
        bar2.pack(fill="x", padx=14, pady=(12, 4))
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.reload())

        def make_search(parent, bg):
            return tk.Entry(parent, textvariable=self.search, bg=bg, fg=P["fg"], insertbackground=P["fg"],
                            relief="flat", bd=0, font=("Segoe UI", 10), highlightthickness=0)
        self.crew_var = tk.StringVar()                    # "⚓ Your sloop: A, B" while you're on a ship
        self.crew_line = tk.Label(self.players_tab, textvariable=self.crew_var, bg=P["bg"], fg=P["fg"],
                                  font=("Segoe UI", 10, "bold"), anchor="w")
        self.crew_line.pack(fill="x", padx=18, pady=(10, 0), before=bar2)
        self.crew_line.pack_forget()
        self.search_box = chat_ui.RoundBox(bar2, P, P["bg"], make_search, height=38, radius=19)
        self.search_box.pack(side="left", fill="x", expand=True)
        hint = tk.Label(bar2, text="Search players, tags or XUID", bg=self.search_box.fill, fg=P["muted"],
                        font=("Segoe UI", 10))
        entry = self.search_box.inner

        def show_hint(*_):
            if self.search.get() or self.root.focus_get() is entry:
                hint.place_forget()
            else:
                hint.place(in_=self.search_box, x=20, rely=0.5, anchor="w")
        self.search.trace_add("write", show_hint)
        entry.bind("<FocusIn>", show_hint, add="+")
        entry.bind("<FocusOut>", show_hint, add="+")
        hint.bind("<Button-1>", lambda e: entry.focus_set())
        self.root.after_idle(show_hint)
        sortbar = chat_ui.Flow(self.players_tab, P["bg"])
        sortbar.pack(fill="x", padx=14, pady=(6, 4))
        sortbar.add(tk.Label(sortbar, text="Sort by", bg=P["bg"], fg=P["muted"], font=("Segoe UI", 9), padx=4))
        self.sort_chips = {}
        for col, label in (("current", "Xbox list"), ("last_seen", "Last seen"), ("hours", "Hours"),
                           ("gamertag", "Name"), ("twitch", "Twitch"), ("tags", "Tags")):
            b = sortbar.add(pill(sortbar, label, lambda c=col: self.sort_by(c), padx=12, pady=5))
            b.label = label
            self.sort_chips[col] = b
        reset_pill = pill(sortbar, "Reset view", self.reset_table, padx=12, pady=5)
        self.hide_crew = self.db.get_setting("hide_crew", "1") == "1"
        self.crew_btn = pill(sortbar, "", self.toggle_crew, padx=12, pady=5)
        self._crew_button()
        self.fields_btn = pill(sortbar, "Show on cards  ▾", self._fields_menu, padx=12, pady=5)
        self.list_view = self.db.get_setting("list_view", "cards")
        if self.list_view not in self.VIEW_NAMES:
            self.list_view = "cards"
        self.view_var = tk.StringVar(value=self.VIEW_NAMES[self.list_view])
        self.view_box = chat_ui.PillSelect(sortbar, P, P["bg"], self.view_var, values=list(self.VIEW_NAMES.values()),
                                           on_change=lambda: self.set_list_view(
                                               {v: k for k, v in self.VIEW_NAMES.items()}[self.view_var.get()]),
                                           extra=[("Hide search and sort  (Ctrl+F shows them)",
                                                   lambda: self.show_bars(False))],
                                           prefix="View: ", padx=12, pady=5)
        self._pills.append(self.view_box)
        for b in (self.view_box, self.fields_btn, self.crew_btn, reset_pill):
            sortbar.add(b, right=True)
        self.bars = (bar2, sortbar)

        cols = ("gamertag", "hours", "xuid", "tags", "twitch", "last_seen", "origin", "current")
        self.tree = chat_ui.PlayerCards(self.players_tab, P, cols)
        self.col_titles = {"gamertag": "Gamertag", "hours": "Hours in SoT", "xuid": "XUID", "tags": "Tags",
                           "twitch": "Twitch", "last_seen": "Last seen", "origin": "Source", "current": "#"}
        self.sort_col, self.sort_desc = "current", False  # Xbox list order (#1 on top), then older history
        saved = self.db.get_setting("player_sort", "").split(":")        # the sort you picked last time
        if len(saved) == 2 and saved[0] in self.SORT_KEYS:
            self.sort_col, self.sort_desc = saved[0], saved[1] == "1"
        for c, w in self.COL_WIDTHS.items():
            self.tree.column(c, width=w)
        self._apply_hidden_columns()
        self._update_headings()
        self.tree.tag_configure("friend", background=self.pal["friend"])
        self.tree.tag_configure("odd", background=self.pal["stripe"])
        self.tree.set_view(self.list_view)
        self.tree.pack(fill="both", expand=True, padx=(6, 8), pady=(2, 0))
        self.bars_shown = True
        if self.db.get_setting("show_bars", "1") == "0":
            self.show_bars(False, quiet=True)
        r.bind("<Control-f>", lambda e: (self._show_page("players"), self.show_bars(True),
                                         self.search_box.inner.focus_set()))
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: None if self._twitch_cell(e) else self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        # per-player actions live in the right-click menu; whole-list actions are in the top bar
        self.tree.bind("<Button-3>", self._row_menu)
        self.tree.bind("<Shift-F10>", self._row_menu)
        for key in ("<App>", "<Menu>"):                    # the menu key: "App" on Windows, "Menu" on Linux
            try:
                self.tree.bind(key, self._row_menu)
            except tk.TclError:                            # not a key name on this system
                pass

        # ---- status bar: what's happening on the left, legend and count on the right
        self.status = tk.StringVar()
        self.live_status = tk.StringVar(value="")
        statusbar = ttk.Frame(r)
        statusbar.pack(fill="x", padx=14, pady=(6, 8))
        self.spinner = Spinner(statusbar, color=self.pal["accent"], bg=self.pal["bg"])
        self.spinner.pack(side="left", padx=(0, 6))
        self.count_lbl = ttk.Label(statusbar, text="")
        self.count_lbl.pack(side="right")
        legend = tk.Label(statusbar, text=" Friend ", fg=self.pal["fg"], font=("Segoe UI", 8),
                          bg=chat_ui.friend_shade(chat_ui._mix(self.pal["bg"], self.pal["panel"], 0.85)))
        legend.pack(side="right", padx=(0, 10))
        self.legend = legend
        ttk.Label(statusbar, image=self.twitch_img, text=" live now", compound="left",
                  style="Hint.TLabel").pack(side="right", padx=(0, 10))
        ttk.Label(statusbar, text="Right-click a player for more", style="Hint.TLabel").pack(side="right",
                                                                                            padx=(0, 14))
        self.status_extras = [w for w in statusbar.winfo_children() if w is not self.spinner]   # hidden when slim
        self.status_lbl = ttk.Label(statusbar, textvariable=self.status, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.set_status("Ready. Add an Xbox account (☰ menu) or import players.")
        self.slim, self.slim_hooks = None, []
        r.bind("<Configure>", lambda e: self._resized() if e.widget is r else None, add="+")

    # ---- helpers ----
    def set_status(self, text, error=False):
        last = self.db.get_setting("last_sync")
        suffix = f"   |   Last successful live sync: {fmt_ts(int(last))}" if last else "   |   No live sync yet"
        self.status.set(text + suffix)
        self.status_error = error
        self.status_lbl.configure(foreground="#ff7b72" if error else self.pal["fg"])

    SORT_KEYS = {
        "gamertag": lambda a: a["gamertag"].casefold(),
        "xuid": lambda a: int(a["xuid"]) if a["xuid"] else -1,
        "tags": lambda a: (a["tags"] or "").casefold(),
        "twitch": lambda a: (0 if twitch_live(a, time.time()) else 1 if a["twitch_name"] else 2,
                             (a["twitch_name"] or "").casefold()),
        "last_seen": lambda a: (a["last_seen"], -(a["pos"] if a["pos"] is not None else 10 ** 9)),
        "origin": lambda a: a["source"],
        "hours": lambda a: a["sot_minutes"] if a["sot_minutes"] is not None else -1,
        "current": lambda a: (0, a["pos"]) if a["pos"] is not None else (1, -a["last_seen"]),
    }

    COL_WIDTHS = {"gamertag": 180, "hours": 96, "xuid": 130, "tags": 110, "twitch": 190, "last_seen": 115,
                  "origin": 60, "current": 44}

    def reset_table(self):
        """Back to the default view: Xbox list order, no search, all columns shown at their normal width."""
        self.sort_col, self.sort_desc = "current", False
        self.db.set_setting("player_sort", "current:0")
        self.db.set_setting("hidden_columns", DEFAULT_HIDDEN)
        self._apply_hidden_columns()
        for c, w in self.COL_WIDTHS.items():
            self.tree.column(c, width=w)
        self._update_headings()
        self.search.set("")                       # also reloads
        self.reload(to_top=True)

    def sort_by(self, col):
        self.sort_desc = not self.sort_desc if col == self.sort_col else (col == "last_seen")
        self.sort_col = col
        self.db.set_setting("player_sort", f"{col}:{1 if self.sort_desc else 0}")
        self._update_headings()
        self.reload()

    def _update_headings(self):
        """The active sort chip is filled, with an arrow for the direction."""
        for c, b in getattr(self, "sort_chips", {}).items():
            active = c == self.sort_col
            b.set_kind("accent" if active else "soft")
            b.set_text(b.label + ((" ▼" if self.sort_desc else " ▲") if active else ""))

    def _fields_menu(self):
        """Which details the cards show (remembered), the same as hiding a column used to be."""
        b = self.fields_btn
        ev = type("E", (), {"x_root": b.winfo_rootx(), "y_root": b.winfo_rooty() + b.winfo_height() + 2})()
        self._column_menu(ev)

    def reload(self, to_top=False):
        sel = set() if to_top else {self.tree.set(i, "xuid") + "|" + self.tree.set(i, "gamertag")
                                    for i in self.tree.selection()}
        top = 0.0 if to_top else self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        live_ids = {r["id"] for r in self.live.live_now()}   # Xbox says broadcasting (fresh results only)
        now = time.time()
        rows = sorted(self.db.list_accounts(self.search.get(), self.xkey.active()),
                      key=self.SORT_KEYS[self.sort_col], reverse=self.sort_desc)
        if self.hide_crew:                                   # your crewmates aren't "recently met"
            crew = self.db.CREW_TAG.casefold()
            rows = [a for a in rows if crew not in [t.strip().casefold() for t in (a["tags"] or "").split(",")]]
        for n, a in enumerate(rows):
            friend = a["relation"] in ("Friend", "In your list")
            tw_live = twitch_live(a, now)                    # exact: Twitch itself says this saved channel is live
            if a["twitch_never"] is not None:
                tw_text = "⛔ " + (a["twitch_never"] or "never link")
            else:
                tw_text = (a["twitch_name"] or "") + {"auto": " ⚙", "shared": SHARED_MARK}.get(
                    a["twitch_source"], "")
                if tw_live:
                    v = a["twitch_viewers"]
                    tw_text = (f"● {v}  " if v is not None else "●  ") + tw_text
            live = a["id"] in live_ids or tw_live
            iid = self.tree.insert(
                "", "end", iid=str(a["id"]), image=self._row_picture(a["pic"], live),
                tags=(("friend",) if friend else ()) + (("odd",) if n % 2 else ()) + (("live",) if live else ()),
                values=(
                    a["gamertag"], hours_text(a["sot_minutes"], a["stats_at"]), a["xuid"] or "", a["tags"] or "",
                    tw_text, fmt_ts(a["last_seen"]),
                    "Live" if a["source"] in ("xbox-live", "openxbl") else "Import",
                    str(a["pos"] + 1) if a["pos"] is not None else ""))
            if (a["xuid"] or "") + "|" + a["gamertag"] in sel:
                self.tree.selection_add(iid)
        self.tree.yview_moveto(top)  # a background refresh must not scroll the list back to the top
        self.count_lbl.configure(text=f"{self.db.count_accounts()} unique accounts")
        self._shown_xuids = [a["xuid"] for a in rows[:150] if a["xuid"]]       # for the pictures/hours round

    VIEW_NAMES = {"cards": "Cards", "compact": "Compact", "rows": "Rows"}
    VIEW_PICTURE = {"cards": chat_ui.PICTURE_SIZE, "compact": 32, "rows": 0}

    def set_list_view(self, view):
        """Cards, Compact (one line, small picture) or Rows (thin, no pictures); remembered."""
        self.list_view = view
        self.db.set_setting("list_view", view)
        self.view_var.set(self.VIEW_NAMES[view])
        self.tree.set_view(view)
        self.reload()

    def show_bars(self, on, quiet=False):
        """Show or hide the search box and the sort row above the list (remembered; Ctrl+F shows them)."""
        if on == self.bars_shown:
            return
        self.bars_shown = on
        self.db.set_setting("show_bars", "1" if on else "0")
        for w in self.bars:
            if on:
                w.pack(fill="x", padx=14, pady=(12, 4) if w is self.bars[0] else (6, 4), before=self.tree)
            else:
                w.pack_forget()
        if not quiet:
            self.set_status("Search and sort are shown." if on else
                            "Search and sort are hidden: Ctrl+F (or ☰ → List view) shows them again.")

    def _row_picture(self, raw, live):
        """The round profile picture for a row, with a small Twitch badge when live ("" = nothing)."""
        size = self.VIEW_PICTURE.get(getattr(self, "list_view", "cards"), chat_ui.PICTURE_SIZE)
        if not size:
            return ""
        img = self.pictures.get(raw, lambda raw=raw: self._picture_ready(raw), size=size) if raw else None
        if not live:
            return img or ""
        k = (raw if img is not None else "", "live", size)
        if k not in self._badged:
            out = tk.PhotoImage(width=size, height=size)
            if img is not None:
                out.tk.call(out, "copy", img)
            out.tk.call(out, "copy", self.twitch_img, "-to", size - 16, size - 16, "-compositingrule", "overlay")
            self._badged[k] = out
        return self._badged[k]

    def _picture_ready(self, raw):
        """A profile picture arrived: put it on the rows that wait for it (no full reload)."""
        if self._pic_job is None:
            self._pic_job = self.root.after(150, self._pictures_arrived)

    def _pictures_arrived(self):
        self._pic_job = None
        self.reload()

    # ---- profile pictures and hours played, filled in the background ----
    def _profiles_round(self, force=False):
        """After a sync (and at start): pictures for players that have none (Xbox's player summary, 10 per request)
        and Sea of Thieves hours for players not asked in the last day (one request per 50; Microsoft sign-in only).
        Only the players in Xbox's list and the top of the table; at most once a minute."""
        key = self.xkey.get()
        if not key or self.profiles_busy or self.closing:
            return
        if not force and time.time() - self.profiles_at < 60:
            return
        owner = self.xkey.active() or ""
        xuids = [r["xuid"] for r in self.db.list_accounts("", owner) if r["in_latest"] and r["xuid"]]
        xuids += getattr(self, "_shown_xuids", [])
        need_pic, need_stats = self.db.profiles_due(xuids)
        # pictures of players in Xbox's list come with every sync; these are older players further down: few at a
        # time, so Xbox's rate limit isn't reached
        need_pic, need_stats = need_pic[:20], (need_stats[:150] if direct.is_ms_key(key) else [])
        if not need_pic and not need_stats:
            return
        self.profiles_busy, self.profiles_at = True, time.time()

        def work():
            pics, mins, err = {}, {}, None
            try:
                if need_pic:
                    found = self.fetch_profiles(key, need_pic)
                    pics = {x: (found.get(x) or {}).get("pic", "") for x in need_pic}
                if need_stats:
                    mins = self.fetch_minutes(key, need_stats)
            except ProviderError as e:
                err = str(e)
            except Exception as e:                       # never let a worker thread die silently
                err = f"Unexpected error: {e}"
            self.q.put(("call", lambda: done(pics, mins, err)))

        def done(pics, mins, err):
            self.profiles_busy = False
            if pics:
                self.db.save_pictures(pics)
            if need_stats and err is None:
                self.db.save_minutes(need_stats, mins)
            if pics or (need_stats and err is None):
                self.reload()
        threading.Thread(target=work, daemon=True).start()

    def _twitch_cell(self, event):
        """The row id when the click was on the live icon or in the Twitch column, else None."""
        row = self.tree.identify_row(event.y)
        self.tree._last_y = event.y                       # the cards need the row to know where the Twitch pill is
        col = self.tree.identify_column(event.x)
        if not row:
            return None
        if col == "#0":
            return row if "live" in self.tree.item(row, "tags") else None
        idx = int(col[1:]) - 1 if col.startswith("#") and col[1:].isdigit() else -1
        cols = self.tree["displaycolumns"]                 # "#n" counts the columns on screen, not all of them
        if not cols or cols[0] == "#all":
            cols = self.tree["columns"]
        return row if 0 <= idx < len(cols) and cols[idx] == "twitch" else None

    def _on_tree_click(self, event):
        """A click on the Twitch icon or the Twitch column opens that player's Twitch channel."""
        row = self._twitch_cell(event)
        if row is None:
            return
        links = self.db.twitch_links([int(row)])
        if links:
            webbrowser.open(f"https://www.twitch.tv/{links[0]['login']}")
        elif "live" in self.tree.item(row, "tags"):
            self.set_status("Xbox says this player is broadcasting, but no Twitch channel is linked yet. "
                            "Right-click > Link Twitch channel..., or use Live streams, to link it.")

    def _row_menu_items(self):
        """(label, command or None for a separator, enabled) for the right-click menu of the current selection."""
        ids = self._selected_ids()
        one = len(ids) == 1
        linked = bool(self.db.twitch_links(ids))
        never = self.db.twitch_never(ids)
        all_never = bool(ids) and len(never) == len(ids)
        p = self._one_player() if one else None
        xbox_ok = bool(p and p[1] and self.xkey.get())
        following = xbox_ok and self.db.friend_relations(self.xkey.active()).get(p[1]) in self.FOLLOWING
        return [
            ("Open SOTREP", self.open_selected, True),
            ("Open Twitch", self.open_twitch_selected, linked),
            ("Check live", self.check_live_selected, True),
            ("Encounters", self.encounters_dialog, True),
            (None, None, True),
            ("Remove Xbox friend..." if following else "Add Xbox friend",
             self.remove_friend_selected if following else self.add_friend_selected, xbox_ok),
            ("Xbox messages...", self.message_selected, xbox_ok),
            ("Open Xbox profile", self.xbox_profile_selected, one),
            (None, None, True),
            ("Tag...", self.tag_selected, True),
            ("Untag", self.untag_selected, True),
            (None, None, True),
            ("Link Twitch channel...", self.link_twitch_selected, one),
            ("Allow linking again" if all_never else "Never link...", self.never_link_selected, True),
        ] + ([(None, None, True), ("Remove from master list", self.remove_from_master, True)]
             if self.is_list_owner() else [])

    HIDEABLE = ("hours", "xuid", "tags", "twitch", "last_seen", "origin", "current")

    def _apply_hidden_columns(self):
        hidden = set(filter(None, self.db.get_setting("hidden_columns", DEFAULT_HIDDEN).split(",")))
        self.tree["displaycolumns"] = [c for c in self.tree["columns"] if c not in hidden]

    def toggle_column(self, col):
        hidden = set(filter(None, self.db.get_setting("hidden_columns", DEFAULT_HIDDEN).split(",")))
        hidden ^= {col}
        self.db.set_setting("hidden_columns", ",".join(sorted(hidden)))
        self._apply_hidden_columns()

    def _column_menu(self, event):
        """Right-click on the headings: show or hide columns (remembered)."""
        shown = set(self.tree["displaycolumns"])
        m = tk.Menu(self.root, tearoff=0, bg=self.pal["panel"], fg=self.pal["fg"], selectcolor=self.pal["fg"],
                    activebackground=self.pal["accent"], activeforeground=self.pal["bg"])
        self.col_vars = {}
        for c in self.HIDEABLE:
            v = self.col_vars[c] = tk.BooleanVar(value=c in shown)
            label = "Xbox list #" if c == "current" else self.col_titles[c]
            m.add_checkbutton(label=label, variable=v, command=lambda c=c: self.toggle_column(c))
        self.column_menu = m
        m.tk_popup(event.x_root, event.y_root)

    def _row_menu(self, event):
        """Right-click: act on the clicked player (or on the selection when the click is inside it)."""
        if getattr(event, "y", None) is not None and self.tree.identify_region(event.x, event.y) == "heading":
            return self._column_menu(event)
        row = self.tree.identify_row(event.y) if getattr(event, "y", None) is not None else ""
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
            self.tree.focus(row)
        if not self.tree.selection():
            return
        m = tk.Menu(self.root, tearoff=0, bg=self.pal["panel"], fg=self.pal["fg"],
                    activebackground=self.pal["accent"], activeforeground=self.pal["bg"],
                    disabledforeground=self.pal["muted"])
        for label, cmd, enabled in self._row_menu_items():
            if label is None:
                m.add_separator()
            else:
                m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")
        self.row_menu = m                                            # kept for tests
        x = getattr(event, "x_root", None)
        if x is None:                                                # keyboard: open next to the focused row
            box = self.tree.bbox(self.tree.selection()[0]) or (0, 0, 0, 0)
            x, y = self.tree.winfo_rootx() + 40, self.tree.winfo_rooty() + box[1] + box[3]
        else:
            y = event.y_root
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

    def _set_busy(self, name: str, on: bool):
        (self.busy.add if on else self.busy.discard)(name)
        if self.busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _menu(self, parent=None):
        P = self.pal
        return tk.Menu(parent or self.root, tearoff=0, bg=P["panel"], fg=P["fg"], selectcolor=P["fg"], bd=0,
                       activebackground=P["accent"], activeforeground=P["bg"], font=("Segoe UI", 9))

    KEEP_DAYS = 14

    def keep_days(self) -> int:
        try:
            return max(0, int(self.db.get_setting("keep_days", str(self.KEEP_DAYS))))
        except ValueError:
            return self.KEEP_DAYS

    def set_keep_days(self, days):
        self.db.set_setting("keep_days", str(int(days)))
        self.db.set_setting("pruned_at", "0")
        self.set_status("Players are kept for good now." if not days else
                        f"Players not seen for {days} days are forgotten (tags, your unshared Twitch links and "
                        "the shared list are kept). Cleaning up now...")
        if days:
            self.root.after(500, self._prune_round)

    # ---- your crew (Microsoft sign-in): who is on your ship, crewmates tagged and hidden from the list
    def toggle_crew(self):
        self.hide_crew = not self.hide_crew
        self.db.set_setting("hide_crew", "1" if self.hide_crew else "0")
        self._crew_button()
        self.reload()
        self.set_status("Crewmates (tag 'crew') are hidden from the list." if self.hide_crew else
                        "Crewmates are shown in the list.")

    def _crew_button(self):
        self.crew_btn.set_text("Crew hidden" if self.hide_crew else "Crew shown")
        self.crew_btn.set_kind("accent" if self.hide_crew else "soft")

    fetch_crew = staticmethod(lambda key: social.my_crew(key))

    def _crew_round(self):
        """After a refresh: who is on your ship. People you sail with in 2+ sessions get the tag crew."""
        key, owner = self.xkey.get(), self.xkey.active()
        if not direct.is_ms_key(key) or getattr(self, "_crew_busy", False) or self.closing:
            return
        self._crew_busy = True

        def work():
            try:
                res, err = self.fetch_crew(key), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:                       # never let a worker thread die silently
                res, err = None, f"Unexpected error: {e}"
            self.q.put(("call", lambda: done(res, err)))

        def done(res, err):
            self._crew_busy = False
            if err or key != self.xkey.get():
                return                                   # quiet: the next refresh tries again
            self.crew_now = res
            if not res:
                self.crew_var.set("")
                return self.crew_line.pack_forget()
            names = [m["gamertag"] for m in res["members"]]
            self.crew_var.set(f"⚓  Your {res['ship']}: " + (", ".join(names) if names else "just you"))
            self.crew_line.pack(fill="x", padx=18, pady=(10, 0), before=self.search_box.master)
            newly = self.db.note_crew(owner, res["session"], res["members"])
            if newly:
                self.reload()
                self.set_status(f"Crewmate(s) found: {', '.join(newly)} (tagged crew"
                                + (", hidden from the list" if self.hide_crew else "") + "; untag to show again).")
        threading.Thread(target=work, daemon=True).start()

    # ---- Discord: the list in a channel through a webhook
    fetch_discord = staticmethod(lambda cfg, account, players, force: discord.post_list(cfg, account, players, force))

    def _discord_round(self, force=False, said=None):
        """Post (or update) the list in Discord: after a refresh when that's switched on, or on Post now."""
        cfg = self.discord_cfg
        if not cfg["url"] or (not force and not cfg["auto"]) or getattr(self, "_discord_busy", False):
            return
        account = self.xkey.active() or ""
        players = report.recent(self.db, account, limit=int(cfg["limit"]))
        self._discord_busy = True

        def work():
            try:
                res, err = self.fetch_discord(cfg, account, players, force), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:                       # never let a worker thread die silently
                res, err = None, f"Unexpected error: {e}"
            self.q.put(("call", lambda: done(res, err)))

        def done(res, err):
            self._discord_busy = False
            text = (f"Discord: {err}" if err else {"sent": "Posted the list to Discord.",
                                                    "edited": "Updated the list in Discord.",
                                                    "unchanged": "Discord: the list hasn't changed."}[res])
            if err or force:
                self.set_status(text, error=bool(err))
            if said is not None:
                try:
                    said.set(text)
                except tk.TclError:                      # the Discord tab was closed meanwhile
                    pass
        threading.Thread(target=work, daemon=True).start()

    def discord_dialog(self):
        """The Discord tab: the webhook address, when to post, and Post now."""
        P, cfg = self.pal, self.discord_cfg
        win = self._page("discord", "Discord", fresh=True)
        box = tk.Frame(win, bg=P["bg"])
        box.pack(fill="x", padx=24, pady=20)
        tk.Label(box, text="Your list in Discord", bg=P["bg"], fg=P["fg"], font=("Segoe UI", 15, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(box, text="Shows your recently-played list in a Discord channel: each player with a SOTREP link, "
                 "hours, and their Twitch channel (live or not). In Discord: Server settings → Integrations → "
                 "Webhooks → New webhook → Copy webhook URL, then paste it here. The address is saved only on this "
                 "PC; anyone who has it can post in that channel.", bg=P["bg"], fg=P["muted"], font=("Segoe UI", 9),
                 wraplength=640, justify="left", anchor="w").pack(fill="x", pady=(4, 14))
        url = tk.StringVar(value=cfg["url"])

        def make(p, bg):
            return tk.Entry(p, textvariable=url, bg=bg, fg=P["fg"], insertbackground=P["fg"], relief="flat", bd=0,
                            font=("Segoe UI", 10), highlightthickness=0, show="•" if cfg["url"] else "")
        field = chat_ui.RoundBox(box, P, P["bg"], make, height=38, radius=18)
        field.pack(fill="x")
        said = tk.StringVar(value="Webhook saved." if cfg["url"] else "No webhook yet.")
        row = chat_ui.Flow(box, P["bg"])
        row.pack(fill="x", pady=(12, 0))

        def save():
            try:
                cfg.update(url=discord.check_url(url.get()))
            except ProviderError as e:
                return said.set(str(e))
            field.inner.configure(show="•")
            said.set("Webhook saved. Post now to see it in your channel.")

        def remove():
            cfg.update(url="", auto=False)
            url.set("")
            field.inner.configure(show="")
            auto.set(False)
            said.set("Webhook removed: nothing is posted any more.")
        row.add(chat_ui.RoundButton(row, "Save", save, P, P["bg"], kind="accent", padx=18, pady=7))
        row.add(chat_ui.RoundButton(row, "Post now", lambda: self._discord_round(force=True, said=said), P, P["bg"],
                                    padx=16, pady=7))
        row.add(chat_ui.RoundButton(row, "Remove webhook", remove, P, P["bg"], padx=16, pady=7), right=True)
        opts = tk.Frame(box, bg=P["bg"])
        opts.pack(fill="x", pady=(16, 0))
        auto, keep = tk.BooleanVar(value=cfg["auto"]), tk.BooleanVar(value=cfg["keep_one"])
        ttk.Checkbutton(opts, text="Update Discord after every refresh (only when the list changed)", variable=auto,
                        command=lambda: cfg.update(auto=auto.get())).pack(anchor="w")
        ttk.Checkbutton(opts, text="Keep one message up to date (off: a new message each time)", variable=keep,
                        command=lambda: cfg.update(keep_one=keep.get(), message_id="")).pack(anchor="w", pady=(4, 0))
        lim = tk.StringVar(value=f"{cfg['limit']} players")
        lrow = tk.Frame(opts, bg=P["bg"])
        lrow.pack(anchor="w", pady=(8, 0))
        tk.Label(lrow, text="Show", bg=P["bg"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        chat_ui.PillSelect(lrow, P, P["bg"], lim, values=[f"{n} players" for n in (5, 10, 15, 20, 30)],
                           on_change=lambda: cfg.update(limit=int(lim.get().split()[0])), pady=5).pack(side="left")
        tk.Label(box, textvariable=said, bg=P["bg"], fg=P["fg"], font=("Segoe UI", 9), anchor="w", wraplength=640,
                 justify="left").pack(fill="x", pady=(14, 0))
        win.url, win.save, win.remove, win.said, win.auto = url, save, remove, said, auto        # for tests
        return win

    def _prune_round(self):
        """Once a day (and at start): forget old players and unused pictures, so the data stays small."""
        if self.closing:
            return
        self.root.after(6 * 3600 * 1000, self._prune_round)
        days = self.keep_days()
        now = int(time.time())
        if not days or now - int(self.db.get_setting("pruned_at", "0") or 0) < 86400:
            return
        self.db.set_setting("pruned_at", str(now))
        try:
            gone = self.db.prune(days, now)
            pics = self.pictures.prune(days, now)
        except Exception as e:                           # cleaning up must never break the app
            return self.set_status(f"Cleaning up old players failed: {e}", error=True)
        if gone:
            self.reload()
            self.set_status(f"Forgot {gone} player(s) not seen for {days} days"
                            + (f" and {pics} unused picture(s)" if pics else "")
                            + ". Tagged players, your unshared Twitch links and the shared list are kept.")

    def _main_menu(self):
        """☰: accounts, import/export, Twitch and shared list, theme and updates."""
        m = self._menu()
        has = "normal" if self.xkey.active() else "disabled"
        m.add_command(label="Add Xbox account...", command=self.set_key)
        m.add_command(label="Remove this account", command=self.remove_account, state=has)
        if sys.platform == "win32":                       # Game Bar and the Xbox app exist only on Windows
            m.add_command(label="Switch Game Bar to this account", command=self.switch_game_bar, state=has)
        m.add_checkbutton(label="Pop-up for new Xbox messages", variable=self.msg_popups,
                          command=self.set_message_popups)
        m.add_separator()
        m.add_command(label="Import players...", command=self.import_dialog)
        m.add_command(label="Export...", command=self.export)
        m.add_separator()
        m.add_command(label="Shared Twitch list...", command=self.shared_dialog)
        m.add_command(label="Twitch setup...", command=self.twitch_setup_dialog)
        m.add_command(label="Discord...", command=self.discord_dialog)
        m.add_separator()
        themes = self._menu(m)
        self.theme_choice = tk.IntVar(value=self.theme)
        for i, t in enumerate(THEMES, 1):
            themes.add_radiobutton(label=t[0], value=i, variable=self.theme_choice,
                                   command=lambda i=i: self.set_theme(i))
        m.add_cascade(label="Theme", menu=themes)
        keep = self._menu(m)
        self.keep_choice = tk.IntVar(value=self.keep_days())
        for d, label in ((7, "1 week"), (14, "2 weeks"), (30, "1 month"), (90, "3 months"), (0, "Never")):
            keep.add_radiobutton(label=label, value=d, variable=self.keep_choice,
                                 command=lambda d=d: self.set_keep_days(d))
        m.add_cascade(label="Forget players not seen for", menu=keep)
        views = self._menu(m)
        for key, label in self.VIEW_NAMES.items():
            views.add_radiobutton(label=label, value=label, variable=self.view_var,
                                  command=lambda key=key: self.set_list_view(key))
        views.add_separator()
        self.bars_var = tk.BooleanVar(value=self.bars_shown)
        views.add_checkbutton(label="Show search and sort", variable=self.bars_var,
                              command=lambda: self.show_bars(self.bars_var.get()))
        m.add_cascade(label="List view", menu=views)
        upd = self._menu(m)
        upd.add_command(label="Check now", command=lambda: threading.Thread(
            target=self._update_check, args=(True,), daemon=True).start())
        upd.add_checkbutton(label="Check at startup", variable=self.auto_upd, command=lambda: self.db.set_setting(
            "auto_update_check", "1" if self.auto_upd.get() else "0"))
        m.add_cascade(label="Updates", menu=upd)
        m.add_separator()
        m.add_command(label=f"Version {__version__}", state="disabled")
        self.main_menu = m
        b = self.menu_btn
        m.tk_popup(b.winfo_rootx() + b.winfo_width() - 230, b.winfo_rooty() + b.winfo_height())

    def _save_interval(self):
        try:
            v = max(MIN_INTERVAL, int(self.interval.get()))
        except (tk.TclError, ValueError):
            v = 60
        self.interval.set(v)
        self.db.set_setting("interval", str(v))
        self._schedule()

    # ---- actions ----
    def open_selected(self):
        for i in self.tree.selection():
            webbrowser.open(sotrep.profile_url(self.tree.set(i, "gamertag")))

    # ---- Xbox friends and messages (OpenXBL, only when the user clicks) ----
    FOLLOWING = ("Friend", "You follow", "In your list")

    def _xbox_bg(self, work, done):
        """Run work(key) off the UI thread; done(result, error) runs on the UI thread."""
        key = self.xkey.get()
        if not key:
            return self.set_status("Add an OpenXBL account first.", error=True)
        self._social_jobs = getattr(self, "_social_jobs", 0) + 1       # several can run at once
        self._set_busy("social", True)

        def finished():
            self._social_jobs -= 1
            if self._social_jobs <= 0:
                self._social_jobs = 0
                self._set_busy("social", False)

        def run():
            try:
                res, err = work(key), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:                  # never let a worker thread die silently
                res, err = None, f"Unexpected error: {e}"
            self.q.put(("call", lambda: (finished(), done(res, err))))
        threading.Thread(target=run, daemon=True).start()

    def _one_player(self):
        """(gamertag, xuid) of the single selected player, or None."""
        sel = self.tree.selection()
        if len(sel) != 1:
            return None
        return self.tree.set(sel[0], "gamertag"), self.tree.set(sel[0], "xuid")

    def add_friend_selected(self, gt=None, xuid=None, after=None):
        if gt is None:
            p = self._one_player()
            if not p or not p[1]:
                return self.set_status("Select one player with an XUID first.", error=True)
            gt, xuid = p

        def done(_, err):
            if err:
                return self.set_status(f"Could not add {gt}: {err}", error=True)
            self.db.set_friend(self.xkey.active() or "", xuid, "You follow")
            self.set_status(f"Added {gt} on Xbox: you're friends once they accept (or add you back).")
            self.reload()
            if after:
                after()
        self._xbox_bg(lambda k: social.add_friend(k, xuid), done)

    def remove_friend_selected(self, gt=None, xuid=None, after=None):
        if gt is None:
            p = self._one_player()
            if not p or not p[1]:
                return self.set_status("Select one player with an XUID first.", error=True)
            gt, xuid = p
        if not self.ask_yes("Remove friend", f"Remove {gt} from your Xbox friends?", yes="Remove friend",
                            danger=True):
            return

        def done(_, err):
            if err:
                return self.set_status(f"Could not remove {gt}: {err}", error=True)
            self.db.set_friend(self.xkey.active() or "", xuid, None)
            self.set_status(f"Removed {gt} from your Xbox friends.")
            self.reload()
            if after:
                after()
        self._xbox_bg(lambda k: social.remove_friend(k, xuid), done)

    def xbox_profile_selected(self):
        """The player's profile in the Xbox app (or on xbox.com); whatever Xbox itself offers (invite, join) is there."""
        for i in self.tree.selection()[:5]:
            self.open_xbox_profile(self.tree.set(i, "gamertag"), self.tree.set(i, "xuid"))

    def switch_game_bar(self):
        """Game Bar uses the Xbox app's Windows sign-in, which only Microsoft's apps can change. Open the Xbox app,
        sign it out through its own menu and press Sign in (see xboxui.py), so Microsoft's account picker opens; the
        user picks the account there. The gamertag is copied too."""
        name = self.xkey.active()
        if not name:
            return self.set_status("Add an Xbox account first.", error=True)
        if "xboxapp" in self.busy:
            return self.set_status("Already switching the Xbox app's account...")
        self.root.clipboard_clear()
        self.root.clipboard_append(name)
        manual = (f"In the Xbox app, click your profile picture (top left) > Sign out, then sign in as {name}: "
                  "Game Bar uses the same sign-in.")
        if not social.open_xbox_app():
            return self.set_status(f"Couldn't open the Xbox app ({name} copied). {manual}", error=True)
        key = self.xkey.get() or ""
        email = self.msa.email(key.strip()[len(MARK):]) if direct.is_ms_key(key) else ""
        self.set_status(f"Switching the Xbox app to {name}... (don't use the mouse for a moment)")
        self._set_busy("xboxapp", True)

        def work():
            res = xboxui.switch_account(name, email)
            self.q.put(("call", lambda: done(res)))

        def done(res):
            self._set_busy("xboxapp", False)
            teach = ("" if email else " Tip: sign this account in again once (☰ > Add Xbox account... > Sign in with "
                     "Microsoft) and the app picks it in that window for you next time.")
            msgs = {
                xboxui.ALREADY: (f"The Xbox app (and Game Bar) is already signed in as {name}.", False),
                xboxui.PICKED: (f"Picked {name} in Microsoft's sign-in window: the Xbox app and Game Bar switch to it. "
                                "If Microsoft asks for a password, type it there.", False),
                xboxui.NOT_LISTED: (f"Microsoft's sign-in window doesn't list {name}'s email yet: choose \"Use a "
                                    "different account\" there and sign in once.", False),
                xboxui.PICKER: (f"Signed out of the Xbox app. Pick {name} in Microsoft's sign-in window "
                                f"({name} copied): Game Bar then uses it too." + teach, False),
                xboxui.SIGN_IN_SCREEN: (f"Signed out of the Xbox app. Click Sign in there and pick {name}.", False),
                xboxui.NO_WINDOW: ("The Xbox app didn't open its window (stuck on \"Launching\"? Restarting the PC "
                                   "fixes that). " + manual, True),
                xboxui.BLOCKED: ("Something covered the Xbox window, so nothing was clicked. " + manual, True),
            }
            text, err = msgs.get(res, (f"Couldn't switch the Xbox app by itself ({res}). " + manual, True))
            self.set_status(text, error=err)
        threading.Thread(target=work, daemon=True).start()

    def open_xbox_profile(self, gt, xuid):
        where = social.open_profile(gt, xuid)
        self.set_status(f"Opened {gt}'s profile " + ("in the Xbox app." if where == "app" else
                        "on xbox.com (the Xbox app isn't installed, or the player has no XUID)."))

    def message_selected(self):
        p = self._one_player()
        if not p or not p[1]:
            return self.set_status("Select one player with an XUID first.", error=True)
        self.message_dialog(*p)

    def message_dialog(self, gt, xuid):
        """Chat with one player in its own window (see _chat_panel)."""
        win = self._page(f"chat:{xuid}", f"✉  {gt}")
        win.title(f"✉  {gt}")
        win.configure(bg=self.pal["bg"])
        panel = self._chat_panel(win, gt, xuid)
        panel.pack(fill="both", expand=True)
        win.send, win.box, win.info, win.texts = panel.send, panel.box, panel.info, panel.texts   # kept for tests
        win.sotrep_btn = panel.sotrep_btn
        panel.box.focus_set()
        self._place_dialog(win, 680, 640)
        return win

    # ---- conversation actions (Microsoft sign-in), shared by the chat and the Messages list
    def _mute_chat(self, gt, conversation, mute, after=None):
        def done(_, err):
            if err:
                return self.set_status(f"Could not {'mute' if mute else 'unmute'} {gt}: {err}", error=True)
            self.set_status(f"{'Muted' if mute else 'Unmuted'} the chat with {gt} on Xbox.")
            if after:
                after()
        self._xbox_bg(lambda k: social.mute_chat(k, conversation, mute), done)

    def _delete_chat(self, parent, gt, conversation, horizon, after=None):
        """Asks inside parent (no separate window), then removes the chat from your Xbox inbox."""
        def go():
            def done(_, err):
                if err:
                    return self.set_status(f"Could not delete the chat with {gt}: {err}", error=True)
                self.set_status(f"Deleted the chat with {gt} from your Xbox inbox.")
                if after:
                    after()
            self._xbox_bg(lambda k: social.delete_chat(k, conversation, horizon), done)
        return chat_ui.confirm_card(parent, self.pal, f"Delete the chat with {gt} from your Xbox inbox? {gt} keeps "
                                    "their copy, and a new message brings the chat back.", "Delete chat", go)

    def _picture_of(self, xuid):
        """The profile picture address of a player we've seen in chats, friends or requests, else ""."""
        for res in list(self.inbox_cache.values()):
            people = (res.get("chats", []) + res.get("received", []) + res.get("sent", [])
                      if isinstance(res, dict) else res if isinstance(res, list) else [])
            for c in people:
                if c.get("xuid") == str(xuid) and c.get("pic"):
                    return c["pic"]
        return ""

    def _chat_panel(self, parent, gt, xuid, on_sent=None, on_changed=None, pic=""):
        """Your Xbox message history with one player as rounded bubbles (yours right, theirs left) and a box to
        write a new one. Nothing is sent until Send (or Ctrl+Enter). ⋯ mutes or deletes the chat; right-click a
        message to copy it or delete your own. Used by the chat window and the Messages window."""
        P = self.pal
        panel = tk.Frame(parent, bg=P["bg"])
        state_ = {"conv": "", "muted": False, "horizon": ""}
        cache_key = (self.xkey.get() or "", str(xuid))

        head = tk.Frame(panel, bg=P["head"])
        head.pack(fill="x")
        av = tk.Canvas(head, width=44, height=44, bg=P["head"], highlightthickness=0)
        av.pack(side="left", padx=(14, 10), pady=10)
        pic = pic or self._picture_of(xuid)

        def draw_av():
            if av.winfo_exists():
                av.delete("all")
                chat_ui.draw_avatar(av, 22, 22, 20, gt, xuid, P, self.pictures.get(pic, draw_av))
        draw_av()
        names = tk.Frame(head, bg=P["head"])
        names.pack(side="left", fill="y", pady=8)
        tk.Label(names, text=gt, bg=P["head"], fg=P["fg"], font=("Segoe UI", 13, "bold"), anchor="w").pack(anchor="w")
        info = tk.StringVar(value="Loading your messages...")
        tk.Label(names, textvariable=info, bg=P["head"], fg=P["muted"], font=("Segoe UI", 8), anchor="w").pack(
            anchor="w")

        def more_menu():
            m = self._menu(panel)
            ms = direct.is_ms_key(self.xkey.get())
            can = "normal" if ms and state_["conv"] else "disabled"
            m.add_command(label="Unmute chat" if state_["muted"] else "Mute chat", state=can,
                          command=lambda: self._mute_chat(gt, state_["conv"], not state_["muted"], after=changed))
            m.add_command(label="Delete chat...", state="normal" if can == "normal" and state_["horizon"] else "disabled",
                          command=lambda: self._delete_chat(panel, gt, state_["conv"], state_["horizon"],
                                                            after=changed))
            m.add_separator()
            m.add_command(label="Copy gamertag", command=lambda: (self.root.clipboard_clear(),
                                                                  self.root.clipboard_append(gt)))
            m.tk_popup(more.winfo_rootx(), more.winfo_rooty() + more.winfo_height())

        def changed():
            load()
            if on_changed:
                on_changed()
        more = chat_ui.RoundButton(head, "⋯", more_menu, P, P["head"], padx=12)
        more.pack(side="right", padx=(4, 14))
        rep = chat_ui.RoundButton(head, "SOTREP", lambda: webbrowser.open(sotrep.profile_url(gt)), P, P["head"])
        rep.pack(side="right", padx=4)
        if gt.startswith("Unknown player"):                    # no gamertag known: nothing to search on SOTREP
            rep.state(["disabled"])
        panel.sotrep_btn = rep
        chat_ui.RoundButton(head, "Xbox profile", lambda: self.open_xbox_profile(gt, xuid), P, P["head"]).pack(
            side="right", padx=4)

        def message_menu(m, e):
            menu = self._menu(panel)
            if not m.get("deleted"):
                menu.add_command(label="Copy text", command=lambda: (self.root.clipboard_clear(),
                                                                    self.root.clipboard_append(m["text"])))
            mine = m["sender"] != str(xuid)
            if mine and not m.get("deleted") and m.get("id") and state_["conv"] and direct.is_ms_key(self.xkey.get()):
                menu.add_command(label="Delete message...", command=lambda: delete_message(m))
            if menu.index("end") is not None:
                menu.tk_popup(e.x_root, e.y_root)

        def delete_message(m):
            def go():
                def done(_, err):
                    if err:
                        return self.set_status(f"Could not delete the message: {err}", error=True)
                    self.set_status("Message deleted.")
                    load()
                self._xbox_bg(lambda k: social.delete_message(k, state_["conv"], m["id"]), done)
            text = m["text"] if len(m["text"]) <= 120 else m["text"][:120] + "…"
            panel.confirm = chat_ui.confirm_card(panel, P, f"Delete this message? It shows as deleted for {gt} too.\n"
                                                 f"\u201c{text}\u201d", "Delete", go)

        view = chat_ui.ChatView(panel, P, on_menu=message_menu)
        view.pack(fill="both", expand=True)

        compose = tk.Frame(panel, bg=P["bg"])
        compose.pack(fill="x", padx=12, pady=(6, 2))
        send_btn = chat_ui.RoundButton(compose, "Send", None, P, P["bg"], kind="accent", padx=20, pady=9,
                                       font=("Segoe UI", 10, "bold"))
        send_btn.pack(side="right", padx=(8, 0))                   # packed first so the box can't squeeze it

        def make_text(parent_, bg):
            return tk.Text(parent_, height=2, wrap="word", bg=bg, fg=P["fg"], insertbackground=P["fg"],
                           relief="flat", bd=0, font=("Segoe UI", 10), highlightthickness=0)
        rbox = chat_ui.RoundBox(compose, P, P["bg"], make_text, height=58, radius=18)
        rbox.pack(side="left", fill="x", expand=True)
        box = rbox.inner
        state = tk.StringVar()
        hint = f"Ctrl+Enter sends · at most {social.MAX_MESSAGE} characters · right-click a message for more"
        tk.Label(panel, textvariable=state, bg=P["bg"], fg=P["muted"], font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=18, pady=(0, 8))
        state.set(hint)
        box.bind("<KeyRelease>", lambda e: state.set(
            f"{len(box.get('1.0', 'end').strip())} / {social.MAX_MESSAGE}  ·  Ctrl+Enter sends"))

        def show(res, err, cached=False):
            if not panel.winfo_exists():
                return
            if err:
                return info.set(f"Could not load the messages: {err}")
            if not cached:
                self.chat_cache[cache_key] = res
                self._chat_opened(xuid)
            msgs = res["messages"]
            state_.update(conv=res.get("conversation", ""), muted=bool(res.get("muted")),
                          horizon=max((m.get("id") or "" for m in msgs), key=lambda i: int(i) if i.isdigit() else 0,
                                      default=""))
            view.set(msgs, xuid, note="" if msgs else f"No messages with {gt} yet. Say ahoy!")
            n = sum(1 for m in msgs if not m.get("deleted"))
            info.set((f"{n} message(s)" if n else "no messages yet")
                     + (" · muted" if state_["muted"] else "")
                     + (" · only the newest" if res.get("more") else "")
                     + (" · updating..." if cached else f" · as {self.xkey.active() or 'your account'}"))

        def load():
            self._xbox_bg(lambda k: social.history(k, xuid), show)

        def send():
            text = box.get("1.0", "end").strip()
            send_btn.state(["disabled"])
            state.set("Sending...")

            def done(_, err):
                if not panel.winfo_exists():
                    return self.set_status(f"Message to {gt}: {'failed: ' + err if err else 'sent.'}", error=bool(err))
                send_btn.state(["!disabled"])
                if err:
                    return state.set(f"Not sent: {err}")
                box.delete("1.0", "end")
                state.set(hint)
                self.set_status(f"Xbox message sent to {gt}.")
                load()
                if on_sent:
                    on_sent()
            self._xbox_bg(lambda k: social.send_message(k, xuid, text), done)
        send_btn.command = send
        box.bind("<Control-Return>", lambda e: (send(), "break")[1])
        panel.send, panel.box, panel.info, panel.load, panel.texts = send, box, info, load, view.texts
        panel.view, panel.head = view, head
        panel.delete_message = delete_message                   # for tests
        if cache_key in self.chat_cache:                        # what we had last time, at once; then fresh
            show(self.chat_cache[cache_key], None, cached=True)
        load()
        return panel

    def friends_dialog(self, goto=None):
        """Friends & messages, on the Friends tab."""
        return self.inbox_dialog(goto=goto, tab="friends")

    def requests_dialog(self, goto=None):
        """Friends & messages, on the Requests tab."""
        return self.inbox_dialog(goto=goto, tab="requests")

    def inbox_dialog(self, goto=None, tab="friends"):
        """Friends & messages in one window. Left: tabs for your Xbox chats (newest first, unread marked), your
        friends (online first, what they are doing) and friend requests; right: the chat with whoever is selected,
        or for a request a card to accept or ignore it. Typing filters the tab; Enter finds anyone on Xbox. Right-
        click a row for more. Chats and requests need an account signed in with Microsoft (OpenXBL has neither).
        What was shown last time appears at once while a fresh copy loads."""
        if self.inbox_win is not None and self.inbox_win.winfo_exists():    # one window: bring it up
            w = self.inbox_win
            w.deiconify()
            w.lift()
            w.focus_force()
            w.show_tab(tab)
            w.goto(goto)
            return w
        P = self.pal
        win = self._page("social", self.SOCIAL, closable=False)
        self.inbox_win = win
        win.title("Friends & messages")
        win.configure(bg=P["bg"])
        key_now = self.xkey.get() or ""

        # the list and the chat side by side; drag the line between them for a slim or a wide list (remembered)
        body = tk.PanedWindow(win, orient="horizontal", bg=chat_ui._mix(P["panel"], P["fg"], 0.08), bd=0,
                              sashwidth=5, sashrelief="flat", showhandle=False, opaqueresize=True, sashcursor="sb_h_double_arrow")
        body.pack(fill="both", expand=True)
        try:
            left_w = max(260, min(700, int(self.db.get_setting("social_list_width", "350"))))
        except ValueError:
            left_w = 350
        left = tk.Frame(body, bg=P["panel"], width=left_w)
        body.add(left, minsize=260, width=left_w, stretch="never")

        def remember_width(_=None):
            try:
                self.db.set_setting("social_list_width", str(int(left.winfo_width())))
            except (tk.TclError, ValueError):
                pass
        body.bind("<ButtonRelease-1>", lambda e: body.after(50, remember_width), add="+")
        top = tk.Frame(left, bg=P["panel"])
        top.pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(top, text="Friends & messages", bg=P["panel"], fg=P["fg"], font=("Segoe UI", 14, "bold")).pack(
            side="left")
        chat_ui.RoundButton(top, "↻", lambda: load_all(), P, P["panel"], padx=11).pack(side="right")
        tabs = chat_ui.Tabs(left, P, P["panel"], ("chats", "friends", "requests"), lambda t: switch(t))
        tabs.pack(fill="x", padx=12, pady=(10, 0))
        info = tk.StringVar(value="Loading...")
        tk.Label(left, textvariable=info, bg=P["panel"], fg=P["muted"], font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=16, pady=(8, 0))
        query = tk.StringVar()

        def make_entry(parent_, bg):
            return tk.Entry(parent_, textvariable=query, bg=bg, fg=P["fg"], insertbackground=P["fg"], relief="flat",
                            bd=0, font=("Segoe UI", 10), highlightthickness=0)
        sbox = chat_ui.RoundBox(left, P, P["panel"], make_entry, height=38, radius=19)
        sbox.pack(fill="x", padx=12, pady=(8, 8))
        entry = sbox.inner

        data = {"chats": [], "friends": [], "received": [], "sent": [], "groups": 0, "ships": {}}
        errs = {"chats": None, "friends": None, "requests": None}
        loaded = set()
        state = {"tab": tab if tab in ("chats", "friends", "requests") else "chats", "results": None, "term": "",
                 "placeholder": False, "panel": None, "open": None, "goto": goto}
        hints = {"chats": "Search chats, or Enter to find anyone on Xbox",
                 "friends": "Search friends, or Enter to find anyone on Xbox",
                 "requests": "Search requests, or Enter to find anyone on Xbox"}

        def name_of(r):
            return r["gamertag"] if not str(r["gamertag"]).isdigit() else "this player"

        def request_of(xuid):
            for side in ("received", "sent"):
                for f in data[side]:
                    if f["xuid"] == xuid:
                        return f, side
            return None, None

        def friend_of(xuid):
            return next((f for f in data["friends"] if f["xuid"] == xuid), None)

        def presence(f):
            ship = data["ships"].get(f["xuid"])
            if ship and f["state"].casefold() == "online":
                aboard = [m["gamertag"] for m in ship.get("crew") or [] if m["xuid"] != f["xuid"]]
                return ("⚓ " + social.ship_label(ship) + (" · with " + ", ".join(aboard) if aboard else "")
                        + (f" · {f['text']}" if f["text"] and not aboard else ""))
            if f["state"].casefold() == "online":
                return f["text"] or "Online"
            if f.get("last_seen"):
                return "last seen " + chat_ui.ago_label(f["last_seen"])
            return f["text"] or ("" if f["state"] in ("Unknown", "Offline") else f["state"])

        # ---- right-click menus
        def row_menu(r, e):
            m = self._menu(win)
            name = name_of(r)
            req, side = request_of(r["xuid"]) if state["tab"] == "requests" else (None, None)
            if side == "received":
                m.add_command(label="Accept friend request", command=lambda: answer(req, True))
                m.add_command(label="Ignore request", command=lambda: answer(req, False))
                m.add_separator()
            elif side == "sent":
                m.add_command(label="Cancel friend request...", command=lambda: cancel_request(req))
                m.add_separator()
            if state["tab"] == "chats" and r.get("conversation"):
                m.add_command(label="Unmute chat" if r.get("muted") else "Mute chat",
                              command=lambda: self._mute_chat(name, r["conversation"], not r.get("muted"),
                                                              after=load_chats))
                m.add_command(label="Delete chat...", state="normal" if r.get("horizon") else "disabled",
                              command=lambda: setattr(win, "confirm", self._delete_chat(
                                  body, name, r["conversation"], r["horizon"], after=deleted(r))))
                m.add_separator()
            if state["tab"] != "chats" or not r.get("conversation"):
                m.add_command(label="Message", command=lambda: (lst.select(r["xuid"], notify=False), open_chat(r)))
            m.add_command(label="SOTREP", command=lambda: webbrowser.open(sotrep.profile_url(r["gamertag"])),
                          state="disabled" if str(r["gamertag"]).isdigit() else "normal")
            m.add_command(label="Xbox profile", command=lambda: self.open_xbox_profile(r["gamertag"], r["xuid"]))
            f = friend_of(r["xuid"])
            if side is None:
                m.add_separator()
                if f is not None and f["relation"] in self.FOLLOWING:
                    m.add_command(label="Remove friend...", command=lambda: remove_friend(f))
                else:
                    m.add_command(label="Add friend", command=lambda: self.add_friend_selected(
                        r["gamertag"], r["xuid"], after=load_friends))
            win.row_menu_widget = m
            m.tk_popup(e.x_root, e.y_root)

        def deleted(c):
            def after():
                if state["open"] == ("chat", c["xuid"]):
                    close_panel()
                load_chats()
            return after

        def remove_friend(f):
            name = name_of(f)

            def go():
                def done(_, err):
                    if err:
                        return self.set_status(f"Could not remove {name}: {err}", error=True)
                    self.db.set_friend(self.xkey.active() or "", f["xuid"], None)
                    self.set_status(f"Removed {name} from your Xbox friends.")
                    self.reload()
                    load_friends()
                self._xbox_bg(lambda k: social.remove_friend(k, f["xuid"]), done)
            win.confirm = chat_ui.confirm_card(body, P, f"Remove {name} from your Xbox friends?", "Remove friend", go)

        # ---- friend requests
        def answer(f, accept):
            name = name_of(f)

            def done(_, err):
                if err:
                    msg = f"Could not {'accept' if accept else 'ignore'} {name}'s request: {err}"
                    self.set_status(msg, error=True)
                    if win.winfo_exists() and state["open"] == ("req", f["xuid"]) and state["panel"] is not None:
                        state["panel"].note.set(msg)
                    return
                self.set_status(f"You and {name} are now friends on Xbox." if accept
                                else f"Ignored {name}'s friend request.")
                if accept:
                    self.db.set_friend(self.xkey.active() or "", f["xuid"], "Friend")
                    self.reload()
                self._request_answered(f["xuid"])
                if not win.winfo_exists():
                    return
                data["received"] = [x for x in data["received"] if x["xuid"] != f["xuid"]]
                if state["open"] == ("req", f["xuid"]):
                    if accept:
                        lst.select(f["xuid"], notify=False)
                        open_chat(dict(f, relation="Friend"), force=True)
                    else:
                        close_panel()
                render()
                load_requests()
                if accept:
                    load_friends()
            self._xbox_bg(lambda k: (social.accept_friend if accept else social.ignore_friend)(k, f["xuid"]), done)

        def cancel_request(f):
            name = name_of(f)

            def go():
                def done(_, err):
                    if err:
                        return self.set_status(f"Could not cancel the request to {name}: {err}", error=True)
                    self.set_status(f"Cancelled your friend request to {name}.")
                    if win.winfo_exists():
                        if state["open"] == ("req", f["xuid"]):
                            close_panel()
                        load_requests()
                self._xbox_bg(lambda k: social.ignore_friend(k, f["xuid"]), done)
            win.confirm = chat_ui.confirm_card(body, P, f"Cancel your friend request to {name}?", "Cancel request", go)

        # ---- the right side: a chat, a request card, or nothing
        def close_panel():
            if state["panel"] is not None:
                state["panel"].destroy()
            state["panel"], state["open"], win.panel = None, None, None
            lst.selected = None
            empty.pack(expand=True)
            if win.winfo_exists() and hasattr(win, "show_list"):
                arrange()

        def show_panel(kind, xuid, make):
            if state["open"] == (kind, xuid) and state["panel"] is not None:
                return
            if state["panel"] is not None:
                state["panel"].destroy()
            empty.pack_forget()
            state["open"] = (kind, xuid)
            state["panel"] = make()
            state["panel"].pack(fill="both", expand=True)
            win.panel = state["panel"]
            arrange()

        def open_chat(c, force=False):
            if force:
                state["open"] = None
            name = c["gamertag"] if not str(c["gamertag"]).isdigit() else "Unknown player"
            show_panel("chat", c["xuid"], lambda: self._chat_panel(right, name, c["xuid"], on_sent=load_chats,
                                                                    on_changed=load_chats, pic=c.get("pic", "")))

        def open_request(f, side):
            show_panel("req", f["xuid"], lambda: self._request_card(
                right, f, side, accept=lambda: answer(f, True), ignore=lambda: answer(f, False),
                cancel=lambda: cancel_request(f),
                message=lambda: (state.update(open=None), open_chat(f))))

        def on_select(r):
            req, side = request_of(r["xuid"]) if state["tab"] == "requests" else (None, None)
            if req is not None:
                open_request(req, side)
            else:
                open_chat(r)

        lst = chat_ui.ConversationList(left, P, on_select=on_select, on_menu=row_menu, pictures=self.pictures)
        lst.pack(fill="both", expand=True, pady=(0, 6))
        right = tk.Frame(body, bg=P["bg"])
        body.add(right, minsize=380, stretch="always")
        win.panes = body                                         # for tests
        back = chat_ui.RoundButton(right, "‹  Back", lambda: show_list(), P, P["bg"], padx=12, pady=5)
        cols = {"one": False}

        def columns(width):
            one = width < self.ONE_COLUMN
            if one != cols["one"]:
                cols["one"] = one
                if one:
                    body.paneconfigure(left, minsize=200)
                    body.paneconfigure(right, minsize=200)
                arrange()

        def arrange():
            if not win.winfo_exists():
                return
            panes = [str(p) for p in body.panes()]
            showing_chat = state["panel"] is not None and cols["one"]
            want = [right] if showing_chat else [left] if cols["one"] else [left, right]
            if [str(w) for w in want] != panes:
                for w in (left, right):
                    if str(w) in panes:
                        body.forget(w)
                for w in want:
                    body.add(w, minsize=200 if cols["one"] else (260 if w is left else 380),
                             stretch="always" if (w is right or cols["one"]) else "never",
                             **({} if cols["one"] or w is right else {"width": left_w}))
            panel = state["panel"]
            head = getattr(panel, "head", None) if panel is not None else None
            if showing_chat and head is not None:              # a chat: Back sits in its header
                back.place_forget()
                if getattr(panel, "back", None) is None:
                    panel.back = chat_ui.RoundButton(head, "‹", show_list, P, P["head"], padx=10, pady=5)
                    panel.back.pack(side="left", padx=(10, 0), before=head.winfo_children()[0])
            elif showing_chat:                                 # a request card: Back at the top left
                back.place(x=8, y=8)
                back.tk.call("raise", back._w)
            else:
                back.place_forget()
                if panel is not None and getattr(panel, "back", None) is not None:
                    panel.back.destroy()
                    panel.back = None

        def show_list():
            close_panel()
            arrange()
        win.show_list, win.columns = show_list, columns
        self.slim_hooks.append(columns)
        empty = tk.Label(right, text="Pick a chat, a friend or a request", bg=P["bg"], fg=P["muted"],
                         font=("Segoe UI", 11))
        empty.pack(expand=True)

        # ---- what the list shows
        def typed():
            return "" if state["placeholder"] else query.get().strip()

        def matches(r, q):
            return not q or q in str(r["gamertag"]).casefold()

        def shown_rows():
            q = " ".join(typed().casefold().split())
            t, rows = state["tab"], []
            if errs[t] and t not in loaded:
                rows.append({"kind": "note", "text": "Couldn't load this: see the line above."})
            elif t not in loaded:
                rows.append({"kind": "note", "text": "Loading..."})
            elif t == "chats":
                shown = [c for c in data["chats"] if matches(c, q)]
                rows += [dict(c, kind="chat") for c in shown]
                if not data["chats"]:
                    rows.append({"kind": "note", "text": "No chats yet: Enter finds anyone on Xbox"})
                elif q and not shown and state["results"] is None:
                    rows.append({"kind": "note", "text": "no chat matches: Enter searches Xbox"})
            elif t == "friends":
                shown = [f for f in data["friends"] if matches(f, q)]
                on = [f for f in shown if f["state"].casefold() == "online"]
                off = [f for f in shown if f["state"].casefold() != "online"]
                for title, part in ((f"ONLINE — {len(on)}", on), (f"OFFLINE — {len(off)}", off)):
                    if part:
                        rows.append({"kind": "header", "text": title})
                        rows += [dict(f, kind="chat", preview=presence(f), at=None, unread=False, mine=False,
                                      aside=("★" if f["favorite"] else "")
                                      + ("" if f["relation"] == "Friend" else ("  " if f["favorite"] else "")
                                         + f["relation"].lower())) for f in part]
                if not data["friends"]:
                    rows.append({"kind": "note", "text": "No friends in your list yet"})
                elif q and not shown and state["results"] is None:
                    rows.append({"kind": "note", "text": "no friend matches: Enter searches Xbox"})
            else:
                rec = [f for f in data["received"] if matches(f, q)]
                sent = [f for f in data["sent"] if matches(f, q)]
                if rec:
                    rows.append({"kind": "header", "text": f"WANT TO BE YOUR FRIEND — {len(rec)}"})
                    rows += [dict(f, kind="chat", preview="wants to be friends · " + (presence(f) or "on Xbox"),
                                  unread=True, mine=False, aside=chat_ui.when_label(f.get("at"))) for f in rec]
                if sent:
                    rows.append({"kind": "header", "text": f"SENT BY YOU — {len(sent)}"})
                    rows += [dict(f, kind="chat", preview="waiting for an answer · " + (presence(f) or "on Xbox"),
                                  unread=False, mine=False, aside=chat_ui.when_label(f.get("at"))) for f in sent]
                if not data["received"] and not data["sent"]:
                    rows.append({"kind": "note", "text": "No friend requests right now"})
                elif q and not rec and not sent and state["results"] is None:
                    rows.append({"kind": "note", "text": "no request matches: Enter searches Xbox"})
            if state["results"] is not None:
                rows.append({"kind": "header", "text": f"ON XBOX — \"{state['term']}\""})
                have = {r["xuid"] for r in (data["chats"] if t == "chats" else data["friends"] if t == "friends"
                                            else data["received"] + data["sent"])}     # already in this tab
                extra = [dict(r, kind="chat", preview=r["relation"].lower() if r["relation"] != "Not in your list"
                              else presence(r), at=None, mine=False, unread=False) for r in state["results"]
                         if r["xuid"] not in have]
                rows += extra or [{"kind": "note", "text": "nobody else found"}]
            return rows

        def render():
            lst.selected = state["open"][1] if state["open"] else None
            lst.set(shown_rows())
            unread = sum(1 for c in data["chats"] if c.get("unread") and not c.get("muted"))
            tabs.set_label("chats", f"Chats · {unread}" if unread else "Chats")
            tabs.set_label("friends", "Friends" + (f" · {sum(1 for f in data['friends'] if f['state'].casefold() == 'online')}"
                                                   if data["friends"] else ""))
            tabs.set_label("requests", f"Requests · {len(data['received'])}" if data["received"] else "Requests")
            tabs.mark("requests", bool(data["received"]))
            summary()

        def summary():
            t = state["tab"]
            if errs[t]:
                return info.set(errs[t])
            who = f" · {self.xkey.active() or ''}"
            if t not in loaded:
                return info.set("Loading..." + who)
            if t == "chats":
                unread = sum(1 for c in data["chats"] if c.get("unread") and not c.get("muted"))
                info.set(f"{len(data['chats'])} chats" + (f" · {unread} unread" if unread else "")
                         + (f" · {data['groups']} group chat(s) not shown" if data["groups"] else "") + who)
            elif t == "friends":
                n_on = sum(1 for f in data["friends"] if f["state"].casefold() == "online")
                info.set(f"{n_on} online · {len(data['friends'])} in your list" + who)
            else:
                info.set(f"{len(data['received'])} waiting for you · {len(data['sent'])} sent" + who)

        def switch(t):
            if t == state["tab"]:
                return
            state["tab"] = t
            tabs.select(t)
            if state["placeholder"]:
                query.set(hints[t])
            render()
        win.show_tab = lambda t: switch(t) if t in hints else None

        def on_type(*_):
            if state["placeholder"]:
                return
            if state["results"] is not None and typed() != state["term"]:
                state["results"] = None
            render()
        query.trace_add("write", on_type)

        def search(_=None):
            term = typed()
            if not term:
                return
            info.set(f"Searching Xbox for \"{term}\"...")

            def found(res, err):
                if not win.winfo_exists():
                    return
                if err:
                    return info.set(f"Xbox search failed: {err}")
                state["results"], state["term"] = res, term
                render()
            self._xbox_bg(lambda k: social.search_players(k, term), found)
        entry.bind("<Return>", search)
        entry.bind("<Escape>", lambda e: query.set(""))

        def placeholder(on):                                   # grey hint text while the box is empty
            if on and not query.get():
                state["placeholder"] = True
                entry.configure(fg=P["muted"])
                query.set(hints[state["tab"]])
            elif not on and state["placeholder"]:
                query.set("")
                state["placeholder"] = False
                entry.configure(fg=P["fg"])
        entry.bind("<FocusIn>", lambda e: placeholder(False), add="+")
        entry.bind("<FocusOut>", lambda e: placeholder(True), add="+")

        # ---- loading (each part on its own, so one failing doesn't hide the others)
        def got(part, apply, cache=True):
            def done(res, err, cached=False):
                if not win.winfo_exists():
                    return
                if err:
                    errs[part] = err
                    return render()
                errs[part] = None
                if cache and not cached:
                    self.inbox_cache[(key_now, part)] = res
                apply(res)
                loaded.add(part)
                render()
                if state["goto"] and not cached and part == state["tab"]:     # opened from a pop-up: show that one
                    xuid, state["goto"] = state["goto"], None
                    lst.select(xuid)
            return done

        def apply_chats(res):
            data["chats"], data["groups"] = res["chats"], res["groups"]
            self.inbox_cache[key_now] = res                    # for _picture_of

        def apply_friends(res):
            data["friends"] = res
            owner = self.xkey.active()
            if owner:
                self.db.replace_friends(owner, {f["xuid"]: f["relation"] for f in res})
                self.reload()

        def apply_requests(res):
            data["received"], data["sent"] = res["received"], res["sent"]
            self._requests_seen(res["received"])

        show_chats, show_friends, show_requests = (got("chats", apply_chats), got("friends", apply_friends),
                                                   got("requests", apply_requests))

        def load_chats():
            self._xbox_bg(social.inbox, show_chats)

        def load_friends():
            self._xbox_bg(social.friend_list, show_friends)
            if direct.is_ms_key(self.xkey.get()):
                self._xbox_bg(social.ships, got_ships)

        def got_ships(res, err):
            if win.winfo_exists() and not err:                # quiet when it fails: the list still works
                data["ships"] = res
                render()

        def load_requests():
            self._xbox_bg(social.friend_requests, show_requests)

        def load_all():
            load_chats()
            load_friends()
            load_requests()

        def goto_one(xuid):
            state["goto"] = xuid
            {"chats": load_chats, "friends": load_friends, "requests": load_requests}[state["tab"]]()
        win.goto = goto_one
        tabs.select(state["tab"])
        placeholder(True)
        win.info, win.query, win.search, win.entry, win.list, win.tabs = info, query, search, entry, lst, tabs
        win.row_menu, win.confirm, win.answer = row_menu, None, answer          # for tests

        def rows():
            out = []
            for r in lst.rows:
                if r.get("kind", "chat") == "chat":
                    name = r["gamertag"] if not str(r["gamertag"]).isdigit() else f"Unknown player ({r['gamertag']})"
                    out.append((name + ("  · muted" if r.get("muted") else ""),
                                r["aside"] if "aside" in r else chat_ui.when_label(r.get("at"))))
                else:
                    out.append((r["text"],))
            return out
        win.rows = rows
        win.open = lambda gt: [lst.select(r["xuid"]) for r in list(lst.rows)
                               if r.get("kind", "chat") == "chat" and r["gamertag"] == gt][:1]
        win.panel = None
        self._place_dialog(win, 1080, 720)
        columns(self.root.winfo_width())
        for part, show in (("chats", show_chats), ("friends", show_friends), ("requests", show_requests)):
            if (key_now, part) in self.inbox_cache:             # the last copy at once; then the fresh one
                show(self.inbox_cache[(key_now, part)], None, cached=True)
        render()
        load_all()
        return win

    def _request_card(self, parent, f, side, accept, ignore, cancel, message):
        """The right side for a friend request: who it is, what they are doing, and Accept / Ignore (or Cancel for
        one you sent). Nothing happens until a button is clicked."""
        P = self.pal
        card = tk.Frame(parent, bg=P["bg"])
        box = tk.Frame(card, bg=P["bg"])
        box.place(relx=0.5, rely=0.42, anchor="center")
        name = f["gamertag"] if not str(f["gamertag"]).isdigit() else "Unknown player"
        av = tk.Canvas(box, width=84, height=84, bg=P["bg"], highlightthickness=0)
        av.pack()

        def draw_av():
            if av.winfo_exists():
                av.delete("all")
                chat_ui.draw_avatar(av, 42, 42, 36, name, f["xuid"], P, self.pictures.get(f.get("pic"), draw_av, size=64))
                if f["state"].casefold() == "online":
                    av.create_oval(60, 60, 76, 76, fill="#4cc764", outline=P["bg"], width=3)
        draw_av()
        tk.Label(box, text=name, bg=P["bg"], fg=P["fg"], font=("Segoe UI", 17, "bold")).pack(pady=(10, 0))
        what = ("wants to be your friend on Xbox" if side == "received" else "hasn't answered your friend request yet")
        when = chat_ui.ago_label(f["at"]) if f.get("at") else ""
        tk.Label(box, text=what + (f" · {when}" if when else ""), bg=P["bg"], fg=P["muted"],
                 font=("Segoe UI", 10)).pack(pady=(2, 0))
        doing = (f["text"] or "Online") if f["state"].casefold() == "online" else (
            "last seen " + chat_ui.ago_label(f["last_seen"]) if f.get("last_seen") else f["text"])
        if doing:
            tk.Label(box, text=("●  " if f["state"].casefold() == "online" else "") + doing, bg=P["bg"],
                     fg="#4cc764" if f["state"].casefold() == "online" else P["muted"],
                     font=("Segoe UI", 9)).pack(pady=(6, 0))
        row = tk.Frame(box, bg=P["bg"])
        row.pack(pady=(18, 0))
        big = dict(font=("Segoe UI", 10, "bold"), padx=22, pady=9)
        if side == "received":
            card.accept_btn = chat_ui.RoundButton(row, "Accept", accept, P, P["bg"], kind="accent", **big)
            card.accept_btn.pack(side="left", padx=5)
            card.ignore_btn = chat_ui.RoundButton(row, "Ignore", ignore, P, P["bg"], **big)
            card.ignore_btn.pack(side="left", padx=5)
        else:
            card.cancel_btn = chat_ui.RoundButton(row, "Cancel request", cancel, P, P["bg"], **big)
            card.cancel_btn.pack(side="left", padx=5)
        more = tk.Frame(box, bg=P["bg"])
        more.pack(pady=(12, 0))
        for text, fn in (("Message", message), ("Xbox profile", lambda: self.open_xbox_profile(f["gamertag"], f["xuid"])),
                         ("SOTREP", lambda: webbrowser.open(sotrep.profile_url(f["gamertag"])))):
            chat_ui.RoundButton(more, text, fn, P, P["bg"]).pack(side="left", padx=3)
        card.note = tk.StringVar()
        tk.Label(box, textvariable=card.note, bg=P["bg"], fg=P["muted"], font=("Segoe UI", 9), wraplength=420).pack(
            pady=(12, 0))
        return card

    # ---- tabs and pages (see _build)
    MAIN_TABS = (("players", "Players"), ("social", "Friends & messages"), ("streams", "Live streams"),
                 ("live", "Live now"))

    def _tab_clicked(self, key):
        if key == "players" or key in self.pages:
            return self._show_page(key)
        if key == "social":
            return self._open_social()
        if key == "streams":                     # the last list at once (no new request); Refresh asks again
            return self._on_sot_streams(self.last_streams, None) if self.last_streams is not None \
                else self.sot_streams()
        if key == "live":
            return self._show_live_results(*self.last_live) if self.last_live is not None else self.check_live_all()

    # ---- questions inside the window (no message boxes)
    def _question(self, title, text, buttons, entry=None):
        """Ask in the open tab and wait for the answer (the window keeps working meanwhile). Closing the tab or
        Escape gives the first button's value."""
        parent = self.pages.get(self.page_now) if self.page_now in self.pages else self.players_tab
        if not parent.winfo_ismapped():
            parent = self.pages_host
        got = {}
        var = tk.StringVar(master=self.root)
        card = chat_ui.question_card(parent, self.pal, title, text, buttons,
                                     lambda v: (got.setdefault("v", v), var.set("1")), entry=entry)
        card.bind("<Destroy>", lambda e: var.set("1") if e.widget is card else None, add="+")
        self.question = card
        self.root.wait_variable(var)
        self.question = None
        v = got.get("v", (buttons[0][1], "") if entry is not None else buttons[0][1])
        return v

    def ask_yes(self, title, text, yes="Yes", no="Cancel", danger=False) -> bool:
        return self._question(title, text, [(no, False, "soft"), (yes, True, "danger" if danger else "accent")]) is True

    def _loading_page(self, key, text):
        p = self._page(key, dict(self.MAIN_TABS)[key], closable=False, fresh=True)
        tk.Label(p, text=text, bg=self.pal["bg"], fg=self.pal["muted"], font=("Segoe UI", 11)).pack(expand=True)

    SLIM = 900                     # narrower than this: the slim layout
    ONE_COLUMN = 760               # narrower than this: Friends & messages shows the list or the chat, not both
    SHORT_TABS = {"players": "Players", "social": "Friends", "streams": "Streams", "live": "Live"}

    def _resized(self):
        width = self.root.winfo_width()
        if width < 100:                                    # not laid out yet
            return
        slim = width < self.SLIM
        if slim != self.slim:
            self.slim = slim
            for key, label in self.MAIN_TABS:
                if key != "social":
                    self.tabstrip.set_label(key, self.SHORT_TABS[key] if slim else label)
            self.msg_btn.base = self.SHORT_TABS["social"] if slim else self.SOCIAL
            self._update_msg_button()
            for w in self.status_extras:
                if slim:
                    w.pack_forget()
                else:
                    w.pack(side="right", padx=(0, 0 if w is self.count_lbl else 10), before=self.status_lbl)
        for hook in list(self.slim_hooks):
            try:
                hook(width)
            except tk.TclError:
                self.slim_hooks.remove(hook)

    def _tab_close(self, key):
        p = self.pages.get(key)
        if p is not None and p.winfo_exists():
            p.close()

    def _show_page(self, key):
        new = self.players_tab if key == "players" else self.pages.get(key)
        if new is None or not new.winfo_exists():
            return
        if self.page_now != key:
            cur = self.players_tab if self.page_now == "players" else self.pages.get(self.page_now)
            if cur is not None and cur.winfo_exists():
                cur.pack_forget()
            new.pack(fill="both", expand=True)
            self.page_now = key
        self.page_trail = [k for k in getattr(self, "page_trail", []) if k != key][-20:] + [key]
        self.tabstrip.select(key)

    def _page(self, key, title, closable=True, fresh=False):
        """The page for key inside this window (made and shown as a tab, or the existing one brought up; fresh=True
        empties it first, for pages that are rebuilt with new data)."""
        p = self.pages.get(key)
        if p is None or not p.winfo_exists():
            p = chat_ui.Page(self.pages_host, key, self.pal["bg"], self._page_title, self._show_page, self._page_gone)
            self.pages[key] = p
            self.tabstrip.add(key, title, closable)
        elif fresh:
            for w in p.winfo_children():
                w.destroy()
        p.title(title)
        self._show_page(key)
        return p

    def _page_title(self, key, title):
        if key not in dict(self.MAIN_TABS):          # the main tabs keep their own names
            self.tabstrip.set_label(key, title if len(title) <= 32 else title[:31] + "…")

    def _page_gone(self, key, page):
        if self.pages.get(key) is page:
            del self.pages[key]
        if self.closing or not self.tabstrip.winfo_exists():   # the window is closing: nothing to update
            return
        if key not in dict(self.MAIN_TABS):
            self.tabstrip.remove(key)
        self.page_trail = [k for k in getattr(self, "page_trail", []) if k != key]
        if self.page_now == key:
            self.page_now = None
            if not self.closing:                       # back to the tab you came from
                back = next((k for k in reversed(self.page_trail) if k == "players" or k in self.pages), "players")
                self._show_page(back)

    def _place_dialog(self, win, width=None, height=None):
        """Show a page's tab (pages live in this window); a real window is centred over the main one."""
        if isinstance(win, chat_ui.Page):
            return win.lift()
        win.update_idletasks()
        w, h = width or win.winfo_reqwidth(), height or win.winfo_reqheight()
        r = self.root
        x = r.winfo_rootx() + max(0, (r.winfo_width() - w) // 2)
        y = r.winfo_rooty() + max(0, (r.winfo_height() - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.lift()
        win.focus_force()

    def _ask_tag(self, title, prompt, values, editable):
        win = self._page("ask", title)
        win.title(title)
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        ttk.Label(win, text=prompt).pack(padx=14, pady=(14, 6))
        var = tk.StringVar()
        box = ttk.Combobox(win, textvariable=var, values=values, width=32, state="normal" if editable else "readonly")
        box.pack(padx=14)
        box.focus_set()
        result = {"v": None}

        def ok(_e=None):
            result["v"] = var.get().strip()
            win.destroy()

        row = ttk.Frame(win)
        row.pack(pady=12)
        ttk.Button(row, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())
        self._place_dialog(win)
        self.root.wait_window(win)
        return result["v"]

    def tag_selected(self):
        ids = self._selected_ids()
        if not ids:
            return self.set_status("Tag: select one or more players first.", error=True)
        tag = self._ask_tag("Tag players", f"Tag {len(ids)} selected player(s) with (pick one or type a new tag):",
                            self.db.all_tags(), True)
        if tag:
            self.db.add_tag(ids, tag)
            self.reload()

    def untag_selected(self):
        ids = self._selected_ids()
        have = self.db.tags_for(ids)
        if not ids or not have:
            return self.set_status("Untag: select players that have a tag first.", error=True)
        tag = self._ask_tag("Remove tag", "Remove which tag from the selected player(s)?", have, False)
        if tag:
            self.db.remove_tag(ids, tag)
            self.reload()

    def encounters_dialog(self):
        ids = self._selected_ids()
        if not ids:
            return self.set_status("Encounters: select one or more players first.", error=True)
        owner = self.xkey.active()
        details = self.db.account_details(ids, owner)
        rows = self.db.encounters_for_many(ids)
        win = self._page("encounters", "Encounters", fresh=True)
        win.title(f"Encounters ({len(ids)})")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=820, justify="left", text=(
            "Xbox does not provide encounter times. 'First/last seen' is when this app saw the player in Xbox's "
            "recent list, and an encounter is recorded when a player appears in the list of the account you sync "
            "with (not on every refresh). 'Last online' comes from Xbox presence and only exists for players the "
            "Twitch/presence check has already looked at.")).pack(fill="x", padx=10, pady=(10, 4))
        cols = ("player", "friend", "tags", "first", "last", "times", "pos", "online")
        sm = ttk.Treeview(win, columns=cols, show="headings", height=min(max(len(details), 1), 6))
        for c, t, w in (("player", "Player", 160), ("friend", "Friend", 60), ("tags", "Tags", 90),
                        ("first", "First seen", 110), ("last", "Last seen", 110), ("times", "Times", 55),
                        ("pos", "Xbox #", 55), ("online", "Last online (Xbox)", 200)):
            sm.heading(c, text=t)
            sm.column(c, width=w, anchor="w")
        sm.tag_configure("friend", background=self.pal["friend"])
        for d in details:
            if d["state"] is None:
                online = "not checked yet"
            elif str(d["state"]).casefold() == "online":
                online = "online now" + (f": {d['title']}" if d["title"] else "")
            else:
                online = (fmt_ts(d["seen_at"]) if d["seen_at"] else "offline") + (f" ({d['title']})" if d["title"] else "")
            name = d["gamertag"] + (f" (was: {d['old_names']})" if d["old_names"] else "")
            sm.insert("", "end", tags=("friend",) if d["relation"] in ("Friend", "In your list") else (), values=(
                name, d["relation"] or "", d["tags"] or "", fmt_ts(d["first_seen"]), fmt_ts(d["last_seen"]),
                d["times"], d["pos"] + 1 if d["pos"] is not None else "", online))
        sm.pack(fill="x", padx=10, pady=4)
        ttk.Label(win, text="Recorded encounters", style="Hint.TLabel").pack(anchor="w", padx=10, pady=(6, 0))
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        tv = ttk.Treeview(frame, columns=("player", "when", "source"), show="headings")
        for c, t, w in (("player", "Player", 220), ("when", "When (seen by app)", 170), ("source", "Source", 90)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)
        tv.tag_configure("odd", background=self.pal["stripe"])
        for n, r in enumerate(rows):
            tv.insert("", "end", tags=("odd",) if n % 2 else (), values=(
                r["gamertag"], fmt_ts(r["observed_at"]), "Live" if r["source"] in ("xbox-live", "openxbl") else "Import"))
        win.summary_tv, win.log_tv = sm, tv          # kept for tests
        self._place_dialog(win, 860, 470)

    # ---- optional Twitch section ----
    def twitch_setup_dialog(self):
        win = self._page("twitch_setup", "Twitch setup", fresh=True)
        win.title("Twitch setup (optional)")
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        self._twitch_win = win
        creds = self.tcreds.load()
        ttk.Label(win, style="Hint.TLabel", wraplength=520, justify="left", text=(
            "Optional. With your own free Twitch application the app can check whether players you linked to a "
            "Twitch channel are live, with title, game and viewers.\n\n"
            "1. Open the Twitch developer console, choose 'Register Your Application'.\n"
            "2. Any name, OAuth Redirect URL http://localhost, category 'Other', client type 'Confidential'.\n"
            "3. Copy the Client ID and create a New Secret, then paste both here.\n\n"
            "They are stored only on this PC (in your app-data folder), never in the repository or the program file.")
        ).pack(fill="x", padx=14, pady=(14, 6))
        ttk.Button(win, text="Open Twitch developer console", command=lambda: webbrowser.open(CONSOLE_URL)).pack(pady=4)
        form = ttk.Frame(win)
        form.pack(padx=14, pady=6)
        ttk.Label(form, text="Client ID").grid(row=0, column=0, sticky="w", pady=3)
        cid_var, sec_var = tk.StringVar(), tk.StringVar()
        cid_entry = ttk.Entry(form, textvariable=cid_var, width=44)
        cid_entry.grid(row=0, column=1, padx=8)
        ttk.Label(form, text="Client secret").grid(row=1, column=0, sticky="w", pady=3)
        sec_entry = ttk.Entry(form, textvariable=sec_var, width=44, show="*")
        sec_entry.grid(row=1, column=1, padx=8)
        state = tk.StringVar(value=(f"Saved on this PC (client ID {creds[0][:6]}...). Paste new values to replace them."
                                    if creds else "Not set up yet."))
        status = ttk.Label(win, textvariable=state, style="Hint.TLabel", wraplength=520, justify="left")
        status.pack(fill="x", padx=14, pady=4)
        row = ttk.Frame(win)
        row.pack(pady=(4, 14))

        def save():
            cid, sec = cid_var.get().strip(), sec_var.get().strip()
            if not cid or not sec:
                return state.set("Paste both the client ID and the client secret.")
            state.set("Testing with Twitch...")
            self._set_busy("twitchsetup", True)

            def work():
                try:
                    probe = TwitchClient(_MemCreds(cid, sec), http=self.tclient.http)
                    probe.token(force=True)
                    self.q.put(("twitch_setup", (cid, sec, None)))
                except ProviderError as e:
                    self.q.put(("twitch_setup", (cid, sec, str(e))))
                except Exception as e:
                    self.q.put(("twitch_setup", (cid, sec, f"Unexpected error: {e}")))

            threading.Thread(target=work, daemon=True).start()

        def remove():
            self.tcreds.clear()
            self.tclient._token_value = None
            state.set("Keys removed from this PC.")
            self.reload()

        ttk.Button(row, text="Save and test", command=save).pack(side="left", padx=4)
        ttk.Button(row, text="Remove keys", command=remove).pack(side="left", padx=4)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=4)
        win.state_var = state                        # kept for tests and for showing results
        cid_entry.focus_set()
        self._place_dialog(win, 560, 470)

    def _on_twitch_setup(self, cid, secret, err):
        self._set_busy("twitchsetup", False)
        win = getattr(self, "_twitch_win", None)
        alive = win is not None and win.winfo_exists()
        if err:
            if alive:
                win.state_var.set(f"Not saved: {err}")
            return self.set_status(f"Twitch setup failed: {err}", error=True)
        self.tcreds.save(cid, secret)
        self.tclient._token_value = None
        if alive:
            win.destroy()
        self.set_status("Twitch keys saved on this PC. Right-click a player > Link Twitch channel... to link one.")
        self.twitch_check_now(manual=True)

    def link_twitch_selected(self):
        ids = self._selected_ids()
        if len(ids) != 1:
            return self.set_status("Select exactly one player to link a Twitch channel.", error=True)
        if not self.tcreds.configured():
            self.set_status("Twitch is not set up yet: set it up first, then link the channel.", error=True)
            return self.twitch_setup_dialog()
        aid = ids[0]
        gamertag = self.tree.set(str(aid), "gamertag")
        if self.db.twitch_never([aid]):
            if not self.ask_yes("Never link", f"{gamertag} is marked 'never link'. Remove that mark?",
                                yes="Remove mark"):
                return
            self.db.clear_twitch_never([aid])
            self.reload()
        cur = self.db.twitch_links([aid])
        name = self._ask_tag(
            "Link Twitch", f"Twitch channel for {gamertag}\n(name, @name or twitch.tv link; empty removes the link)"
            + (f"\nCurrently: {cur[0]['display_name']}" + (" (found automatically)" if cur[0]["source"] == "auto"
                                                            else "") if cur else ""), [], True)
        if name is None:
            return
        if name == "":
            if cur and self.ask_yes("Twitch", f"Remove the Twitch link of {gamertag}?", yes="Remove link",
                                    danger=True):
                self.db.unlink_twitch([aid])
                self.reload()
            return
        self._set_busy("twitchlink", True)
        self.set_status(f"Asking Twitch about '{name}'...")

        def work():
            try:
                self.q.put(("twitch_link", (aid, self.tclient.resolve(name), None)))
            except ValueError as e:
                self.q.put(("twitch_link", (aid, None, str(e))))
            except ProviderError as e:
                self.q.put(("twitch_link", (aid, None, str(e))))
            except Exception as e:
                self.q.put(("twitch_link", (aid, None, f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _on_twitch_link(self, account_id, user, err):
        self._set_busy("twitchlink", False)
        if err:
            return self.set_status(f"Could not link Twitch: {err}", error=True)
        try:
            self.db.link_twitch(account_id, user.id, user.login, user.display_name)
        except ValueError as e:
            return self.set_status(f"Could not link Twitch: {e}", error=True)
        self.set_status(f"Linked to Twitch channel {user.display_name} (ID {user.id}).")
        self.reload()
        self.twitch_check_now(manual=True)

    def sot_streams(self):
        """Everyone live in Sea of Thieves on Twitch right now, so the user can spot a streamer they just met."""
        if not self.tcreds.configured():
            return self.set_status("Twitch is not set up. Use ☰ > Twitch setup... first.", error=True)
        if "sotstreams" in self.busy:
            return
        self._set_busy("sotstreams", True)
        self.set_status("Asking Twitch who is live in Sea of Thieves...")
        if "streams" not in self.pages:
            self._loading_page("streams", "Asking Twitch who is live in Sea of Thieves...")

        def work():
            try:
                streams = self.tclient.game_streams()
                try:
                    pics = self.tclient.profile_pictures([s["id"] for s in streams])
                except ProviderError:                   # no pictures is fine: letters instead
                    pics = {}
                for s in streams:
                    s["pic"] = pics.get(s["id"], "")
                self.q.put(("sot_streams", (streams, None)))
            except ProviderError as e:
                self.q.put(("sot_streams", ([], str(e))))
            except Exception as e:
                self.q.put(("sot_streams", ([], f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _uptime(started, now) -> str:
        if not started:
            return ""
        m = max(0, int(now - started)) // 60
        return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"

    LIVE_RED = "#e5484d"
    TWITCH_PURPLE = "#9146ff"
    STREAM_ROWS = {"cards": 78, "compact": 38, "rows": 28}     # Live streams / Live now: height per view
    STREAM_GAPS = {"cards": 4, "compact": 4, "rows": 0}
    LIVE_ROWS = {"cards": 76, "compact": 42, "rows": 28}
    VIEWER_RANGES = (("Any viewers", "", ""), ("Under 10", "", "9"), ("10 – 100", "10", "100"),
                     ("100 – 1,000", "100", "1000"), ("1,000+", "1000", ""))

    def _on_sot_streams(self, streams, err):
        self._set_busy("sotstreams", False)
        if err:
            return self.set_status(f"Live SoT streams failed: {err}", error=True)
        self.set_status(f"{len(streams)} streams live in Sea of Thieves right now.")
        self.last_streams = streams                              # the tab shows these again without asking
        geo = None
        P = self.pal
        win = self.sot_win = self._page("streams", "Live streams", closable=False, fresh=True)
        win.title(f"Live in Sea of Thieves ({len(streams)})")
        win.configure(bg=P["bg"])
        accounts = self.db.list_accounts(owner=self.xkey.active())
        linked = {l["twitch_id"]: l["account_id"] for l in self.db.twitch_links()}
        master = self.db.shared_by_twitch()                    # also players this PC has never met
        tag_of = {a["id"]: a["gamertag"] for a in accounts}

        def match(s):
            if s["id"] in linked:
                return "linked: " + tag_of.get(linked[s["id"]], "?")
            if s["id"] in master:
                return "linked: " + master[s["id"]] + SHARED_MARK
            return ""

        # ---- header: title, count, refresh
        head = tk.Frame(win, bg=P["head"])
        head.pack(fill="x")
        top = tk.Frame(head, bg=P["head"])
        top.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(top, text="Live in Sea of Thieves", bg=P["head"], fg=P["fg"], font=("Segoe UI", 15, "bold")).pack(
            side="left")
        shown = tk.StringVar()
        tk.Label(top, textvariable=shown, bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left",
                                                                                                padx=(12, 0))
        chat_ui.RoundButton(top, "↻  Refresh", self.sot_streams, P, P["head"], kind="accent", pady=7).pack(
            side="right")
        filters_btn = chat_ui.RoundButton(top, "", lambda: toggle_filters(), P, P["head"], pady=7)
        filters_btn.pack(side="right", padx=(0, 8))
        stream_view = self.db.get_setting("stream_view", "cards")
        if stream_view not in self.STREAM_ROWS:
            stream_view = "cards"
        view_now = {"v": stream_view}
        sview_var = tk.StringVar(value=self.VIEW_NAMES[stream_view])

        def pick_view():
            v = {b: a for a, b in self.VIEW_NAMES.items()}[sview_var.get()]
            self.db.set_setting("stream_view", v)
            view_now["v"], tv.row, tv.gap = v, self.STREAM_ROWS[v], self.STREAM_GAPS[v]
            tv.redraw()
        chat_ui.PillSelect(top, P, P["head"], sview_var, values=list(self.VIEW_NAMES.values()), on_change=pick_view,
                           prefix="View: ", pady=7).pack(side="right", padx=(0, 8))
        hint_lbl = tk.Label(head, text="Everyone streaming Sea of Thieves on Twitch now. ✔ marks a stream of one of "
                            "your players. Click a stream for a live preview; double-click to watch it on Twitch.",
                            bg=P["head"], fg=P["muted"], font=("Segoe UI", 9), anchor="w", justify="left",
                            wraplength=900)
        hint_lbl.pack(fill="x", padx=16, pady=(0, 8))

        # ---- filters: search, language, viewers, only my players
        filt = chat_ui.Flow(head, P["head"])
        filt.pack(fill="x", padx=12, pady=(0, 6))
        fvar = tk.StringVar()

        def make_entry(parent_, bg):
            return tk.Entry(parent_, textvariable=fvar, bg=bg, fg=P["fg"], insertbackground=P["fg"], relief="flat",
                            bd=0, font=("Segoe UI", 10), highlightthickness=0)
        sbox = chat_ui.RoundBox(filt, P, P["head"], make_entry, height=36, radius=18)
        sbox.configure(width=260)
        filt.add(sbox)
        hint = tk.Label(sbox, text="Search channel, title or tag", bg=sbox.inner.cget("bg"), fg=P["muted"],
                        font=("Segoe UI", 10), cursor="xterm")
        hint.bind("<Button-1>", lambda e: sbox.inner.focus_set())

        def show_hint(*_):
            if fvar.get() or win.focus_get() is sbox.inner:
                hint.place_forget()
            else:
                hint.place(in_=sbox.inner, x=0, rely=0.5, anchor="w")
        fvar.trace_add("write", show_hint)
        sbox.inner.bind("<FocusIn>", show_hint, add="+")
        sbox.inner.bind("<FocusOut>", lambda e: win.after(1, show_hint), add="+")
        win.after(50, show_hint)
        langs = {}
        for s in streams:
            langs[s["language"] or "?"] = langs.get(s["language"] or "?", 0) + 1
        lang_label = {"All languages": None, **{f"{l} ({n})": l for l, n in
                                                sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))}}
        lvar = tk.StringVar(value="All languages")
        filt.add(chat_ui.PillSelect(filt, P, P["head"], lvar, values=list(lang_label), pady=8), space=2)
        vmin, vmax, vrange = tk.StringVar(), tk.StringVar(), tk.StringVar(value="Any viewers")

        def pick_range():
            lo, hi = next(((a, b) for n, a, b in self.VIEWER_RANGES if n == vrange.get()), ("", ""))
            vmin.set(lo)
            vmax.set(hi)
        filt.add(chat_ui.PillSelect(filt, P, P["head"], vrange, values=[r[0] for r in self.VIEWER_RANGES],
                                    on_change=pick_range, pady=8))
        mine = tk.BooleanVar()
        mine_btn = chat_ui.RoundButton(filt, "Only my players", lambda: mine.set(not mine.get()), P, P["head"], pady=8)
        filt.add(mine_btn)
        mine.trace_add("write", lambda *a: mine_btn.set_kind("accent" if mine.get() else "soft"))

        # ---- sort chips
        chips_row = chat_ui.Flow(head, P["head"], gap=4)
        chips_row.pack(fill="x", padx=12, pady=(0, 12))
        chips_row.add(tk.Label(chips_row, text="Sort", bg=P["head"], fg=P["muted"], font=("Segoe UI", 9), padx=4))
        sort = {"col": None, "desc": False}
        chips = {}
        for c, label in (("viewers", "Viewers"), ("uptime", "Live for"), ("channel", "Channel"),
                         ("match", "Your player"), ("lang", "Language")):
            b = chat_ui.RoundButton(chips_row, label, lambda c=c: sort_by(c), P, P["head"], padx=12, pady=5)
            b.label = label
            chips_row.add(b)
            chips[c] = b
        reset_btn = chat_ui.RoundButton(chips_row, "Reset view", lambda: reset(), P, P["head"], padx=12, pady=5)
        chips_row.add(reset_btn, right=True)

        def show_filters(on):
            """The hint, filter and sort rows; hidden, the list gets the room (the filters still apply)."""
            for w, pady in ((hint_lbl, (0, 8)), (filt, (0, 6)), (chips_row, (0, 12))):
                if on:
                    w.pack(fill="x", padx=16 if w is hint_lbl else 12, pady=pady)
                else:
                    w.pack_forget()
            if not on:                                        # keep the header from collapsing into the title
                top.pack_configure(pady=(14, 12))
            else:
                top.pack_configure(pady=(14, 4))
            active = bool(fvar.get().strip() or lang_label.get(lvar.get()) or vmin.get() or vmax.get() or mine.get())
            filters_btn.set_text(("Hide filters  ▴" if on else "Filters  ▾") + ("" if on or not active else "  •"))
            filters_btn.set_kind("soft")

        def toggle_filters():
            on = self.db.get_setting("stream_filters", "1") != "1"
            self.db.set_setting("stream_filters", "1" if on else "0")
            show_filters(on)
        win.toggle_filters = toggle_filters
        win.set_view = lambda label: (sview_var.set(label), pick_view())             # for tests

        def show_chips():
            for c, b in chips.items():
                on = c == sort["col"]
                b.set_kind("accent" if on else "soft")
                b.set_text(b.label + ((" ▼" if sort["desc"] else " ▲") if on else ""))
        keys = {"match": lambda s: (not match(s), match(s).casefold()), "channel": lambda s: s["name"].casefold(),
                "viewers": lambda s: s["viewers"], "uptime": lambda s: -(s["started"] or 0),
                "lang": lambda s: s["language"], "title": lambda s: s["title"].casefold(),
                "tags": lambda s: ", ".join(s.get("tags") or []).casefold()}

        def sort_by(c):
            sort["desc"] = not sort["desc"] if sort["col"] == c else c == "viewers"
            sort["col"] = c
            fill()
            remember()

        # ---- link bar at the bottom (packed before the list so it never gets squeezed out)
        bar = tk.Frame(win, bg=P["head"])
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=P["head"])
        inner.pack(fill="x", padx=12, pady=10)
        open_btn = chat_ui.RoundButton(inner, "Open stream", lambda: open_stream(), P, P["head"], kind="accent",
                                       padx=18, pady=7)
        open_btn.pack(side="left", padx=3)
        rep_btn = chat_ui.RoundButton(inner, "Find on SOTREP", lambda: find_on_sotrep(), P, P["head"], pady=7)
        rep_btn.pack(side="left", padx=3)

        # ---- the streams as cards
        now = time.time()
        small, bold, tiny = ("Segoe UI", 8), ("Segoe UI", 11, "bold"), ("Segoe UI", 8, "bold")
        bold_font = tkfont.Font(font=bold)

        def draw(c, iid, values, tags, x1, y1, x2, y2, sel, hov):
            s = rows[iid]
            m = values[0]
            ok = m.startswith("linked")
            if view_now["v"] == "rows" and tv.expanded != iid:
                tv.line(iid, x1, y1, x2, y2, sel, hov, stripe=P["accent"] if ok else None)
                return draw_row(c, iid, s, values, m, ok, x1, y1, x2, y2, pic=False)
            tv.card(x1, y1, x2, y2, sel, hov, stripe=P["accent"] if ok else None)
            if tv.expanded == iid:
                return draw_open(c, iid, s, values, m, ok, x1, y1, x2, y2)
            if view_now["v"] == "compact":
                return draw_row(c, iid, s, values, m, ok, x1, y1, x2, y2)
            cx, cy = x1 + 40, (y1 + y2) / 2
            img = self.pictures.get(s.get("pic"), tv.picture_arrived) if s.get("pic") else None
            if img is not None:
                c.create_image(cx, cy, image=img)
            else:
                c.create_oval(cx - 20, cy - 20, cx + 20, cy + 20, fill=self.TWITCH_PURPLE, outline="")
                c.create_text(cx, cy, text=(s["name"][:1] or "?").upper(), fill="#ffffff",
                              font=("Segoe UI", 13, "bold"))
            right = x2 - 14
            vx = chat_ui.pill(c, right, y1 + 22, f"●  {s['viewers']:,}", self.LIVE_RED, "#ffffff", tiny)
            mx = vx
            if m:
                mx = chat_ui.pill(c, right, y2 - 20, "✔ " + m.split(": ", 1)[1],
                                  chat_ui._mix(P["panel"], P["accent"], 0.45), P["fg"], small)
            tx = cx + 32
            name_w = min(bold_font.measure(s["name"]), max(60, vx - tx - 150))
            c.create_text(tx, y1 + 20, anchor="w", text=chat_ui.fit(s["name"], bold, name_w), fill=P["fg"], font=bold)
            meta = "  ·  ".join(x for x in (values[3] and "live " + values[3], (s["language"] or "").upper()) if x)
            c.create_text(tx + name_w + 10, y1 + 21, anchor="w", text=chat_ui.fit(meta, small, vx - tx - name_w - 20),
                          fill=P["muted"], font=small)
            c.create_text(tx, y1 + 41, anchor="w", text=chat_ui.fit(s["title"], ("Segoe UI", 9), mx - tx - 12),
                          fill=P["fg"], font=("Segoe UI", 9))
            if values[6]:
                c.create_text(tx, y1 + 59, anchor="w", text=chat_ui.fit("#  " + values[6].replace(", ", "  ·  "),
                                                                        small, mx - tx - 12),
                              fill=P["muted"], font=small)

        def draw_open(c, iid, s, values, m, ok, x1, y1, x2, y2):
            """An unfolded stream: Twitch's live snapshot, the whole title, the tags and buttons."""
            wide = x2 - x1 >= 660
            pw, ph = (320, 180) if wide else (192, 108)
            px, py = x1 + 16, y1 + 16
            img = self.previews.get(s.get("thumb"), tv.picture_arrived, size=(pw, ph))
            if img is not None:
                c.create_image(px, py, image=img, anchor="nw")
            else:
                chat_ui.round_rect(c, px, py, px + pw, py + ph, 10, fill=chat_ui._mix(P["bg"], "#000000", 0.3),
                                   outline="")
                c.create_text(px + pw / 2, py + ph / 2, text="Loading the preview..." if s.get("thumb") else
                              "No preview", fill=P["muted"], font=("Segoe UI", 9))
            c.create_oval(px + pw / 2 - 22, py + ph / 2 - 22, px + pw / 2 + 22, py + ph / 2 + 22,
                          fill=chat_ui._mix("#000000", self.TWITCH_PURPLE, 0.6), outline="")
            c.create_text(px + pw / 2 + 2, py + ph / 2, text="▶", fill="#ffffff", font=("Segoe UI", 16))
            tv.spot(iid, (px, py, px + pw, py + ph), lambda: open_login(s["login"]))
            right = x2 - 14
            vx = chat_ui.pill(c, right, y1 + 26, f"●  {s['viewers']:,} watching", self.LIVE_RED, "#ffffff", tiny)
            tx, ty = px + pw + 18, y1 + 16                    # the text next to the preview, also when slim
            c.create_text(tx, ty + 10, anchor="w", text=chat_ui.fit(s["name"], bold, max(40, vx - tx - 12)),
                          fill=P["fg"], font=bold)
            meta = "  ·  ".join(x for x in ("live " + values[3] if values[3] else "",
                                            (s["language"] or "").upper(),
                                            ("✔ " + m.split(": ", 1)[1]) if ok else "") if x)
            c.create_text(tx, ty + 32, anchor="w", text=meta, fill=P["muted"], font=small)
            title = c.create_text(tx, ty + 46, anchor="nw", text=s["title"], fill=P["fg"], font=("Segoe UI", 10),
                                  width=max(120, right - tx - 4))
            bb = c.bbox(title) or (0, 0, 0, ty + 66)
            if values[6]:
                c.create_text(tx, bb[3] + 8, anchor="nw", text="#  " + values[6].replace(", ", "   #  "),
                              fill=P["muted"], font=small, width=max(120, right - tx - 4))
            bx, by = tx, y2 - 30
            for label, kind, fn in (("▶  Watch on Twitch", "accent", lambda: open_login(s["login"])),
                                    ("Find on SOTREP", "soft", lambda: webbrowser.open(
                                        sotrep.twitch_search_url(s["login"])))):
                fill_c, fg_c = (P["accent"], P["bg"]) if kind == "accent" else                     (chat_ui._mix(P["panel"], P["fg"], 0.12), P["fg"])
                w = chat_ui.pill(c, bx, by, label, fill_c, fg_c, ("Segoe UI", 9, "bold"), anchor="w", padx=14, h=28)
                end = bx + (tkfont.Font(font=("Segoe UI", 9, "bold")).measure(label) + 28)
                tv.spot(iid, (w, by - 14, end, by + 14), fn)
                bx = end + 8

        def draw_row(c, iid, s, values, m, ok, x1, y1, x2, y2, pic=True):
            """One stream on one line: (picture,) name, live for, language, title; viewers (and your player) right."""
            cy = (y1 + y2) / 2
            cx = x1 + 22
            if pic:
                img = self.pictures.get(s.get("pic"), tv.picture_arrived, size=24) if s.get("pic") else None
                if img is not None:
                    c.create_image(cx, cy, image=img)
                else:
                    c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, fill=self.TWITCH_PURPLE, outline="")
                    c.create_text(cx, cy, text=(s["name"][:1] or "?").upper(), fill="#ffffff",
                                  font=("Segoe UI", 9, "bold"))
            else:
                cx = x1 - 6
            right = x2 - 12
            right = chat_ui.pill(c, right, cy, f"●  {s['viewers']:,}", self.LIVE_RED, "#ffffff", tiny, h=18) - 8
            if ok:
                right = chat_ui.pill(c, right, cy, "✔ " + m.split(": ", 1)[1],
                                     chat_ui._mix(P["panel"], P["accent"], 0.45), P["fg"], small, h=18) - 8
            tx = cx + 20
            name_font = ("Segoe UI", 10, "bold") if pic else ("Segoe UI", 9, "bold")
            nw = min(tkfont.Font(font=name_font).measure(s["name"]), 170)
            c.create_text(tx, cy, anchor="w", text=chat_ui.fit(s["name"], name_font, nw), fill=P["fg"], font=name_font)
            meta = "  ·  ".join(x for x in (values[3], (s["language"] or "").upper()) if x)
            mx = tx + nw + 10
            c.create_text(mx, cy, anchor="w", text=meta, fill=P["muted"], font=small)
            mx += tkfont.Font(font=small).measure(meta) + 14
            c.create_text(mx, cy, anchor="w", text=chat_ui.fit(s["title"], ("Segoe UI", 9), right - mx - 6),
                          fill=P["fg"], font=("Segoe UI", 9))

        def open_login(login):
            webbrowser.open(f"https://www.twitch.tv/{login}")

        tv = chat_ui.CardList(win, P, draw, row=self.STREAM_ROWS[stream_view], big=232,
                              gap=self.STREAM_GAPS[stream_view],
                              empty="No stream matches these filters")
        tv.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        rows = {}

        def num(var):
            try:
                return int(var.get().strip())
            except ValueError:
                return None

        def fill(*_):
            tv.delete(*tv.get_children())
            rows.clear()
            f = fvar.get().strip().casefold()
            lang, lo, hi = lang_label.get(lvar.get()), num(vmin), num(vmax)
            if sort["col"]:
                order = sorted(streams, key=keys[sort["col"]], reverse=sort["desc"])
            else:                                      # default: most viewers first, linked or not
                order = sorted(streams, key=lambda s: -s["viewers"])
            for s in order:
                m, tags = match(s), ", ".join(s.get("tags") or [])
                if f and not any(f in x.casefold() for x in (s["name"], s["title"], m, tags)):
                    continue
                if (lang and (s["language"] or "?") != lang) or (lo is not None and s["viewers"] < lo) \
                        or (hi is not None and s["viewers"] > hi) or (mine.get() and not m):
                    continue
                iid = tv.insert("", "end", tags=("match",) if m else (), values=(
                    m, s["name"], s["viewers"], self._uptime(s["started"], now), s["language"], s["title"], tags))
                rows[iid] = s
            shown.set(f"{len(rows)} of {len(streams)} streams")
            show_chips()

        keep = getattr(self, "sot_filters", None)                     # Refresh keeps the filters,
        if keep is None:                                              # and so does the next start (not the search)
            try:
                keep = json.loads(self.db.get_setting("stream_filter_values", "{}") or "{}")
            except ValueError:
                keep = {}
            keep = keep if isinstance(keep, dict) else {}
        fvar.set(keep.get("search", ""))
        lvar.set(next((k for k, v in lang_label.items() if v == keep.get("lang")), "All languages"))
        vrange.set(keep.get("vrange", "Any viewers"))
        vmin.set(keep.get("vmin", ""))
        vmax.set(keep.get("vmax", ""))
        mine.set(keep.get("mine", False))
        sort.update(keep.get("sort", {}))

        def remember(*_):
            self.sot_filters = {"search": fvar.get(), "lang": lang_label.get(lvar.get()), "vmin": vmin.get(),
                                "vmax": vmax.get(), "vrange": vrange.get(), "mine": mine.get(), "sort": dict(sort)}
            self.db.set_setting("stream_filter_values", json.dumps(
                {k: v for k, v in self.sot_filters.items() if k != "search"}))
        for v in (fvar, lvar, vmin, vmax, mine):
            v.trace_add("write", fill)
            v.trace_add("write", remember)
        fill()
        show_filters(self.db.get_setting("stream_filters", "1") == "1")

        def open_stream():
            for i in tv.selection():
                webbrowser.open(f"https://www.twitch.tv/{rows[i]['login']}")

        def find_on_sotrep():
            for i in tv.selection():
                webbrowser.open(sotrep.twitch_search_url(rows[i]["login"]))

        def card_menu(e):
            iid = tv.identify_row(e.y)
            if not iid:
                return
            if iid not in tv.selection():
                tv.selection_set([iid])
            s = rows[iid]
            m = self._menu(win)
            m.add_command(label="Open stream", command=open_stream)
            m.add_command(label="Find on SOTREP", command=find_on_sotrep)
            m.add_separator()
            m.add_command(label="Copy channel name", command=lambda: (self.root.clipboard_clear(),
                                                                      self.root.clipboard_append(s["login"])))
            m.tk_popup(e.x_root, e.y_root)

        tv.bind("<Double-1>", lambda e: open_stream())
        tv.bind("<Button-3>", card_menu)

        def reset():
            """Back to the default view: no filters, most viewers first."""
            fvar.set("")
            lvar.set("All languages")
            vrange.set("Any viewers")
            vmin.set("")
            vmax.set("")
            mine.set(False)
            sort.update(col=None, desc=False)
            fill()
            remember()
            tv.yview_moveto(0)
        win.reset = reset
        win.streams_tv, win.filter_var = tv, fvar                                       # kept for tests
        win.lang_var, win.vmin, win.vmax, win.mine, win.sort_by = lvar, vmin, vmax, mine, sort_by
        win.find_on_sotrep, win.rep_btn = find_on_sotrep, rep_btn
        win.minsize(780, 420)
        if geo:
            win.geometry(geo)
        else:
            self._place_dialog(win, 1000, 720)

    def never_link_selected(self):
        """Toggle 'never link a Twitch channel' (cheaters, ...) for the selected players."""
        ids = self._selected_ids()
        if not ids:
            return self.set_status("Select the player(s) that should never get a Twitch channel.", error=True)
        marked = self.db.twitch_never(ids)
        if len(marked) == len(ids):
            self.db.clear_twitch_never(ids)
            self.set_status(f"Removed the 'never link' mark from {len(ids)} player(s).")
            return self.reload()
        reason = self._ask_tag("Never link", f"Never link a Twitch channel for {len(ids)} player(s).\n"
                               "Reason (optional, e.g. cheater):", ["cheater"], True)
        if reason is None:
            return
        self.db.set_twitch_never(ids, reason)
        self.set_status(f"Marked {len(ids)} player(s) 'never link'.")
        self.reload()

    def open_twitch_selected(self):
        links = self.db.twitch_links(self._selected_ids())
        if not links:
            return self.set_status("Select a player that has a Twitch channel linked.", error=True)
        for l in links:
            webbrowser.open(f"https://www.twitch.tv/{l['login']}")

    def twitch_check_now(self, manual=False):
        """Live status of the linked Twitch channels."""
        if self.twitch_busy or not self.tcreds.configured():
            return
        self.twitch_busy = True
        self._set_busy("twitch", True)

        def work():
            try:
                result = self.tcheck.check_accounts() if self.db.twitch_links() else {}
                self.q.put(("twitch", (result, None, manual)))
            except ProviderError as e:
                self.q.put(("twitch", ({}, str(e), manual)))
            except Exception as e:
                self.q.put(("twitch", ({}, f"Unexpected error: {e}", manual)))

        threading.Thread(target=work, daemon=True).start()

    def _on_twitch(self, result, err, manual):
        self.twitch_busy = False
        self._set_busy("twitch", False)
        if err:
            self.set_status(f"Twitch check failed: {err}", error=True)
        elif manual:
            live_n = sum(1 for v in result.values() if v["live"])
            self.set_status(f"Twitch: {live_n} of {len(result)} linked player(s) live.")
        self.reload()

    def _schedule_twitch(self, delay_ms=CHECK_EVERY * 1000):
        if self.twitch_after_id:
            self.root.after_cancel(self.twitch_after_id)
            self.twitch_after_id = None
        if not self.closing:
            self.twitch_after_id = self.root.after(delay_ms, self._twitch_tick)

    def _twitch_tick(self):
        self.twitch_after_id = None
        if not self.paused:
            self.twitch_check_now()
        self._schedule_twitch()


    # ---- on-demand live check ----
    ALL_XBOX = 30          # "Check live" for everyone: Xbox is asked about this many most recent players (3 requests)

    def check_live_all(self):
        """Everyone with source Live: every linked channel is asked on Twitch (100 per request), Xbox's own
        broadcasting flag only for the ALL_XBOX most recently seen (10 per request, hourly budget). Lists who is live."""
        with self.db.lock:
            ids = [r["id"] for r in self.db.conn.execute(
                "SELECT id FROM accounts WHERE source IN ('xbox-live','openxbl') ORDER BY last_seen DESC")]
        if not ids:
            return self.set_status("No players with source Live yet: refresh first.", error=True)
        xbox = ids[:self.ALL_XBOX] if self.xkey.get() else []
        tw = [l["account_id"] for l in self.db.twitch_links(ids)] if self.tcreds.configured() else []
        if not xbox and not tw:
            return self.set_status("Add an OpenXBL account or set up Twitch first.", error=True)
        note = (f"Asked Twitch about {len(tw)} linked player(s)" if tw else "Twitch not set up") + \
               (f" and Xbox about the {len(xbox)} most recently seen." if xbox else "; no OpenXBL account for Xbox.")
        self._start_livecheck(xbox, sorted(set(xbox) | set(tw)), only_live=True, note=note)

    def check_live_selected(self):
        ids = self._selected_ids()
        if not ids:
            return self.set_status("Check live: select one or more players first (up to 20 at a time).",
                                   error=True)
        if not self.xkey.get() and not (self.tcreds.configured() and self.db.twitch_links(ids)):
            return self.set_status("Add an OpenXBL account first.", error=True)
        self._start_livecheck(ids if self.xkey.get() else [], ids)

    def _start_livecheck(self, xbox_ids, ids, only_live=False, note=""):
        if "livecheck" in self.busy:
            return
        self._set_busy("livecheck", True)
        if "live" not in self.pages:
            self._loading_page("live", "Checking who is live...")
        self.set_status("Checking who is live..." if only_live else f"Checking {min(len(ids), 20)} selected player(s)...")

        def work():
            try:
                results = (self.live.check_accounts(xbox_ids, limit=self.ALL_XBOX) if only_live else
                           self.live.check_accounts(xbox_ids)) if xbox_ids else []
                asked = {r["id"] for r in results}
                rest = [i for i in ids if i not in asked]
                if rest:
                    results += self._blank_results(rest, "not asked (only the most recent players)" if only_live
                                                   else "no OpenXBL account added")
                tw, tw_err = {}, None
                links = {l["account_id"]: l for l in self.db.twitch_links(ids)}
                if links and self.tcreds.configured():
                    try:
                        tw = self.tcheck.check_accounts(list(links))
                    except ProviderError as e:
                        tw_err = str(e)
                for r in results:
                    r["tw"], r["tw_err"], r["tw_link"] = tw.get(r["id"]), tw_err, links.get(r["id"])
                    if r["tw_link"] is not None:
                        r["tw_link"] = dict(r["tw_link"])
                if only_live:
                    results = sorted((r for r in results if self._is_live(r)),
                                     key=lambda r: -max(r["viewers"] or 0, (r["tw"] or {}).get("viewers") or 0))
                self.q.put(("livecheck", (results, None, only_live, note + (f" Twitch: {tw_err}" if tw_err else ""))))
            except ProviderError as e:
                self.q.put(("livecheck", ([], str(e))))
            except Exception as e:  # never let a worker thread die silently
                self.q.put(("livecheck", ([], f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _blank_results(self, ids, error="no OpenXBL account added"):
        with self.db.lock:
            rows = self.db.conn.execute(
                f"SELECT id, gamertag FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return [{"id": r["id"], "gamertag": r["gamertag"], "state": None, "title": None, "seen_at": None,
                 "broadcasting": False, "provider": None, "twitch": False, "viewers": None, "started": None,
                 "error": error} for r in rows]

    @staticmethod
    def _is_live(r) -> bool:
        return bool(r["broadcasting"] or r["twitch"] or (r.get("tw") and r["tw"]["live"]))

    @staticmethod
    def _twitch_text(r) -> str:
        link, tw = r.get("tw_link"), r.get("tw")
        if link is None:
            return "no Twitch channel saved"
        if r.get("tw_err"):
            return f"{link['display_name']}: {r['tw_err']}"
        if tw is None:
            return f"{link['display_name']}: not checked"
        if tw["live"]:
            title = (tw.get("title") or "")[:60]
            return f"LIVE: {tw.get('game') or 'no game set'} - {title} ({tw.get('viewers', 0)} viewers)"
        return f"{link['display_name']}: offline"

    def _show_live_results(self, results, only_live=False, note=""):
        """Who is live, as cards: picture, name, what Xbox and Twitch say, viewers and how long. Double-click (or
        Open Twitch) opens the linked channel; right-click for more."""
        P = self.pal
        self.last_live = (results, only_live, note)              # the tab shows these again without asking
        geo = None
        win = self.live_win = self._page("live", "Live now", closable=False, fresh=True)
        live_n = sum(1 for r in results if self._is_live(r))
        win.title(f"Live now: {len(results)} player(s)" if only_live else "Live check results")
        win.configure(bg=P["bg"])
        with self.db.lock:
            ids = [r["id"] for r in results]
            pics = {row[0]: row[1] for row in self.db.conn.execute(
                f"SELECT a.id, p.pic FROM accounts a JOIN xbox_profiles p ON p.xuid = a.xuid "
                f"WHERE a.id IN ({','.join('?' * len(ids))}) AND p.pic IS NOT NULL", ids)} if ids else {}
            xuids = {row[0]: row[1] for row in self.db.conn.execute(
                f"SELECT id, xuid FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}

        head = tk.Frame(win, bg=P["head"])
        head.pack(fill="x")
        top = tk.Frame(head, bg=P["head"])
        top.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(top, text="Live now" if only_live else "Live check", bg=P["head"], fg=P["fg"],
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(top, text=(f"{live_n} live" + ("" if only_live else f" of {len(results)} asked")),
                 bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left", padx=(12, 0))
        chat_ui.RoundButton(top, "↻  Check again", self.check_live_all if only_live else self.check_live_selected,
                            P, P["head"], kind="accent", pady=7).pack(side="right")
        live_view = self.db.get_setting("live_view", "cards")
        if live_view not in self.STREAM_ROWS:
            live_view = "cards"
        lview = {"v": live_view}
        lview_var = tk.StringVar(value=self.VIEW_NAMES[live_view])

        def pick_live_view():
            v = {b: a for a, b in self.VIEW_NAMES.items()}[lview_var.get()]
            self.db.set_setting("live_view", v)
            lview["v"], tv.row, tv.gap = v, self.LIVE_ROWS[v], self.STREAM_GAPS[v]
            tv.redraw()
        chat_ui.PillSelect(top, P, P["head"], lview_var, values=list(self.VIEW_NAMES.values()),
                           on_change=pick_live_view, prefix="View: ", pady=7).pack(side="right", padx=(0, 8))
        win.set_view = lambda label: (lview_var.set(label), pick_live_view())        # for tests
        tk.Label(head, text=((note + "  ") if note else "") + "Twitch: straight from Twitch for players you linked "
                 "to a channel. Xbox: its own broadcasting flag, which misses streams started from a PC. Double-click "
                 "a card to open the stream.", bg=P["head"], fg=P["muted"], font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=820).pack(fill="x", padx=16, pady=(0, 12))

        logins = {}
        bold, small, tiny = ("Segoe UI", 11, "bold"), ("Segoe UI", 8), ("Segoe UI", 8, "bold")
        by_iid = {}

        def what(r):
            tw, link = r.get("tw") or {}, r.get("tw_link")
            if tw.get("live"):
                return f"Twitch · {link['display_name']} · {tw.get('game') or 'no game set'} — {tw.get('title') or ''}"
            if r["twitch"]:
                return "Live on Twitch, says Xbox" + (f" · {r['viewers']:,} viewers" if r.get("viewers") else "")
            if r["broadcasting"]:
                return f"Broadcasting, says Xbox ({r['provider'] or 'service not stated'})"
            if r["error"]:
                return "Xbox not asked: " + r["error"]
            return self._twitch_text(r) if link else "Not broadcasting"

        def live_pill(c, r, right, cy, h=20):
            tw = r.get("tw") or {}
            if tw.get("live"):
                return chat_ui.pill(c, right, cy, f"●  LIVE  {tw.get('viewers', 0):,}", self.LIVE_RED, "#ffffff",
                                    tiny, h=h)
            if r["twitch"] or r["broadcasting"]:
                v = f"  {r['viewers']:,}" if r.get("viewers") is not None else ""
                return chat_ui.pill(c, right, cy, f"●  LIVE{v}", self.LIVE_RED, "#ffffff", tiny, h=h)
            return chat_ui.pill(c, right, cy, "offline", chat_ui._mix(P["panel"], P["fg"], 0.12), P["muted"], tiny,
                                h=h)

        def draw_line(c, iid, r, x1, y1, x2, y2, pic):
            """Compact (a small picture) or Rows (none): one line per player."""
            live, cy = self._is_live(r), (y1 + y2) / 2
            tx = x1 + 12
            if pic:
                img = self.pictures.get(pics.get(r["id"]), tv.picture_arrived, size=32) if r["id"] in pics else None
                chat_ui.draw_avatar(c, x1 + 26, cy, 14, r["gamertag"], xuids.get(r["id"]) or r["gamertag"], P, img,
                                    dim=not live)
                tx = x1 + 50
            if (r.get("state") or "").casefold() == "online":
                c.create_oval(tx - 1, cy - 4, tx + 7, cy + 4, fill="#4cc764", outline="")
                tx += 14
            px = live_pill(c, r, x2 - 12, cy, h=18 if pic else 16)
            name_font = ("Segoe UI", 10, "bold") if pic else ("Segoe UI", 9, "bold")
            nw = min(tkfont.Font(font=name_font).measure(r["gamertag"]), 180)
            c.create_text(tx, cy, anchor="w", text=chat_ui.fit(r["gamertag"], name_font, nw), fill=P["fg"],
                          font=name_font)
            c.create_text(tx + nw + 12, cy, anchor="w", text=chat_ui.fit(what(r), ("Segoe UI", 9), px - tx - nw - 22),
                          fill=P["fg"] if live else P["muted"], font=("Segoe UI", 9))

        def draw(c, iid, values, tags, x1, y1, x2, y2, sel, hov):
            r = by_iid[iid]
            live, tw = self._is_live(r), r.get("tw") or {}
            if lview["v"] == "rows":
                tv.line(iid, x1, y1, x2, y2, sel, hov, stripe=self.LIVE_RED if live else None)
                return draw_line(c, iid, r, x1, y1, x2, y2, pic=False)
            tv.card(x1, y1, x2, y2, sel, hov, stripe=self.LIVE_RED if live else None)
            if lview["v"] == "compact":
                return draw_line(c, iid, r, x1, y1, x2, y2, pic=True)
            cx, cy = x1 + 40, (y1 + y2) / 2
            img = self.pictures.get(pics.get(r["id"]), tv.picture_arrived) if r["id"] in pics else None
            chat_ui.draw_avatar(c, cx, cy, 20, r["gamertag"], xuids.get(r["id"]) or r["gamertag"], P, img,
                                dim=not live)
            if (r.get("state") or "").casefold() == "online":
                c.create_oval(cx + 9, cy + 9, cx + 19, cy + 19, fill="#4cc764", outline=P["panel"], width=2)
            right = x2 - 14
            if tw.get("live"):
                px = chat_ui.pill(c, right, y1 + 24, f"●  LIVE  {tw.get('viewers', 0):,}", self.LIVE_RED, "#ffffff",
                                  tiny)
                since = self._uptime(tw.get("started"), time.time())
            elif r["twitch"] or r["broadcasting"]:
                v = f"  {r['viewers']:,}" if r.get("viewers") is not None else ""
                px = chat_ui.pill(c, right, y1 + 24, f"●  LIVE{v}", self.LIVE_RED, "#ffffff", tiny)
                since = self._uptime(r.get("started"), time.time())
            else:
                px = chat_ui.pill(c, right, y1 + 24, "offline", chat_ui._mix(P["panel"], P["fg"], 0.12), P["muted"],
                                  tiny)
                since = ""
            if since:
                c.create_text(right, y2 - 18, anchor="e", text="live " + since, fill=P["muted"], font=small)
            tx = cx + 32
            c.create_text(tx, y1 + 20, anchor="w", text=chat_ui.fit(r["gamertag"], bold, px - tx - 12), fill=P["fg"],
                          font=bold)
            link = r.get("tw_link")
            if tw.get("live"):
                line = f"Twitch · {link['display_name']} · {tw.get('game') or 'no game set'} — {tw.get('title') or ''}"
            elif r["twitch"]:
                line = "Live on Twitch, says Xbox" + (f" · {r['viewers']:,} viewers" if r.get("viewers") else "")
            elif r["broadcasting"]:
                line = f"Broadcasting, says Xbox ({r['provider'] or 'service not stated'})"
            elif r["error"]:
                line = "Xbox not asked: " + r["error"]
            else:
                line = self._twitch_text(r) if link else "Not broadcasting"
            c.create_text(tx, y1 + 41, anchor="w", text=chat_ui.fit(line, ("Segoe UI", 9), right - tx - 90),
                          fill=P["fg"] if live else P["muted"], font=("Segoe UI", 9))
            playing = r.get("title") or ""
            if playing or r.get("state"):
                c.create_text(tx, y1 + 59, anchor="w",
                              text=chat_ui.fit(("Playing " + playing) if playing else (r.get("state") or ""), small,
                                               right - tx - 90), fill=P["muted"], font=small)

        tv = chat_ui.CardList(win, P, draw, row=self.LIVE_ROWS[live_view], gap=self.STREAM_GAPS[live_view],
                              empty="Nobody is live right now" if only_live else "")
        for r in results:
            if r["error"]:
                bcast, state, game = "-" if only_live else f"not checked: {r['error']}", "-", ""
            else:
                state = r["state"]
                since = f", since {time.strftime('%H:%M', time.localtime(r['started']))}" if r.get("started") else ""
                views = f", {r['viewers']} viewers" if r.get("viewers") is not None else ""
                if r["twitch"]:
                    bcast = f"LIVE on Twitch (Xbox's record says Twitch{since}{views})"
                elif r["broadcasting"]:
                    bcast = f"BROADCASTING per Xbox ({r['provider'] or 'service not stated'}{since}{views})"
                else:
                    bcast = "not broadcasting"
                game = r["title"] or ""
            iid = tv.insert("", "end", tags=("live",) if self._is_live(r) else (), values=(
                r["gamertag"], state or "", bcast, self._twitch_text(r), game))
            by_iid[iid] = r
            if r.get("tw_link"):
                logins[iid] = r["tw_link"]["login"]

        def open_twitch():
            for i in tv.selection():
                if i in logins:
                    webbrowser.open(f"https://www.twitch.tv/{logins[i]}")

        def card_menu(e):
            iid = tv.identify_row(e.y)
            if not iid:
                return
            tv.selection_set([iid])
            r = by_iid[iid]
            m = self._menu(win)
            m.add_command(label="Open Twitch", command=open_twitch, state="normal" if iid in logins else "disabled")
            m.add_command(label="SOTREP", command=lambda: webbrowser.open(sotrep.profile_url(r["gamertag"])))
            m.add_command(label="Xbox profile", command=lambda: self.open_xbox_profile(r["gamertag"],
                                                                                     xuids.get(r["id"]) or ""))
            m.tk_popup(e.x_root, e.y_root)
        tv.bind("<Double-1>", lambda e: open_twitch())
        tv.bind("<Button-3>", card_menu)

        bar = tk.Frame(win, bg=P["head"])
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=P["head"])
        inner.pack(fill="x", padx=12, pady=10)
        chat_ui.RoundButton(inner, "Open Twitch", open_twitch, P, P["head"], kind="accent", padx=18, pady=7).pack(
            side="left")
        tv.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        if results:
            tv.selection_set([tv.get_children()[0]])
        win.results_tv = tv                          # kept for tests
        if geo:
            win.geometry(geo)
        else:
            self._place_dialog(win, 860, min(760, 230 + 82 * max(1, min(len(results), 6))))

    def _on_livecheck(self, results, err, only_live=False, note=""):
        self._set_busy("livecheck", False)
        if err:
            return self.set_status(f"Live check failed: {err}", error=True)
        live_n = sum(1 for r in results if self._is_live(r))
        self.set_status(f"Live check: {live_n} player(s) live. {note}" if only_live else
                        f"Live check: {len(results)} player(s) asked, {live_n} live.")
        self.reload()                                # shows the Twitch icon for anyone confirmed live
        self._show_live_results(results, only_live, note)

    def set_refresh_mode(self, mode: str):
        """'Only while SoT is open', 'While Xbox says I play SoT', 'Always' or 'Off' (Off also stops the background
        live and Twitch checks)."""
        self.mode_var.set(mode)
        self.paused = mode == REFRESH_MODES[2]
        self.db.set_setting("paused", "1" if self.paused else "0")
        if not self.paused:
            self.db.set_setting("only_with_game", "1" if mode == REFRESH_MODES[0] else "0")
            self.db.set_setting("refresh_xbox", "1" if mode == XBOX_MODE else "0")
        self.presence_at = 0.0                       # ask Xbox again on the next round
        self.set_status({REFRESH_MODES[0]: "Auto-refresh only runs while Sea of Thieves is open on this PC.",
                         XBOX_MODE: "Auto-refresh runs while Xbox says you're in Sea of Thieves (PC or console).",
                         REFRESH_MODES[1]: "Auto-refresh always runs.",
                         REFRESH_MODES[2]: "Auto-refresh is off. 'Refresh now' still works."}[mode])
        self._schedule()

    def export(self):
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("JSON", "*.json")])
        if p:
            try:
                self.db.export(p)
                self.set_status(f"Exported to {p}")
            except OSError as e:
                self.set_status(f"Export failed: {e}", error=True)

    def import_dialog(self):
        win = self._page("import", "Import players", fresh=True)
        win.title("Import players")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, text="Paste gamertags (one per line, optionally `gamertag,xuid`) or JSON.\n"
                            "Imported players are marked 'Import', not 'Live'.").pack(padx=8, pady=6)
        box = scrolledtext.ScrolledText(win, width=60, height=14, bg=self.pal["panel"], fg=self.pal["fg"],
                                       insertbackground=self.pal["fg"])
        box.pack(padx=8, pady=4)

        def load_file():
            p = filedialog.askopenfilename(parent=win, filetypes=[("Text/CSV/JSON", "*.txt *.csv *.json")])
            if p:
                with open(p, encoding="utf-8-sig") as f:
                    box.delete("1.0", "end")
                    box.insert("1.0", f.read())

        def go():
            out = self.sync.run(ManualImportProvider(box.get("1.0", "end")))
            self._finish(out, imported=True)
            if out and out.ok:
                win.destroy()

        row = ttk.Frame(win)
        row.pack(pady=6)
        ttk.Button(row, text="Load file...", command=load_file).pack(side="left", padx=4)
        ttk.Button(row, text="Import", command=go).pack(side="left", padx=4)
        self._place_dialog(win)

    # ---- refresh ----
    def _provider(self):
        return OpenXblProvider(self.xkey) if self.xkey.get() else None

    def set_key(self):
        """Add an Xbox account: Microsoft sign-in (recommended) or an OpenXBL key."""
        win = self._page("account", "Add Xbox account", fresh=True)
        win.title("Add Xbox account")
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        ttk.Button(win, text="Sign in with Microsoft (recommended)",
                   command=lambda: (win.destroy(), self.sign_in_microsoft())).pack(fill="x", padx=16, pady=(16, 4))
        ttk.Label(win, style="Hint.TLabel", wraplength=420, justify="left", text=(
            "Opens Microsoft's own sign-in page in your browser; you type your password there, never in this app. "
            "The app talks to Xbox directly: no xbl.io account or key, the game and time for each recent player, "
            "and your full message inbox. " + ("The sign-in is saved encrypted for this Windows user only."
                                               if sys.platform == "win32" else
                                               "The sign-in is saved in a file only your user can read."))
        ).pack(fill="x", padx=16)
        ttk.Button(win, text="Use an OpenXBL key instead...",
                   command=lambda: (win.destroy(), self.add_openxbl_key())).pack(fill="x", padx=16, pady=(14, 4))
        ttk.Label(win, style="Hint.TLabel", wraplength=420, justify="left", text=(
            "The older way: a free key from xbl.io, a third-party service that talks to Xbox for you.")
        ).pack(fill="x", padx=16)
        ttk.Button(win, text="Cancel", command=win.destroy).pack(anchor="e", padx=16, pady=14)
        self._place_dialog(win)
        return win

    def sign_in_microsoft(self):
        if "key" in self.busy:
            return
        self.set_status("Sign in on Microsoft's page in your browser (choose the Xbox account to add)...")
        self._set_busy("key", True)

        def work():
            try:
                xuid, gamertag = self.msa.sign_in()
                self.q.put(("acct_added", (gamertag or "Account", MARK + xuid)))
            except ProviderError as e:
                self.q.put(("acct_err", str(e)))
            except Exception as e:                   # never let a worker thread die silently
                self.q.put(("acct_err", f"Unexpected error: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def add_openxbl_key(self):
        webbrowser.open("https://xbl.io/dashboard")
        ok, k = self._question("OpenXBL key", "Sign in at xbl.io (opened in your browser) with your Microsoft "
                               "account, copy your API key from the dashboard and paste it here:",
                               [("Cancel", False, "soft"), ("Add key", True, "accent")], entry="")
        if not ok or not (k and k.strip()):
            return
        self.set_status("Checking the key...")
        self._set_busy("key", True)

        def work():
            try:
                name = lookup_gamertag(k) or "Account"
                self.q.put(("acct_added", (name, k)))
            except ProviderError as e:
                self.q.put(("acct_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_accounts(self):
        self.acct_box.configure(values=self.xkey.names())
        self.acct_var.set(self.xkey.active() or "(none: add an account)")
        self.root.after(1500, self._account_pictures_round)

    ACCOUNT_PIC = 32

    def _account_image(self, name):
        """The small round picture of one of your own accounts (None until it is known and loaded)."""
        raw = self.db.get_setting("account_pic:" + str(name), "")
        if not raw:
            return None
        return self.pictures.get(raw, lambda: self.acct_box._sync() if self.acct_box.winfo_exists() else None,
                                 size=self.ACCOUNT_PIC)

    def _account_pictures_round(self):
        """Ask Xbox once a day per account for that account's own picture (one request each, in the background)."""
        if getattr(self, "_acct_pics_busy", False) or self.closing:
            return
        now = int(time.time())
        todo = [(n, self.xkey.get(n)) for n in self.xkey.names()
                if now - int(self.db.get_setting("account_pic_at:" + n, "0") or 0) > 86400]
        if not todo:
            return
        self._acct_pics_busy = True

        def work():
            found = {}
            for name, key in todo:
                try:
                    found[name] = self.fetch_account_picture(key)
                except Exception:                        # never let a worker thread die silently; retry tomorrow
                    found[name] = None
            self.q.put(("call", lambda: done(found)))

        def done(found):
            self._acct_pics_busy = False
            for name, raw in found.items():
                if raw is not None:
                    self.db.set_setting("account_pic:" + name, raw)
                    self.db.set_setting("account_pic_at:" + name, str(now))
            self.acct_box._sync()
        threading.Thread(target=work, daemon=True).start()

    def switch_account(self):
        self.xkey.set_active(self.acct_var.get())
        self.to_top_after_sync = True
        self.reload(to_top=True)
        self.set_status(f"Switched to {self.xkey.active()}. Refreshing...")
        self.msg_unread, self.req_waiting, self.msg_rounds = [], [], 0
        self._update_msg_button()
        self._schedule_messages(3_000)
        self.refresh_now(manual=True)

    def remove_account(self):
        name, key = self.xkey.active(), self.xkey.get()
        what = "Microsoft sign-in" if direct.is_ms_key(key) else "key"
        if name and self.ask_yes("Remove account", f"Remove the saved {what} for '{name}'? "
                                 "Your player history is kept.", yes="Remove account", danger=True):
            if direct.is_ms_key(key):
                self.msa.remove(key[len(MARK):])
            self.xkey.remove(name)
            self._refresh_accounts()
            self.set_status(f"Removed {name}.")

    def refresh_now(self, manual=True):
        prov = self._provider()
        if prov is None:
            return self.set_status("Not connected. Use ☰ > Add Xbox account..., or import players.", error=True)
        prov.force_friends = manual
        who = self.xkey.active()
        self.set_status(f"Refreshing as {who}...")
        if manual:
            self.live_check_now()
            self.twitch_check_now(manual=False)
        self._set_busy("sync", True)
        self.sync.run_async(prov, lambda out: self.q.put(("sync", out)))

    # ---- new Xbox messages: a pop-up and a count on the Messages button (Microsoft sign-in only) ----
    def set_message_popups(self):
        on = bool(self.msg_popups.get())
        self.db.set_setting("message_popups", "1" if on else "0")
        if on:
            self._schedule_messages(1_000)
        else:
            self.msg_unread, self.req_waiting = [], []
            self._update_msg_button()
        self.set_status("New Xbox messages pop up now (checked every minute, Microsoft sign-in accounts)." if on
                        else "No pop-ups for new Xbox messages.")

    def _schedule_messages(self, ms=None):
        if self.msg_after_id:
            self.root.after_cancel(self.msg_after_id)
            self.msg_after_id = None
        if not self.closing:
            self.msg_after_id = self.root.after(ms or MESSAGES_EVERY * 1000, self._messages_round)

    def _messages_round(self):
        """Ask Xbox for unread conversations (one request, plus names for the unread ones) and announce new ones."""
        self.msg_after_id = None
        key = self.xkey.get()
        if self.msg_popups.get() and direct.is_ms_key(key) and not self.msg_busy:
            self.msg_busy = True

            ask_requests = self.msg_rounds % 3 == 0             # friend requests: every third round is plenty
            self.msg_rounds += 1

            def work():
                try:
                    chats, err = social.unread_chats(key), None
                except ProviderError as e:
                    chats, err = None, str(e)
                except Exception as e:                   # never let a worker thread die silently
                    chats, err = None, f"Unexpected error: {e}"
                reqs = None
                if ask_requests:
                    try:
                        reqs = social.friend_requests(key)["received"]
                    except Exception:                    # quiet: the next round tries again
                        reqs = None
                self.q.put(("call", lambda: (self._after_requests(key, reqs),
                                             self._after_messages(key, chats, err))))
            threading.Thread(target=work, daemon=True).start()
        self._schedule_messages()

    def _after_messages(self, key, chats, err):
        self.msg_busy = False
        if err or chats is None or self.closing or key != self.xkey.get() or not self.msg_popups.get():
            return                                       # quiet: the next round tries again
        seen = self.msg_seen.setdefault(key, {})
        new = [c for c in chats if (c["at"] or 0) > seen.get(c["xuid"], 0)
               and (c["at"] or 0) > self.msg_opened.get(c["xuid"], 0)]
        for c in chats:
            seen[c["xuid"]] = max(seen.get(c["xuid"], 0), c["at"] or 0)
        self.msg_unread = [c for c in chats if (c["at"] or 0) > self.msg_opened.get(c["xuid"], 0)]
        self._update_msg_button()
        if new:
            self._message_popup(new)

    def _after_requests(self, key, received):
        if received is None or self.closing or key != self.xkey.get() or not self.msg_popups.get():
            return
        seen = self.req_seen.setdefault(key, set())
        new = [f for f in received if f["xuid"] not in seen]
        seen.update(f["xuid"] for f in received)
        self.req_waiting = list(received)
        self._update_msg_button()
        if new and (self.inbox_win is None or not self.inbox_win.winfo_exists()):
            names = [f["gamertag"] for f in new if not str(f["gamertag"]).isdigit()] or ["Someone"]
            title = (f"{names[0]} wants to be your friend" if len(new) == 1 else f"{len(new)} new friend requests")
            self._toast("👋  " + title, "Click to accept or ignore." if len(new) == 1 else ", ".join(names[:5]),
                        lambda: self.requests_dialog(goto=new[0]["xuid"] if len(new) == 1 else None))

    def _requests_seen(self, received):
        """The Friends & messages window loaded the requests: count them, and don't pop up for these."""
        key = self.xkey.get() or ""
        self.req_seen.setdefault(key, set()).update(f["xuid"] for f in received)
        self.req_waiting = list(received)
        self._update_msg_button()

    def _request_answered(self, xuid):
        self.req_waiting = [f for f in self.req_waiting if f["xuid"] != xuid]
        self._update_msg_button()

    def _chat_opened(self, xuid):
        # at least the newest known message's own time, so a PC clock behind Xbox's still clears it
        self.msg_opened[xuid] = max([time.time()] + [c["at"] or 0 for c in self.msg_unread if c["xuid"] == xuid])
        self.msg_unread = [c for c in self.msg_unread if c["xuid"] != xuid]
        self._update_msg_button()

    SOCIAL = "Friends & messages"

    def _open_social(self):
        """The tab opens on your friends (the pop-ups go straight to a chat or a request)."""
        return self.inbox_dialog(tab="friends")

    def _update_msg_button(self):
        n = len(self.msg_unread) + len(self.req_waiting)
        base = self.msg_btn.base
        self.msg_btn.set_text(f"{base} ({n})" if n else base)
        self.msg_btn.set_kind("accent" if n else "soft")

    def _message_popup(self, new):
        if len(new) == 1:
            c = new[0]
            name = c["gamertag"] if not c["gamertag"].isdigit() else "Someone"
            text = (c.get("preview") or "").replace("\n", " ")
            title, body, goto = name, text[:140] + ("…" if len(text) > 140 else ""), c["xuid"]
        else:
            names = [c["gamertag"] for c in new if not c["gamertag"].isdigit()]
            title = f"{len(new)} new Xbox messages"
            body = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            goto = None
        return self._toast("✉  " + title, body, lambda: self.inbox_dialog(goto=goto, tab="chats"))

    def _toast(self, title, text, on_click, seconds=12):
        """A small window at the bottom right of the screen, above other windows, that closes by itself. Clicking it
        runs on_click; ✕ only closes it. While the mouse is over it, it stays."""
        P = self.pal
        if self.toast is not None and self.toast.winfo_exists():
            self.toast.destroy()
        t = tk.Toplevel(self.root)
        t.withdraw()
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=P["accent"])
        inner = tk.Frame(t, bg=P["head"])
        inner.pack(fill="both", expand=True, padx=(4, 1), pady=1)
        head = tk.Label(inner, text=title, bg=P["head"], fg=P["fg"], font=("Segoe UI", 11, "bold"), anchor="w")
        head.pack(fill="x", padx=(14, 34), pady=(10, 2))
        body = tk.Label(inner, text=text, bg=P["head"], fg=P["fg"], font=("Segoe UI", 10), justify="left",
                        anchor="w", wraplength=320)
        body.pack(fill="x", padx=14)
        foot = tk.Label(inner, text="Click to open", bg=P["head"], fg=P["muted"], font=("Segoe UI", 8),
                        anchor="w")
        foot.pack(fill="x", padx=14, pady=(4, 10))
        close = tk.Label(inner, text="✕", bg=P["head"], fg=P["muted"], font=("Segoe UI", 10), cursor="hand2")
        close.place(relx=1.0, x=-10, y=8, anchor="ne")
        state = {"job": None}

        def shut(_=None):
            if t.winfo_exists():
                t.destroy()

        def click(_=None):
            shut()
            on_click()

        def later(ms):
            if state["job"]:
                t.after_cancel(state["job"])
            state["job"] = t.after(ms, shut)

        def hold(_=None):
            if state["job"]:
                t.after_cancel(state["job"])
                state["job"] = None
        for w in (t, inner, head, body, foot):
            w.bind("<Button-1>", click)
            w.configure(cursor="hand2")
        close.bind("<Button-1>", lambda e: (shut(), "break")[1])
        t.bind("<Enter>", hold)
        t.bind("<Leave>", lambda e: later(4_000))
        t.update_idletasks()
        w, h = max(360, t.winfo_reqwidth()), t.winfo_reqheight()
        right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 48
        try:                                             # the screen without the taskbar
            import ctypes
            from ctypes import wintypes
            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0):
                right, bottom = area.right, area.bottom
        except (AttributeError, OSError, ImportError, ValueError):
            pass
        t.geometry(f"{w}x{h}+{right - w - 16}+{bottom - h - 16}")
        t.deiconify()
        later(seconds * 1000)
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except (ImportError, RuntimeError):
            pass
        t.title_text, t.body_text, t.click = title, text, click     # for tests
        self.toast = t
        return t

    def _schedule(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.closing or self.paused:
            return
        self.after_id = self.root.after(max(MIN_INTERVAL, self.interval.get()) * 1000, self._tick)

    def _tick(self):
        self.after_id = None
        if self._provider():
            if self.mode_var.get() == XBOX_MODE:
                self._xbox_presence_round()
            elif self._game_allows_refresh():
                self.refresh_now(manual=False)
        self._schedule()

    def _xbox_presence_round(self):
        """'While Xbox says I play SoT': your own Xbox presence decides, so a console session counts too. Xbox is
        asked at most every PRESENCE_EVERY seconds; in between the last answer is used. The first round after you
        stop still runs, so the players of the last session are picked up."""
        if time.time() - self.presence_at < PRESENCE_EVERY and self.xbox_playing is not None:
            return self._after_presence(self.xbox_playing, None)
        if self.presence_busy:
            return
        self.presence_busy = True
        key = self.xkey.get()

        def work():
            try:
                playing, err = social.am_i_playing_sot(key), None
            except ProviderError as e:
                playing, err = None, str(e)
            except Exception as e:                   # never let a worker thread die silently
                playing, err = None, f"Unexpected error: {e}"
            self.q.put(("call", lambda: self._after_presence(playing, err, fresh=True)))
        threading.Thread(target=work, daemon=True).start()

    def _after_presence(self, playing, err, fresh=False):
        if fresh:
            self.presence_busy = False
            self.presence_at = time.time()
            self.xbox_playing = playing
        if self.mode_var.get() != XBOX_MODE or self.closing:
            return
        if err:                                      # can't tell: keep refreshing as before
            self.set_status(f"Couldn't ask Xbox whether you're playing ({err}). Refreshing anyway.", error=True)
            playing = True
        if playing or self.xbox_was_playing:
            self.xbox_was_playing = bool(playing)
            self.refresh_now(manual=False)
        else:
            self.set_status("Auto-refresh waiting: Xbox says you're not in Sea of Thieves. 'Refresh now' still works.")

    def _game_allows_refresh(self) -> bool:
        """With 'only while Sea of Thieves is open' on, auto-refresh skips rounds while the game is closed. The first
        round after the game closes still runs, so the players of the last session are picked up."""
        if self.mode_var.get() != REFRESH_MODES[0]:
            return True
        try:
            running = self.game_check()
        except Exception:
            return True                              # can't tell: keep refreshing as before
        if running or self.game_was_running:
            self.game_was_running = running
            return True
        self.set_status("Auto-refresh waiting: Sea of Thieves is not open. 'Refresh now' still works.")
        return False

    def _finish(self, out: "SyncOutcome | None", imported=False):
        if out is None:
            return self.set_status("A refresh is already running.")
        if not imported:
            self._set_busy("sync", False)
        if out.ok:
            self.db.apply_shared()                   # newly met players may be in the master list
            if not imported:
                self._crew_round()
                self.root.after(4000, self._discord_round)       # after the crew check, so crewmates stay out
            who = "" if imported or not self.xkey.active() else f" as {self.xkey.active()}"
            self.set_status(("Imported: " if imported else f"Synced{who}: ") + out.message)
        else:
            msg = out.message + ("  -> check or re-add the account key." if out.reauth else "")
            self.set_status(f"Refresh failed (existing data kept): {msg}", error=True)
        self.reload(to_top=self.to_top_after_sync)
        self.to_top_after_sync = False
        if out.ok and not imported:
            self._profiles_round(force=True)

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "sync":
                    self._finish(val)
                elif kind == "live":
                    msg, err = val
                    self.live_busy = False
                    self._set_busy("live", False)
                    done, total = self.live.coverage()
                    self.live_status.set(f"Live check: {done} of {total} recent players checked. {msg}")
                    self.reload()          # shows or removes the Twitch icons
                elif kind == "livecheck":
                    self._on_livecheck(*val)
                elif kind == "twitch":
                    self._on_twitch(*val)
                elif kind == "twitch_link":
                    self._on_twitch_link(*val)
                elif kind == "sot_streams":
                    self._on_sot_streams(*val)
                elif kind == "call":
                    val()
                elif kind == "twitch_setup":
                    self._on_twitch_setup(*val)
                elif kind == "acct_added":
                    self._set_busy("key", False)
                    name = self.xkey.add(*val)
                    self._refresh_accounts()
                    self.set_status(f"Added '{name}'. Click 'Refresh now'.")
                elif kind == "acct_err":
                    self._set_busy("key", False)
                    self.set_status(f"Could not add account: {val}", error=True)
                elif kind == "update":
                    info, err, manual = val
                    if err and manual:
                        self.set_status(f"Update check failed: {err}", error=True)
                    elif info:
                        self._offer_update(info)
                    elif manual:
                        self.set_status(f"You are up to date (version {__version__}).")
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(200, self._poll)

    # ---- Live now ----
    def live_check_now(self):
        if self.live_busy or not self.xkey.get():
            return
        self.live_busy = True
        self._set_busy("live", True)

        def work():
            try:
                self.q.put(("live", (self.live.run_cycle(), False)))
            except ProviderError as e:
                self.q.put(("live", (str(e), True)))
            except Exception as e:  # never let a worker thread die silently
                self.q.put(("live", (f"Unexpected error: {e}", True)))

        threading.Thread(target=work, daemon=True).start()

    def _schedule_live(self, delay_ms=300_000):
        if self.live_after_id:
            self.root.after_cancel(self.live_after_id)
            self.live_after_id = None
        if not self.closing:
            self.live_after_id = self.root.after(delay_ms, self._live_tick)

    def _live_tick(self):
        self.live_after_id = None
        if not self.paused and self.xkey.get():
            self.live_check_now()
        self._schedule_live()

    def _offer_update(self, info):
        text = f"Version {info.version} is available (you have {__version__}).\n\n{info.notes[:600]}"
        if not (info.exe_url and updater.running_exe()):        # running from source, or no program file
            if self.ask_yes("Update available", text, yes="Open release page"):
                webbrowser.open(info.url)
            return
        answer = self._question("Update available", text + "\n\nUpdate now downloads the new version from GitHub, "
                                "checks it and restarts the app.",
                                [("Later", None, "soft"), ("Release page", "page", "soft"),
                                 ("Update now", "update", "accent")])
        if answer == "update":
            self.update_now(info)
        elif answer == "page":
            webbrowser.open(info.url)

    def update_now(self, info):
        exe = updater.running_exe()
        if exe is None or "update" in self.busy:
            return
        self.set_status(f"Downloading version {info.version}...")
        self._set_busy("update", True)

        def work():
            try:
                updater.install(updater.download(info, exe), exe)
                err = None
            except updater.UpdateError as e:
                err = str(e)
            except Exception as e:
                err = f"Unexpected error: {e}"

            def done():
                self._set_busy("update", False)
                if err:
                    self.set_status(f"Update failed: {err}", error=True)
                    if self.ask_yes("Update failed", f"{err}\n\nOpen the release page instead?",
                                    yes="Open release page"):
                        webbrowser.open(info.url)
                    return
                self.set_status(f"Version {info.version} is installed; it is used from the next start of the app.")
                if self.ask_yes("Update installed", f"Version {info.version} is installed. Restart now?",
                                yes="Restart", no="Later"):
                    self.restart(exe)
            self.q.put(("call", done))

        threading.Thread(target=work, daemon=True).start()

    def restart(self, exe):
        try:
            subprocess.Popen([str(exe)], env=updater.restart_env(), close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except OSError as e:
            return self.set_status(f"Could not restart ({e}); start the app again yourself.", error=True)
        self.on_close()

    def _update_check(self, manual):
        try:
            self.q.put(("update", (updater.check(), None, manual)))
        except updater.UpdateError as e:
            self.q.put(("update", (None, str(e), manual)))

    def on_close(self):
        self.closing = True
        self.spinner.stop()
        if self.live_after_id:
            self.root.after_cancel(self.live_after_id)
            self.live_after_id = None
        if self.twitch_after_id:
            self.root.after_cancel(self.twitch_after_id)
            self.twitch_after_id = None
        if self.shared_after_id:
            self.root.after_cancel(self.shared_after_id)
            self.shared_after_id = None
        if self.msg_after_id:
            self.root.after_cancel(self.msg_after_id)
            self.msg_after_id = None
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.db.close()
        self.root.destroy()


def main():
    db = Database(data_dir() / "players.db")
    db.backup()
    root = tk.Tk()
    App(root, db)
    root.mainloop()
