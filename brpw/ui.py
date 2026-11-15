"""Tkinter UI. All network/DB-heavy work runs in worker threads; results return via a queue."""
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import __version__, direct, social, sotrep, updater
from .db import Database
from .paths import data_dir
from .providers import (ManualImportProvider, OpenXblKey, OpenXblProvider, ProviderError,
                        find_gamertag, lookup_gamertag)
from .game import game_running
from .msauth import MARK, MsAccounts
from .shared import SharedConfig, http_rest
from .shared_ui import SHARED_MARK, SharedListMixin
from .live import HOURLY_BUDGET, LIVE_SCOPE, LiveChecker
from .sync import SyncOutcome, SyncService
from .themes import THEMES, clamp, icon_path, palette, twitch_icon_path
from .twitch import (CHECK_EVERY, CONSOLE_URL, STATUS_MAX_AGE, TwitchChecker, TwitchClient, TwitchCreds,
                     login_candidates)

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


class App(SharedListMixin):
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
        self.toast = None
        self.inbox_win = None
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()
        self.root.after(200, self._poll)
        self._schedule()
        self._schedule_live(15_000)
        self._schedule_twitch(20_000)
        self._schedule_shared(10_000)
        self._schedule_messages(8_000)
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
        if hasattr(self, "tree"):
            self.tree.tag_configure("friend", background=P["friend"])
            self.tree.tag_configure("odd", background=P["stripe"])
            self.status_lbl.configure(foreground="#ff7b72" if self.status_error else FG)
            self.legend.configure(bg=P["friend"], fg=FG)
            self.spinner.recolor(acc, BG)

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
        r.minsize(1040, 400)
        self._apply_style()
        self._apply_icon()

        # ---- top bar: account and refresh on the left, the main actions on the right, the rest in the ☰ menu
        bar = ttk.Frame(r, style="Bar.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        self.acct_var = tk.StringVar()
        self.acct_box = ttk.Combobox(bar, textvariable=self.acct_var, state="readonly", width=20)
        self.acct_box.pack(side="left")
        self.acct_box.bind("<<ComboboxSelected>>", lambda e: self.switch_account())
        ttk.Button(bar, text="↻  Refresh", style="Accent.TButton", command=self.refresh_now).pack(side="left",
                                                                                                padx=(8, 0))
        ttk.Label(bar, text="Auto", style="BarHint.TLabel").pack(side="left", padx=(16, 4))
        self.mode_var = tk.StringVar(value=REFRESH_MODES[2] if self.paused else
                                     XBOX_MODE if self.db.get_setting("refresh_xbox", "0") == "1" else
                                     REFRESH_MODES[0] if self.db.get_setting("only_with_game", "1") == "1"
                                     else REFRESH_MODES[1])
        mb = ttk.Combobox(bar, textvariable=self.mode_var, state="readonly", width=24,
                          values=(REFRESH_MODES[0], XBOX_MODE, REFRESH_MODES[1], REFRESH_MODES[2]))
        mb.pack(side="left")
        mb.bind("<<ComboboxSelected>>", lambda e: self.set_refresh_mode(self.mode_var.get()))
        ttk.Label(bar, text="every", style="BarHint.TLabel").pack(side="left", padx=(8, 4))
        self.interval = tk.IntVar(value=int(self.db.get_setting("interval", "60")))
        sp = ttk.Spinbox(bar, from_=MIN_INTERVAL, to=3600, increment=10, width=4, textvariable=self.interval,
                         command=self._save_interval)
        sp.pack(side="left")
        sp.bind("<FocusOut>", lambda e: self._save_interval())
        ttk.Label(bar, text="s", style="BarHint.TLabel").pack(side="left", padx=(3, 0))
        self.auto_upd = tk.BooleanVar(value=self.db.get_setting("auto_update_check", "1") == "1")
        self.menu_btn = ttk.Button(bar, text="☰", width=3, style="Tool.TButton", command=self._main_menu)
        self.menu_btn.pack(side="right", padx=(4, 0))
        for text, cmd in (("Live streams", self.sot_streams), ("Check live", self.check_live_all),
                          ("Messages", self.inbox_dialog), ("Friends", self.friends_dialog)):
            b = ttk.Button(bar, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="right", padx=2)
            if text == "Messages":
                self.msg_btn = b
        self.msg_popups = tk.BooleanVar(value=self.db.get_setting("message_popups", "1") == "1")
        self._refresh_accounts()

        self.players_tab = ttk.Frame(r)
        self.players_tab.pack(fill="both", expand=True)
        try:
            self.twitch_img = tk.PhotoImage(file=str(twitch_icon_path()))
        except tk.TclError:
            self.twitch_img = tk.PhotoImage(width=1, height=1)   # missing asset must never stop the app

        bar2 = ttk.Frame(self.players_tab)
        bar2.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(bar2, text="Search", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.reload())
        ttk.Entry(bar2, textvariable=self.search).pack(side="left", fill="x", expand=True)
        ttk.Button(bar2, text="Reset table", command=self.reset_table).pack(side="left", padx=(6, 0))

        table = ttk.Frame(self.players_tab)
        table.pack(fill="both", expand=True, padx=10)
        cols = ("gamertag", "xuid", "tags", "twitch", "last_seen", "origin", "current")
        self.tree = ttk.Treeview(table, columns=cols, show="tree headings", selectmode="extended")
        self.col_titles = {"gamertag": "Gamertag", "xuid": "XUID", "tags": "Tags", "twitch": "Twitch",
                           "last_seen": "Last seen", "origin": "Source", "current": "#"}
        self.sort_col, self.sort_desc = "current", False  # Xbox list order (#1 on top), then older history
        self.tree.column("#0", width=34, minwidth=34, stretch=False, anchor="center")  # Twitch icon, only when live
        self.tree.heading("#0", text="")
        for c, w in self.COL_WIDTHS.items():
            self.tree.heading(c, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, minwidth=30, anchor="center" if c == "current" else "w",
                             stretch=c not in ("origin", "current"))
        self._apply_hidden_columns()
        self._update_headings()
        # ttk: the tag created FIRST wins when a row has several, so "friend" must be created before "odd"
        self.tree.tag_configure("friend", background=self.pal["friend"])
        self.tree.tag_configure("odd", background=self.pal["stripe"])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: None if self._twitch_cell(e) else self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        # per-player actions live in the right-click menu; whole-list actions are in the top bar
        self.tree.bind("<Button-3>", self._row_menu)
        self.tree.bind("<Shift-F10>", self._row_menu)
        self.tree.bind("<App>", self._row_menu)

        # ---- status bar: what's happening on the left, legend and count on the right
        self.status = tk.StringVar()
        self.live_status = tk.StringVar(value="")
        statusbar = ttk.Frame(r)
        statusbar.pack(fill="x", padx=10, pady=(6, 8))
        self.spinner = Spinner(statusbar, color=self.pal["accent"], bg=self.pal["bg"])
        self.spinner.pack(side="left", padx=(0, 6))
        self.count_lbl = ttk.Label(statusbar, text="")
        self.count_lbl.pack(side="right")
        legend = tk.Label(statusbar, text=" Friend ", bg=self.pal["friend"], fg=self.pal["fg"], font=("Segoe UI", 8))
        legend.pack(side="right", padx=(0, 10))
        self.legend = legend
        ttk.Label(statusbar, image=self.twitch_img, text=" live now", compound="left",
                  style="Hint.TLabel").pack(side="right", padx=(0, 10))
        ttk.Label(statusbar, text="Right-click a player for more", style="Hint.TLabel").pack(side="right",
                                                                                            padx=(0, 14))
        self.status_lbl = ttk.Label(statusbar, textvariable=self.status, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.set_status("Ready. Add an Xbox account (☰ menu) or import players.")

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
        "current": lambda a: (0, a["pos"]) if a["pos"] is not None else (1, -a["last_seen"]),
    }

    COL_WIDTHS = {"gamertag": 170, "xuid": 130, "tags": 110, "twitch": 190, "last_seen": 115, "origin": 55,
                  "current": 44}

    def reset_table(self):
        """Back to the default view: Xbox list order, no search, all columns shown at their normal width."""
        self.sort_col, self.sort_desc = "current", False
        self.db.set_setting("hidden_columns", "")
        self._apply_hidden_columns()
        for c, w in self.COL_WIDTHS.items():
            self.tree.column(c, width=w)
        self._update_headings()
        self.search.set("")                       # also reloads
        self.reload(to_top=True)

    def sort_by(self, col):
        self.sort_desc = not self.sort_desc if col == self.sort_col else (col == "last_seen")
        self.sort_col = col
        self._update_headings()
        self.reload()

    def _update_headings(self):
        for c, t in self.col_titles.items():
            arrow = (" ▼" if self.sort_desc else " ▲") if c == self.sort_col else ""
            self.tree.heading(c, text=t + arrow)

    def reload(self, to_top=False):
        sel = set() if to_top else {self.tree.set(i, "xuid") + "|" + self.tree.set(i, "gamertag")
                                    for i in self.tree.selection()}
        top = 0.0 if to_top else self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        live_ids = {r["id"] for r in self.live.live_now()}   # Xbox says broadcasting (fresh results only)
        now = time.time()
        rows = sorted(self.db.list_accounts(self.search.get(), self.xkey.active()),
                      key=self.SORT_KEYS[self.sort_col], reverse=self.sort_desc)
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
            iid = self.tree.insert(
                "", "end", iid=str(a["id"]), image=self.twitch_img if (a["id"] in live_ids or tw_live) else "",
                tags=(("friend",) if friend else ()) + (("odd",) if n % 2 else ()), values=(
                    a["gamertag"], a["xuid"] or "", a["tags"] or "", tw_text, fmt_ts(a["last_seen"]),
                    "Live" if a["source"] in ("xbox-live", "openxbl") else "Import",
                    str(a["pos"] + 1) if a["pos"] is not None else ""))
            if (a["xuid"] or "") + "|" + a["gamertag"] in sel:
                self.tree.selection_add(iid)
        self.tree.yview_moveto(top)  # a background refresh must not scroll the list back to the top
        self.count_lbl.configure(text=f"{self.db.count_accounts()} unique accounts")

    def _twitch_cell(self, event):
        """The row id when the click was on the live icon or in the Twitch column, else None."""
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row:
            return None
        if col == "#0":
            return row
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
        elif self.tree.item(row, "image"):
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
            ("Invite / join in Game Bar", self.invite_or_join, one),
            (None, None, True),
            ("Tag...", self.tag_selected, True),
            ("Untag", self.untag_selected, True),
            (None, None, True),
            ("Link Twitch channel...", self.link_twitch_selected, one),
            ("Allow linking again" if all_never else "Never link...", self.never_link_selected, True),
        ] + ([(None, None, True), ("Remove from master list", self.remove_from_master, True)]
             if self.is_list_owner() else [])

    HIDEABLE = ("xuid", "tags", "twitch", "last_seen", "origin", "current")

    def _apply_hidden_columns(self):
        hidden = set(filter(None, self.db.get_setting("hidden_columns", "").split(",")))
        self.tree["displaycolumns"] = [c for c in self.tree["columns"] if c not in hidden]

    def toggle_column(self, col):
        hidden = set(filter(None, self.db.get_setting("hidden_columns", "").split(",")))
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

    def _main_menu(self):
        """☰: accounts, import/export, Twitch and shared list, theme and updates."""
        m = self._menu()
        has = "normal" if self.xkey.active() else "disabled"
        m.add_command(label="Add Xbox account...", command=self.set_key)
        m.add_command(label="Remove this account", command=self.remove_account, state=has)
        m.add_command(label="Switch Game Bar to this account", command=self.switch_game_bar, state=has)
        m.add_checkbutton(label="Pop-up for new Xbox messages", variable=self.msg_popups,
                          command=self.set_message_popups)
        m.add_separator()
        m.add_command(label="Import players...", command=self.import_dialog)
        m.add_command(label="Export...", command=self.export)
        m.add_separator()
        m.add_command(label="Shared Twitch list...", command=self.shared_dialog)
        m.add_command(label="Twitch setup...", command=self.twitch_setup_dialog)
        m.add_separator()
        themes = self._menu(m)
        self.theme_choice = tk.IntVar(value=self.theme)
        for i, t in enumerate(THEMES, 1):
            themes.add_radiobutton(label=t[0], value=i, variable=self.theme_choice,
                                   command=lambda i=i: self.set_theme(i))
        m.add_cascade(label="Theme", menu=themes)
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
        self._set_busy("social", True)

        def run():
            try:
                res, err = work(key), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:                  # never let a worker thread die silently
                res, err = None, f"Unexpected error: {e}"
            self.q.put(("call", lambda: (self._set_busy("social", False), done(res, err))))
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
            self.set_status(f"Added {gt} as a friend on Xbox. It becomes mutual once they add you back.")
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
        if not messagebox.askyesno("Remove friend", f"Remove {gt} from your Xbox friends?"):
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
        """Game Bar uses the Xbox app's Windows sign-in, which only Microsoft's apps can change. Open the Xbox app's
        sign-in screen and copy the selected account's gamertag; the user picks that account in Microsoft's picker."""
        name = self.xkey.active()
        if not name:
            return self.set_status("Add an Xbox account first.", error=True)
        self.root.clipboard_clear()
        self.root.clipboard_append(name)
        if social.open_xbox_sign_in():
            self.set_status(f"Xbox sign-in opened ({name} copied). Sign in as {name}: Game Bar uses the same sign-in. "
                            "Accounts added in Windows Settings > Accounts > Email & accounts need no password.")
        else:
            self.set_status(f"Couldn't open the Xbox app ({name} copied). In the Xbox app, click your profile > "
                            "Sign out, then sign in as {name}.".replace("{name}", name), error=True)

    def invite_or_join(self, gt=None):
        """Xbox only offers game invites and joining inside its own UI: open Game Bar's Xbox Social widget and put
        the gamertag on the clipboard to find the player there."""
        if gt is None:
            p = self._one_player()
            if not p:
                return self.set_status("Select one player first.", error=True)
            gt = p[0]
        self.root.clipboard_clear()
        self.root.clipboard_append(gt)
        if social.open_gamebar_social():
            self.set_status(f"Game Bar opened ({gt} copied). In Xbox Social, right-click {gt}: Invite to game / "
                            "Join game. Works while you play, if the game and their privacy settings allow it.")
        else:
            self.set_status(f"Couldn't open Game Bar ({gt} copied). Press Win+G, open Xbox Social and right-click "
                            "the player: Invite to game / Join game.", error=True)

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
        win = tk.Toplevel(self.root)
        win.title(f"Messages: {gt}")
        win.configure(bg=self.pal["bg"])
        panel = self._chat_panel(win, gt, xuid)
        panel.pack(fill="both", expand=True)
        win.send, win.box, win.info, win.texts = panel.send, panel.box, panel.info, panel.texts   # kept for tests
        panel.box.focus_set()
        self._place_dialog(win, 640, 620)
        return win

    def _chat_panel(self, parent, gt, xuid, on_sent=None):
        """Your Xbox message history with one player as bubbles (yours right, theirs left) and a box to write a new
        one. Nothing is sent until Send (or Ctrl+Enter). Used by the chat window and the Messages window."""
        pal = self.pal
        panel = tk.Frame(parent, bg=pal["bg"])
        head = tk.Frame(panel, bg=pal["head"])
        head.pack(fill="x")
        tk.Label(head, text=gt, bg=pal["head"], fg=pal["fg"], font=("Segoe UI", 13, "bold")).pack(
            side="left", padx=(14, 6), pady=10)
        info = tk.StringVar(value="Loading your messages...")
        tk.Label(head, textvariable=info, bg=pal["head"], fg=pal["muted"], font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(head, text="Xbox profile", style="Tool.TButton",
                   command=lambda: self.open_xbox_profile(gt, xuid)).pack(side="right", padx=(4, 12))
        ttk.Button(head, text="Invite / join in Game Bar", style="Tool.TButton",
                   command=lambda: self.invite_or_join(gt)).pack(side="right")

        # scrollable message area: a frame inside a canvas, kept as wide as the canvas
        area = tk.Frame(panel, bg=pal["bg"])
        area.pack(fill="both", expand=True)
        canvas = tk.Canvas(area, bg=pal["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=pal["bg"])
        inner_id = canvas.create_window(0, 0, window=inner, anchor="nw")
        bubbles = []

        def on_resize(e):
            canvas.itemconfigure(inner_id, width=e.width)
            for b in bubbles:
                b.configure(wraplength=max(160, int(e.width * 0.62)))
        canvas.bind("<Configure>", on_resize)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def wheel(e):
            canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
        # the wheel scrolls the chat only while the mouse is over it (a list next to it keeps its own scrolling)
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        compose = tk.Frame(panel, bg=pal["panel"])
        compose.pack(fill="x")
        send_btn = ttk.Button(compose, text="Send", width=8, style="Accent.TButton")
        send_btn.pack(side="right", padx=(0, 12), pady=10)             # packed first so the box can't squeeze it
        box = tk.Text(compose, height=3, wrap="word", bg=pal["bg"], fg=pal["fg"], insertbackground=pal["fg"],
                      relief="flat", font=("Segoe UI", 10), padx=8, pady=6)
        box.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=10)
        state = tk.StringVar()
        hint = f"Ctrl+Enter sends · at most {social.MAX_MESSAGE} characters · non-friends get it as a message request"
        tk.Label(panel, textvariable=state, bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=12, pady=(0, 6))
        state.set(hint)
        box.bind("<KeyRelease>", lambda e: state.set(
            f"{len(box.get('1.0', 'end').strip())} / {social.MAX_MESSAGE}  ·  Ctrl+Enter sends"))

        def day_label(at):
            d = time.localtime(at)
            today = time.localtime()
            yesterday = time.localtime(time.time() - 86400)
            if d[:3] == today[:3]:
                return "Today"
            if d[:3] == yesterday[:3]:
                return "Yesterday"
            return time.strftime("%A %d %B" + (" %Y" if d.tm_year != today.tm_year else ""), d)

        def add_line(text):
            tk.Label(inner, text=text, bg=pal["bg"], fg=pal["muted"], font=("Segoe UI", 8)).pack(pady=(10, 2))

        def add_bubble(m):
            mine = m["sender"] != xuid
            row = tk.Frame(inner, bg=pal["bg"])
            row.pack(fill="x", padx=12, pady=3)
            side = "e" if mine else "w"
            b = tk.Label(row, text=m["text"], justify="left", anchor="w", font=("Segoe UI", 10),
                         bg=pal["accent"] if mine else pal["friend"], fg=pal["bg"] if mine else pal["fg"],
                         padx=12, pady=7, wraplength=max(160, int(canvas.winfo_width() * 0.62)))
            b.pack(anchor=side)
            if m["at"]:
                tk.Label(row, text=time.strftime("%H:%M", time.localtime(m["at"])), bg=pal["bg"], fg=pal["muted"],
                         font=("Segoe UI", 7)).pack(anchor=side, padx=4)
            bubbles.append(b)

        def show(res, err):
            if not panel.winfo_exists():
                return
            if err:
                return info.set(f"Could not load the messages: {err}")
            self._chat_opened(xuid)
            for w in inner.winfo_children():
                w.destroy()
            bubbles.clear()
            day = None
            for m in res["messages"]:
                d = day_label(m["at"]) if m["at"] else ""
                if d and d != day:
                    add_line(d)
                    day = d
                add_bubble(m)
            if not res["messages"]:
                add_line(f"No messages with {gt} yet. Say ahoy!")
            n = len(res["messages"])
            info.set((f"{n} message(s)" if n else "no messages yet")
                     + (" · only the newest (older ones can't be fetched)" if res["more"] else "")
                     + f" · as {self.xkey.active() or 'your account'}")
            panel.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(1.0)

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
        send_btn.configure(command=send)
        box.bind("<Control-Return>", lambda e: (send(), "break")[1])
        panel.send, panel.box, panel.info, panel.load = send, box, info, load
        panel.texts = lambda: [b.cget("text") for b in bubbles]
        load()
        return panel

    def inbox_dialog(self, goto=None):
        """Messages: all your Xbox conversations on the left (newest first, unread marked, who is online), the chat
        with the selected one on the right. Typing filters by name; Enter finds anyone on Xbox to start a new chat.
        Needs an account signed in with Microsoft (OpenXBL's inbox endpoints don't work)."""
        if self.inbox_win is not None and self.inbox_win.winfo_exists():    # one Messages window: bring it up
            w = self.inbox_win
            w.deiconify()
            w.lift()
            w.focus_force()
            w.goto(goto)
            return w
        P = self.pal
        win = tk.Toplevel(self.root)
        self.inbox_win = win
        win.title("Messages")
        win.configure(bg=P["bg"])
        head = tk.Frame(win, bg=P["head"])
        head.pack(fill="x")
        top = tk.Frame(head, bg=P["head"])
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="Messages", bg=P["head"], fg=P["fg"], font=("Segoe UI", 14, "bold")).pack(side="left")
        info = tk.StringVar(value="Loading your inbox...")
        tk.Label(top, textvariable=info, bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left",
                                                                                                 padx=(10, 0))
        ttk.Button(top, text="↻  Refresh", style="Tool.TButton", command=lambda: load()).pack(side="right")

        body = tk.Frame(win, bg=P["bg"])
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=P["panel"], width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        query = tk.StringVar()
        entry = ttk.Entry(left, textvariable=query, font=("Segoe UI", 10))
        entry.pack(fill="x", padx=10, pady=10)
        lst = tk.Frame(left, bg=P["panel"])
        lst.pack(fill="both", expand=True)
        tv = ttk.Treeview(lst, columns=("when",), show="tree", selectmode="browse", style="Inbox.Treeview")
        tv.column("#0", width=250)
        tv.column("when", width=62, anchor="e", stretch=False)
        tv.tag_configure("unread", foreground=P["fg"], font=("Segoe UI", 9, "bold"))
        tv.tag_configure("read", foreground=P["fg"])
        tv.tag_configure("preview", foreground=P["muted"], font=("Segoe UI", 8))
        tv.tag_configure("section", foreground=P["muted"], font=("Segoe UI", 8, "bold"))
        sb = ttk.Scrollbar(lst, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=P["bg"])
        right.pack(side="left", fill="both", expand=True)
        empty = tk.Label(right, text="Pick a conversation", bg=P["bg"], fg=P["muted"], font=("Segoe UI", 11))
        empty.pack(expand=True)

        chats, rows, state = [], {}, {"results": None, "term": "", "placeholder": False, "panel": None,
                                      "open": None, "goto": goto}
        hint = "Search chats, or Enter to find anyone on Xbox"

        def typed():
            return "" if state["placeholder"] else query.get().strip()

        def when(ts):
            if not ts:
                return ""
            d, now = time.localtime(ts), time.localtime()
            if d[:3] == now[:3]:
                return time.strftime("%H:%M", d)
            if time.time() - ts < 6 * 86400:
                return time.strftime("%a", d)
            return time.strftime("%d %b" + (" %Y" if d.tm_year != now.tm_year else ""), d)

        def add_chat(c):
            online = c.get("state", "").casefold() == "online"
            name = c["gamertag"] if not c["gamertag"].isdigit() else f"Unknown player ({c['gamertag']})"
            iid = tv.insert("", "end", text=("●  " if online else "    ") + name, values=(when(c.get("at")),),
                            tags=("unread" if c.get("unread") else "read",), open=True)
            preview = (("You: " if c.get("mine") else "") + (c.get("preview") or "")).replace("\n", " ")
            if preview:
                tv.insert(iid, "end", text="     " + preview[:46] + ("…" if len(preview) > 46 else ""),
                          tags=("preview",))
            rows[iid] = c

        def render():
            tv.delete(*tv.get_children())
            rows.clear()
            q = " ".join(typed().casefold().split())
            shown = [c for c in chats if not q or q in c["gamertag"].casefold()]
            for c in shown:
                add_chat(c)
            if state["results"] is not None:
                tv.insert("", "end", text=f"NEW CHAT — \"{state['term']}\"", tags=("section",))
                have = {c["xuid"] for c in chats}
                extra = [dict(r, preview="", at=None, mine=False, unread=False) for r in state["results"]
                         if r["xuid"] not in have]
                for c in extra:
                    add_chat(c)
                if not extra:
                    tv.insert("", "end", text="    nobody else found", tags=("preview",))
            elif q and not shown:
                tv.insert("", "end", text="    no chat matches: Enter searches Xbox", tags=("preview",))
            if state["open"]:                                  # keep the open conversation selected
                for iid, c in rows.items():
                    if c["xuid"] == state["open"]:
                        tv.selection_set(iid)
                        tv.see(iid)
                        break

        def open_chat(c):
            if state["open"] == c["xuid"] and state["panel"] is not None:
                return
            if state["panel"] is not None:
                state["panel"].destroy()
            empty.pack_forget()
            state["open"] = c["xuid"]
            name = c["gamertag"] if not c["gamertag"].isdigit() else "Unknown player"
            state["panel"] = self._chat_panel(right, name, c["xuid"], on_sent=load)
            state["panel"].pack(fill="both", expand=True)
            win.panel = state["panel"]

        def on_select(_=None):
            sel = tv.selection()
            if not sel:
                return
            iid = sel[0]
            if iid not in rows:                                # a preview line: open its conversation
                iid = tv.parent(iid)
            if iid in rows:
                open_chat(rows[iid])

        tv.bind("<<TreeviewSelect>>", on_select)

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
                summary()
                render()
            self._xbox_bg(lambda k: social.search_players(k, term), found)
        entry.bind("<Return>", search)
        entry.bind("<Escape>", lambda e: query.set(""))

        def placeholder(on):                                   # grey hint text while the box is empty
            if on and not query.get():
                state["placeholder"] = True
                entry.configure(foreground=P["muted"])
                query.set(hint)
            elif not on and state["placeholder"]:
                query.set("")
                state["placeholder"] = False
                entry.configure(foreground=P["fg"])
        entry.bind("<FocusIn>", lambda e: placeholder(False))
        entry.bind("<FocusOut>", lambda e: placeholder(True))

        def summary():
            unread = sum(1 for c in chats if c.get("unread"))
            info.set(f"{len(chats)} conversations" + (f" · {unread} unread" if unread else "")
                     + f" · {self.xkey.active() or ''}")

        def show(res, err):
            if not win.winfo_exists():
                return
            if err:
                return info.set(err)
            chats[:] = res["chats"]
            summary()
            if res["groups"]:
                info.set(info.get() + f" · {res['groups']} group chat(s) not shown")
            render()
            if state["goto"]:                                  # opened from a pop-up: show that chat
                for iid, c in rows.items():
                    if c["xuid"] == state["goto"]:
                        tv.selection_set(iid)
                        tv.see(iid)
                        break
                state["goto"] = None

        def load():
            self._xbox_bg(social.inbox, show)

        def goto_chat(xuid):
            state["goto"] = xuid
            load()
        win.goto = goto_chat
        placeholder(True)
        win.inbox_tv, win.info, win.query, win.search, win.entry = tv, info, query, search, entry   # for tests
        win.rows = lambda: [(tv.item(i, "text").strip(" ●"), tv.item(i, "values")[0]) for i in rows]
        win.open = lambda gt: [tv.selection_set(i) for i, c in rows.items() if c["gamertag"] == gt]
        win.panel = None
        self._place_dialog(win, 1000, 660)
        load()
        return win

    def friends_dialog(self):
        """Your Xbox friends (online first, what they are doing) and a search for anyone on Xbox. Typing filters the
        friends; Enter searches Xbox. Loading the friends also refreshes the Friend highlight in the main list."""
        P = self.pal
        win = tk.Toplevel(self.root)
        win.title("Friends")
        win.configure(bg=P["bg"])
        head = tk.Frame(win, bg=P["head"])
        head.pack(fill="x")
        top = tk.Frame(head, bg=P["head"])
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="Friends", bg=P["head"], fg=P["fg"], font=("Segoe UI", 14, "bold")).pack(side="left")
        info = tk.StringVar(value="Loading from Xbox...")
        tk.Label(top, textvariable=info, bg=P["head"], fg=P["muted"], font=("Segoe UI", 9)).pack(side="left",
                                                                                                 padx=(10, 0))
        ttk.Button(top, text="↻  Refresh", style="Tool.TButton", command=lambda: load()).pack(side="right")
        query = tk.StringVar()
        entry = ttk.Entry(head, textvariable=query, font=("Segoe UI", 10))
        entry.pack(fill="x", padx=14, pady=(0, 12))
        hint = "Search your friends, or press Enter to find anyone on Xbox"

        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        tv = ttk.Treeview(box, columns=("status", "relation"), show="tree", height=10, selectmode="browse")
        tv.column("#0", width=230, stretch=False)
        tv.column("status", width=300, anchor="w")
        tv.column("relation", width=110, anchor="e", stretch=False)
        tv.tag_configure("section", foreground=P["muted"], font=("Segoe UI", 8, "bold"))
        tv.tag_configure("online", foreground=P["fg"])
        tv.tag_configure("offline", foreground=P["muted"])
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)

        friends, people, state = [], {}, {"results": None, "term": "", "placeholder": False}

        def typed():                                           # what was typed (not the grey hint)
            return "" if state["placeholder"] else query.get().strip()

        def ago(ts):
            s = max(0, int(time.time() - ts))
            return ("just now" if s < 120 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago"
                    if s < 86400 else f"{s // 86400} days ago" if s < 86400 * 60 else time.strftime("%d %b %Y",
                                                                                                    time.localtime(ts)))

        def add_row(f):
            online = f["state"].casefold() == "online"
            status = (f"●  {f['text'] or 'Online'}" if online else
                      f"last seen {ago(f['last_seen'])}" if f.get("last_seen") else
                      (f["text"] or ("" if f["state"] == "Unknown" else f["state"])))
            iid = tv.insert("", "end", text=("★  " if f["favorite"] else "    ") + f["gamertag"],
                            values=(status, f["relation"] if f["relation"] != "Friend" else ""),
                            tags=("online" if online else "offline",))
            people[iid] = f

        def section(title):
            tv.insert("", "end", text=title, tags=("section",))

        def render():
            tv.delete(*tv.get_children())
            people.clear()
            q = " ".join(typed().casefold().split())
            shown = [f for f in friends if not q or q in f["gamertag"].casefold()]
            on = [f for f in shown if f["state"].casefold() == "online"]
            off = [f for f in shown if f["state"].casefold() != "online"]
            if on:
                section(f"ONLINE — {len(on)}")
                for f in on:
                    add_row(f)
            if off:
                section(f"OFFLINE — {len(off)}")
                for f in off:
                    add_row(f)
            if state["results"] is not None:
                section(f"ON XBOX — \"{state['term']}\"")
                mine = {f["xuid"] for f in friends}
                extra = [f for f in state["results"] if f["xuid"] not in mine]
                for f in extra:
                    add_row(f)
                if not extra:
                    tv.insert("", "end", text="    nobody else found", tags=("offline",))
            elif q and not shown:
                tv.insert("", "end", text="    no friend matches: press Enter to search Xbox", tags=("offline",))
            update_buttons()

        def chosen():
            sel = tv.selection()
            return people.get(sel[0]) if len(sel) == 1 else None

        def with_one(fn):
            f = chosen()
            if f is None:
                return info.set("Select a player first.")
            fn(f)

        def following(f):
            return f["relation"] in self.FOLLOWING

        def toggle_friend(f):
            if following(f):
                self.remove_friend_selected(f["gamertag"], f["xuid"], after=load)
            else:
                self.add_friend_selected(f["gamertag"], f["xuid"], after=load)

        bar = ttk.Frame(win, style="Bar.TFrame", padding=(10, 8))
        bar.pack(fill="x", pady=(8, 0))
        actions = (("Messages", lambda f: self.message_dialog(f["gamertag"], f["xuid"])),
                   ("Invite / join in Game Bar", lambda f: self.invite_or_join(f["gamertag"])),
                   ("Xbox profile", lambda f: self.open_xbox_profile(f["gamertag"], f["xuid"])),
                   ("SOTREP", lambda f: webbrowser.open(sotrep.profile_url(f["gamertag"]))))
        buttons = []
        for i, (text, fn) in enumerate(actions):
            b = ttk.Button(bar, text=text, style="Accent.TButton" if i == 0 else "Tool.TButton",
                           command=lambda fn=fn: with_one(fn))
            b.pack(side="left", padx=(0, 4))
            buttons.append(b)
        friend_btn = ttk.Button(bar, text="Add friend", style="Tool.TButton", command=lambda: with_one(toggle_friend))
        friend_btn.pack(side="right")

        def update_buttons(*_):
            f = chosen()
            for b in buttons + [friend_btn]:
                b.state(["!disabled"] if f else ["disabled"])
            friend_btn.configure(text="Remove friend" if f and following(f) else "Add friend")

        def row_menu(e):
            row = tv.identify_row(e.y)
            if row not in people:
                return
            tv.selection_set(row)
            f = people[row]
            m = self._menu(win)
            for text, fn in actions:
                m.add_command(label=text, command=lambda fn=fn: fn(f))
            m.add_separator()
            m.add_command(label="Remove friend..." if following(f) else "Add friend", command=lambda: toggle_friend(f))
            win.row_menu = m
            m.tk_popup(e.x_root, e.y_root)

        tv.bind("<<TreeviewSelect>>", update_buttons)
        tv.bind("<Double-1>", lambda e: with_one(actions[0][1]))
        tv.bind("<Return>", lambda e: with_one(actions[0][1]))
        tv.bind("<Button-3>", row_menu)

        def on_type(*_):
            if state["placeholder"]:
                return
            if state["results"] is not None and typed() != state["term"]:
                state["results"] = None                       # a new search term: back to the friends
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
                summary()
                render()
            self._xbox_bg(lambda k: social.search_players(k, term), found)
        entry.bind("<Return>", search)
        entry.bind("<Escape>", lambda e: query.set(""))

        def summary():
            n_on = sum(1 for f in friends if f["state"].casefold() == "online")
            info.set(f"{n_on} online · {len(friends)} in your list · {self.xkey.active() or ''}")

        def show(res, err):
            if not win.winfo_exists():
                return
            if err:
                return info.set(f"Could not load your friends: {err}")
            friends[:] = res
            summary()
            render()
            owner = self.xkey.active()
            if owner:
                self.db.replace_friends(owner, {f["xuid"]: f["relation"] for f in res})
                self.reload()

        def load():
            info.set("Loading from Xbox...")
            self._xbox_bg(social.friend_list, show)

        def placeholder(on):                                   # grey hint text while the box is empty
            if on and not query.get():
                state["placeholder"] = True
                entry.configure(foreground=P["muted"])
                query.set(hint)
            elif not on and state["placeholder"]:
                query.set("")
                state["placeholder"] = False
                entry.configure(foreground=P["fg"])
        entry.bind("<FocusIn>", lambda e: placeholder(False))
        entry.bind("<FocusOut>", lambda e: placeholder(True))
        placeholder(True)

        win.friends_tv, win.show, win.info, win.query, win.search = tv, show, info, query, search   # for tests
        win.rows = lambda: [(tv.item(i, "text").strip(" ★"),) + tuple(tv.item(i, "values")) for i in people]
        win.select = lambda gt: [tv.selection_set(i) for i, f in people.items() if f["gamertag"] == gt]
        win.friend_btn, win.entry = friend_btn, entry
        self._place_dialog(win, 700, 560)
        load()
        return win

    def _place_dialog(self, win, width=None, height=None):
        """Centre a dialog over the main window and bring it to the front. Without this Windows may put a
        new Toplevel at the screen's top-left corner, out of sight. Dialogs never grab input, so a dialog
        that is somehow hidden can not freeze the main window."""
        win.update_idletasks()
        w, h = width or win.winfo_reqwidth(), height or win.winfo_reqheight()
        r = self.root
        x = r.winfo_rootx() + max(0, (r.winfo_width() - w) // 2)
        y = r.winfo_rooty() + max(0, (r.winfo_height() - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.lift()
        win.focus_force()

    def _ask_tag(self, title, prompt, values, editable):
        win = tk.Toplevel(self.root)
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
            return messagebox.showinfo("Tag", "Select one or more players first.")
        tag = self._ask_tag("Tag players", f"Tag {len(ids)} selected player(s) with (pick one or type a new tag):",
                            self.db.all_tags(), True)
        if tag:
            self.db.add_tag(ids, tag)
            self.reload()

    def untag_selected(self):
        ids = self._selected_ids()
        have = self.db.tags_for(ids)
        if not ids or not have:
            return messagebox.showinfo("Remove tag", "Select players that have a tag first.")
        tag = self._ask_tag("Remove tag", "Remove which tag from the selected player(s)?", have, False)
        if tag:
            self.db.remove_tag(ids, tag)
            self.reload()

    def encounters_dialog(self):
        ids = self._selected_ids()
        if not ids:
            return messagebox.showinfo("Encounters", "Select one or more players first.")
        owner = self.xkey.active()
        details = self.db.account_details(ids, owner)
        rows = self.db.encounters_for_many(ids)
        win = tk.Toplevel(self.root)
        win.title(f"Players: {len(ids)} selected, {len(rows)} recorded encounter(s)")
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
        win = tk.Toplevel(self.root)
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
            return messagebox.showinfo("Twitch", "Select exactly one player to link a Twitch channel.")
        if not self.tcreds.configured():
            messagebox.showinfo("Twitch", "Twitch is not set up yet. Opening 'Twitch setup...'.")
            return self.twitch_setup_dialog()
        aid = ids[0]
        gamertag = self.tree.set(str(aid), "gamertag")
        if self.db.twitch_never([aid]):
            if not messagebox.askyesno("Twitch", f"{gamertag} is marked 'never link'. Remove that mark?"):
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
            if cur and messagebox.askyesno("Twitch", f"Remove the Twitch link of {gamertag}?"):
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

        def work():
            try:
                self.q.put(("sot_streams", (self.tclient.game_streams(), None)))
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
        return f"{m // 60}h {m % 60:02d}m"

    def _on_sot_streams(self, streams, err):
        self._set_busy("sotstreams", False)
        if err:
            return self.set_status(f"Live SoT streams failed: {err}", error=True)
        self.set_status(f"{len(streams)} streams live in Sea of Thieves right now.")
        old = getattr(self, "sot_win", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        win = self.sot_win = tk.Toplevel(self.root)
        win.title(f"Live in Sea of Thieves on Twitch ({len(streams)})")
        win.configure(bg=self.pal["bg"])
        accounts = self.db.list_accounts(owner=self.xkey.active())
        recent = sorted(accounts, key=lambda a: (a["pos"] is None, a["pos"] if a["pos"] is not None else 0))
        by_tag = {}
        for a in recent:
            by_tag.setdefault(a["gamertag"], a)
        linked = {l["twitch_id"]: l["account_id"] for l in self.db.twitch_links()}
        master = self.db.shared_by_twitch()                    # also players this PC has never met
        tag_of = {a["id"]: a["gamertag"] for a in accounts}
        same = {}
        for a in recent:
            if a["twitch_never"] is None:
                for c in login_candidates(a["gamertag"]):
                    same.setdefault(c, a["gamertag"])

        def match(s):
            if s["id"] in linked:
                return "linked: " + tag_of.get(linked[s["id"]], "?")
            if s["id"] in master:
                return "linked: " + master[s["id"]] + SHARED_MARK
            return "same name: " + same[s["login"].lower()] if s["login"].lower() in same else ""

        ttk.Label(win, style="Hint.TLabel", wraplength=860, justify="left", text=(
            "Everyone streaming Sea of Thieves on Twitch now. Just met a streamer? Find them here (filter by name or "
            "title), pick the player below or type any Xbox gamertag (e.g. from 'Find on SOTREP') and press 'Link'. "
            "'same name' is only a guess. Click a column to sort.")).pack(fill="x", padx=10,
                                                                                                    pady=(10, 4))
        langs = {}
        for s in streams:
            langs[s["language"] or "?"] = langs.get(s["language"] or "?", 0) + 1
        lang_label = {"All languages": None, **{f"{l} ({n})": l for l, n in
                                                sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))}}
        top = ttk.Frame(win)
        top.pack(fill="x", padx=10)
        ttk.Label(top, text="Search:").pack(side="left")
        fvar = tk.StringVar()
        ttk.Entry(top, textvariable=fvar, width=24).pack(side="left", padx=(4, 10))
        lvar = tk.StringVar(value="All languages")
        ttk.Combobox(top, textvariable=lvar, values=list(lang_label), state="readonly", width=16).pack(side="left")
        ttk.Label(top, text="Viewers").pack(side="left", padx=(10, 4))
        vmin, vmax = tk.StringVar(), tk.StringVar()
        ttk.Entry(top, textvariable=vmin, width=6).pack(side="left")
        ttk.Label(top, text="to").pack(side="left", padx=3)
        ttk.Entry(top, textvariable=vmax, width=6).pack(side="left")
        mine = tk.BooleanVar()
        ttk.Checkbutton(top, text="Only my players", variable=mine).pack(side="left", padx=(10, 0))
        shown = tk.StringVar()
        ttk.Label(top, textvariable=shown, style="Hint.TLabel").pack(side="right")
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        cols = (("match", "Your player", 180), ("channel", "Channel", 160), ("viewers", "Viewers", 60),
                ("uptime", "Live for", 65), ("lang", "Lang", 45), ("title", "Title", 420), ("tags", "Tags", 260))
        wide = ("title", "tags")                                  # only these grow with the window
        tv = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=14)
        sort = {"col": None, "desc": False}
        keys = {"match": lambda s: (not match(s), match(s).casefold()), "channel": lambda s: s["name"].casefold(),
                "viewers": lambda s: s["viewers"], "uptime": lambda s: -(s["started"] or 0),
                "lang": lambda s: s["language"], "title": lambda s: s["title"].casefold(),
                "tags": lambda s: ", ".join(s.get("tags") or []).casefold()}

        def sort_by(c):
            sort["desc"] = not sort["desc"] if sort["col"] == c else c == "viewers"
            sort["col"] = c
            fill()
            remember()
        for c, t, w in cols:
            tv.heading(c, text=t, command=lambda c=c: sort_by(c))
            tv.column(c, width=w, minwidth=30, anchor="e" if c == "viewers" else "w", stretch=c in wide)
        tv.tag_configure("match", background=self.pal["friend"])
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        rows = {}
        now = time.time()

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
            shown.set(f"showing {len(rows)} of {len(streams)}")

        keep = getattr(self, "sot_filters", {})                       # Refresh keeps the filters
        fvar.set(keep.get("search", ""))
        lvar.set(next((k for k, v in lang_label.items() if v == keep.get("lang")), "All languages"))
        vmin.set(keep.get("vmin", ""))
        vmax.set(keep.get("vmax", ""))
        mine.set(keep.get("mine", False))
        sort.update(keep.get("sort", {}))

        def remember(*_):
            self.sot_filters = {"search": fvar.get(), "lang": lang_label.get(lvar.get()), "vmin": vmin.get(),
                                "vmax": vmax.get(), "mine": mine.get(), "sort": dict(sort)}
        for v in (fvar, lvar, vmin, vmax, mine):
            v.trace_add("write", fill)
            v.trace_add("write", remember)
        fill()
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=10, pady=(4, 10), before=box)   # always visible
        ttk.Label(bar, text="Player:").pack(side="left")
        pvar = tk.StringVar()
        sel = self._selected_ids()
        if len(sel) == 1 and sel[0] in tag_of:
            pvar.set(tag_of[sel[0]])
        ttk.Combobox(bar, textvariable=pvar, values=list(by_tag), width=28).pack(side="left", padx=6)

        def on_pick(_=None):
            s = rows.get((tv.selection() or [None])[0])
            if s and s["id"] in master and s["id"] not in linked:
                pvar.set(master[s["id"]])
            elif s and s["login"].lower() in same and s["id"] not in linked:
                pvar.set(same[s["login"].lower()])
        tv.bind("<<TreeviewSelect>>", on_pick)

        def link():
            s = rows.get((tv.selection() or [None])[0])
            typed = pvar.get().strip()
            a = by_tag.get(typed) or next((x for t, x in by_tag.items() if t.casefold() == typed.casefold()), None)
            if not s:
                return self.set_status("Select a stream first.", error=True)
            if not typed:
                return self.set_status("Pick a player, or type any Xbox gamertag (e.g. copied from SOTREP).",
                                       error=True)
            if not a:
                return lookup_and_link(s, typed)
            try:
                self.db.link_twitch(a["id"], s["id"], s["login"], s["name"])
            except ValueError as e:
                return self.set_status(str(e), error=True)
            linked[s["id"]] = a["id"]
            self.set_status(f"Linked {a['gamertag']} to Twitch channel {s['name']}.")
            pvar.set("")                                                              # ready for the next one
            fill()
            tv.selection_set([i for i, r in rows.items() if r["id"] == s["id"]])     # keep the stream selected
            self.reload()
            self.twitch_check_now(manual=True)

        def open_stream():
            for i in tv.selection():
                webbrowser.open(f"https://www.twitch.tv/{rows[i]['login']}")

        def lookup_and_link(s, typed):
            """A gamertag that is not in the history: ask Xbox (OpenXBL search) for that exact account first."""
            key = self.xkey.get()
            if not key:
                return self.set_status("Add an OpenXBL account to look up gamertags that are not in your list.",
                                       error=True)
            if "xboxlookup" in self.busy:
                return
            self._set_busy("xboxlookup", True)
            self.set_status(f"Looking up '{typed}' on Xbox...")

            def done(player, near, err):
                self._set_busy("xboxlookup", False)
                if err:
                    return self.set_status(f"Xbox lookup failed: {err}", error=True)
                if player is None:
                    return self.set_status(f"No Xbox account named exactly '{typed}'."
                                           + (f" Similar: {', '.join(near)}" if near else ""), error=True)
                aid = self.db.ensure_account(player.gamertag, player.xuid)
                acc = {"id": aid, "gamertag": player.gamertag}
                by_tag[player.gamertag] = acc
                tag_of[aid] = player.gamertag
                try:
                    self.db.link_twitch(aid, s["id"], s["login"], s["name"])
                except ValueError as e:
                    return self.set_status(str(e), error=True)
                linked[s["id"]] = aid
                self.set_status(f"Added {player.gamertag} (Xbox ID {player.xuid}) and linked Twitch channel {s['name']}.")
                if win.winfo_exists():
                    pvar.set("")                                                      # ready for the next one
                    fill()
                    tv.selection_set([i for i, r in rows.items() if r["id"] == s["id"]])
                self.reload()
                self.twitch_check_now(manual=True)

            def work():
                try:
                    player, near = find_gamertag(key, typed)
                    self.q.put(("call", lambda: done(player, near, None)))
                except ProviderError as e:
                    self.q.put(("call", lambda e=e: done(None, [], str(e))))
                except Exception as e:
                    self.q.put(("call", lambda e=e: done(None, [], f"Unexpected error: {e}")))

            threading.Thread(target=work, daemon=True).start()

        win.lookup_and_link = lookup_and_link

        def find_on_sotrep():
            for i in tv.selection():
                webbrowser.open(sotrep.twitch_search_url(rows[i]["login"]))

        tv.bind("<Double-1>", lambda e: open_stream())
        ttk.Button(bar, text="Link", command=link).pack(side="left", padx=4)
        ttk.Button(bar, text="Open stream", command=open_stream).pack(side="left", padx=4)
        ttk.Button(bar, text="Find on SOTREP", command=find_on_sotrep).pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Refresh", command=self.sot_streams).pack(side="right", padx=4)

        def reset():
            """Back to the default view: no filters, most viewers first."""
            fvar.set("")
            lvar.set("All languages")
            vmin.set("")
            vmax.set("")
            mine.set(False)
            sort.update(col=None, desc=False)
            for c, _, w in cols:
                tv.column(c, width=w)
            fill()
            remember()
            tv.yview_moveto(0)
        ttk.Button(bar, text="Reset table", command=reset).pack(side="right", padx=4)
        win.reset = reset
        win.streams_tv, win.player_var, win.filter_var, win.link = tv, pvar, fvar, link   # kept for tests
        win.lang_var, win.vmin, win.vmax, win.mine, win.sort_by = lvar, vmin, vmax, mine, sort_by
        win.find_on_sotrep = find_on_sotrep
        win.minsize(760, 300)
        self._place_dialog(win, 1040, 560)

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
            return messagebox.showinfo("Check live", "Select one or more players first (up to 20 at a time).")
        if not self.xkey.get() and not (self.tcreds.configured() and self.db.twitch_links(ids)):
            return self.set_status("Add an OpenXBL account first.", error=True)
        self._start_livecheck(ids if self.xkey.get() else [], ids)

    def _start_livecheck(self, xbox_ids, ids, only_live=False, note=""):
        if "livecheck" in self.busy:
            return
        self._set_busy("livecheck", True)
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
        win = tk.Toplevel(self.root)
        win.title(f"Live now: {len(results)} player(s)" if only_live else "Live check results")
        win.configure(bg=self.pal["bg"])
        rows_shown = min(max(len(results), 1), 20 if only_live else 10)
        ttk.Label(win, style="Hint.TLabel", wraplength=940, justify="left", text=(
            (f"Everyone with source Live who is live right now. {note}\n" if only_live else "") +
            ("Nobody is live right now.\n" if only_live and not results else "") +
            "Xbox column: Xbox's own broadcasting flag for that exact account. Xbox misses streams started from a PC "
            "and never gives the channel or title. Twitch column: only for players you linked to a channel, straight "
            "from Twitch (exact channel ID). Double-click a row to open its Twitch channel.")
        ).pack(fill="x", padx=10, pady=(10, 4))
        cols = ("player", "state", "broadcast", "twitch", "game")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=rows_shown)
        for c, t, w in (("player", "Player", 150), ("state", "Xbox says", 70), ("broadcast", "Xbox broadcasting", 250),
                        ("twitch", "Twitch", 360), ("game", "Playing / last online", 150)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("live", background=self.pal["friend"])
        logins = {}
        for n, r in enumerate(results):
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
            if r.get("tw_link"):
                logins[iid] = r["tw_link"]["login"]
        tv.bind("<Double-1>", lambda e: [webbrowser.open(f"https://www.twitch.tv/{logins[i]}")
                                         for i in tv.selection() if i in logins])
        tv.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        win.results_tv = tv                          # kept for tests
        self._place_dialog(win, 1000, 160 + 26 * rows_shown)

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
        win = tk.Toplevel(self.root)
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
        win = tk.Toplevel(self.root)
        win.title("Add Xbox account")
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        ttk.Button(win, text="Sign in with Microsoft (recommended)",
                   command=lambda: (win.destroy(), self.sign_in_microsoft())).pack(fill="x", padx=16, pady=(16, 4))
        ttk.Label(win, style="Hint.TLabel", wraplength=420, justify="left", text=(
            "Opens Microsoft's own sign-in page in your browser; you type your password there, never in this app. "
            "The app talks to Xbox directly: no xbl.io account or key, the game and time for each recent player, "
            "and your full message inbox. The sign-in is saved encrypted for this Windows user only.")
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
        k = simpledialog.askstring(
            "OpenXBL key", "Sign in at xbl.io (opened in your browser) with your Microsoft account,\n"
            "copy your API key from the dashboard and paste it here:", parent=self.root)
        if not (k and k.strip()):
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

    def switch_account(self):
        self.xkey.set_active(self.acct_var.get())
        self.to_top_after_sync = True
        self.reload(to_top=True)
        self.set_status(f"Switched to {self.xkey.active()}. Refreshing...")
        self.msg_unread = []
        self._update_msg_button()
        self._schedule_messages(3_000)
        self.refresh_now(manual=True)

    def remove_account(self):
        name, key = self.xkey.active(), self.xkey.get()
        what = "Microsoft sign-in" if direct.is_ms_key(key) else "key"
        if name and messagebox.askyesno("Remove account", f"Remove the saved {what} for '{name}'?\n"
                                        "Your player history is kept."):
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
            self.msg_unread = []
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

            def work():
                try:
                    chats, err = social.unread_chats(key), None
                except ProviderError as e:
                    chats, err = None, str(e)
                except Exception as e:                   # never let a worker thread die silently
                    chats, err = None, f"Unexpected error: {e}"
                self.q.put(("call", lambda: self._after_messages(key, chats, err)))
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

    def _chat_opened(self, xuid):
        # at least the newest known message's own time, so a PC clock behind Xbox's still clears it
        self.msg_opened[xuid] = max([time.time()] + [c["at"] or 0 for c in self.msg_unread if c["xuid"] == xuid])
        self.msg_unread = [c for c in self.msg_unread if c["xuid"] != xuid]
        self._update_msg_button()

    def _update_msg_button(self):
        n = len(self.msg_unread)
        self.msg_btn.configure(text=f"Messages ({n})" if n else "Messages",
                               style="Accent.TButton" if n else "Tool.TButton")

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
        return self._toast("✉  " + title, body, lambda: self.inbox_dialog(goto=goto))

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
        foot = tk.Label(inner, text="Click to open Messages", bg=P["head"], fg=P["muted"], font=("Segoe UI", 8),
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
            who = "" if imported or not self.xkey.active() else f" as {self.xkey.active()}"
            self.set_status(("Imported: " if imported else f"Synced{who}: ") + out.message)
        else:
            msg = out.message + ("  -> check or re-add the account key." if out.reauth else "")
            self.set_status(f"Refresh failed (existing data kept): {msg}", error=True)
        self.reload(to_top=self.to_top_after_sync)
        self.to_top_after_sync = False

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
                        messagebox.showwarning("Updates", err)
                    elif info:
                        self._offer_update(info)
                    elif manual:
                        messagebox.showinfo("Updates", "You are up to date.")
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
        text = f"Version {info.version} is available (you have {__version__}).\n\n{info.notes[:800]}\n\n"
        if not (info.exe_url and updater.running_exe()):        # running from source, or no program file
            if messagebox.askyesno("Update available", text + "Open the release page?"):
                webbrowser.open(info.url)
            return
        answer = messagebox.askyesnocancel("Update available", text + (
            "Yes: update now (downloads the new version from GitHub, checks it and restarts the app)\n"
            "No: open the release page\nCancel: later"))
        if answer:
            self.update_now(info)
        elif answer is False:
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
                    if messagebox.askyesno("Update failed", f"{err}\n\nOpen the release page instead?"):
                        webbrowser.open(info.url)
                    return
                self.set_status(f"Version {info.version} is installed; it is used from the next start of the app.")
                if messagebox.askyesno("Update installed", f"Version {info.version} is installed. Restart now?"):
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
