"""Tkinter UI. All network/DB-heavy work runs in worker threads; results return via a queue."""
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import __version__, sotrep, updater
from .db import Database
from .paths import data_dir
from .providers import (ManualImportProvider, OpenXblKey, OpenXblProvider, ProviderError,
                        find_gamertag, lookup_gamertag)
from .automatch import HITS_NEEDED, AutoMatcher
from .live import HOURLY_BUDGET, LIVE_SCOPE, LiveChecker
from .sync import SyncOutcome, SyncService
from .themes import THEMES, clamp, icon_path, palette, twitch_icon_path
from .twitch import (CHECK_EVERY, CONSOLE_URL, STATUS_MAX_AGE, TwitchChecker, TwitchClient, TwitchCreds,
                     login_candidates)

MIN_INTERVAL = 30


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


class App:
    def __init__(self, root: tk.Tk, db: Database):
        self.root, self.db = root, db
        self.sync = SyncService(db)
        self.xkey = OpenXblKey(data_dir() / "openxbl_keys.json")
        self.q: "queue.Queue" = queue.Queue()
        self.closing = False
        self.paused = db.get_setting("paused", "0") == "1"
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
        self.matcher = AutoMatcher(db, self.tclient)
        self.twitch_after_id = None
        self.twitch_busy = False
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()
        self.root.after(200, self._poll)
        self._schedule()
        self._schedule_live(15_000)
        self._schedule_twitch(20_000)
        if db.recovered_from:
            self.set_status(f"Database was damaged; moved aside to {db.recovered_from}", error=True)
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
        r.title(f"Better Recently Played With {__version__}")
        r.geometry("1130x540")
        r.minsize(760, 380)
        self._apply_style()
        self._apply_icon()

        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(bar, text="Refresh now", command=self.refresh_now).pack(side="left")
        self.pause_btn = ttk.Button(bar, text="", command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Label(bar, text="every").pack(side="left", padx=(8, 2))
        self.interval = tk.IntVar(value=int(self.db.get_setting("interval", "60")))
        sp = ttk.Spinbox(bar, from_=MIN_INTERVAL, to=3600, increment=10, width=5, textvariable=self.interval,
                         command=self._save_interval)
        sp.pack(side="left")
        sp.bind("<FocusOut>", lambda e: self._save_interval())
        ttk.Label(bar, text="s").pack(side="left", padx=2)
        ttk.Button(bar, text="Check for updates", command=lambda: threading.Thread(
            target=self._update_check, args=(True,), daemon=True).start()).pack(side="right")
        self.theme_var = tk.StringVar(value=self.pal["name"])
        tb = ttk.Combobox(bar, textvariable=self.theme_var, state="readonly", width=14,
                          values=[t[0] for t in THEMES])
        tb.pack(side="right", padx=(0, 6))
        tb.bind("<<ComboboxSelected>>", lambda e: self.set_theme(tb.current() + 1))
        ttk.Label(bar, text="Theme").pack(side="right", padx=(0, 4))
        self.auto_upd = tk.BooleanVar(value=self.db.get_setting("auto_update_check", "1") == "1")
        ttk.Checkbutton(bar, text="auto-check", variable=self.auto_upd,
                        command=lambda: self.db.set_setting("auto_update_check", "1" if self.auto_upd.get() else "0")
                        ).pack(side="right", padx=6)

        acct = ttk.Frame(r)
        acct.pack(fill="x", padx=8, pady=4)
        ttk.Label(acct, text="Syncing as Xbox account:").pack(side="left")
        self.acct_var = tk.StringVar()
        self.acct_box = ttk.Combobox(acct, textvariable=self.acct_var, state="readonly", width=24)
        self.acct_box.pack(side="left", padx=6)
        self.acct_box.bind("<<ComboboxSelected>>", lambda e: self.switch_account())
        ttk.Button(acct, text="Add account...", command=self.set_key).pack(side="left")
        ttk.Button(acct, text="Remove", command=self.remove_account).pack(side="left", padx=4)
        ttk.Button(acct, text="Twitch setup...", command=self.twitch_setup_dialog).pack(side="right")
        ttk.Button(acct, text="Live SoT streams", command=self.sot_streams).pack(side="right", padx=4)
        self._refresh_accounts()

        self.players_tab = ttk.Frame(r)
        self.players_tab.pack(fill="both", expand=True)
        try:
            self.twitch_img = tk.PhotoImage(file=str(twitch_icon_path()))
        except tk.TclError:
            self.twitch_img = tk.PhotoImage(width=1, height=1)   # missing asset must never stop the app

        bar2 = ttk.Frame(self.players_tab)
        bar2.pack(fill="x", padx=8, pady=4)
        ttk.Label(bar2, text="Search").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.reload())
        ttk.Entry(bar2, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bar2, text="Import...", command=self.import_dialog).pack(side="left", padx=4)
        ttk.Button(bar2, text="Export...", command=self.export).pack(side="left")

        cols = ("gamertag", "xuid", "tags", "twitch", "last_seen", "origin", "current")
        self.tree = ttk.Treeview(self.players_tab, columns=cols, show="tree headings", selectmode="extended")
        self.col_titles = {"gamertag": "Gamertag", "xuid": "XUID", "tags": "Tags", "twitch": "Twitch",
                           "last_seen": "Last seen by app", "origin": "Source", "current": "Xbox list #"}
        self.sort_col, self.sort_desc = "current", False  # Xbox list order (#1 on top), then older history
        self.tree.column("#0", width=34, minwidth=34, stretch=False, anchor="center")  # Twitch icon, only when live
        self.tree.heading("#0", text="")
        for c, w in (("gamertag", 170), ("xuid", 130), ("tags", 110), ("twitch", 190), ("last_seen", 115),
                     ("origin", 55), ("current", 90)):
            self.tree.heading(c, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="w")
        self._update_headings()
        # ttk: the tag created FIRST wins when a row has several, so "friend" must be created before "odd"
        self.tree.tag_configure("friend", background=self.pal["friend"])
        self.tree.tag_configure("odd", background=self.pal["stripe"])
        sb = ttk.Scrollbar(self.players_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=4)
        self.tree.pack(fill="both", expand=True, padx=(8, 0), pady=4)
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())

        bar3 = ttk.Frame(self.players_tab)
        bar3.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar3, text="Open SOTREP", command=self.open_selected).pack(side="left")
        ttk.Button(bar3, text="Tag...", command=self.tag_selected).pack(side="left", padx=4)
        ttk.Button(bar3, text="Untag", command=self.untag_selected).pack(side="left")
        ttk.Button(bar3, text="Encounters", command=self.encounters_dialog).pack(side="left", padx=4)
        ttk.Button(bar3, text="Check live", command=self.check_live_selected).pack(side="left")
        ttk.Button(bar3, text="Twitch...", command=self.link_twitch_selected).pack(side="left", padx=4)
        ttk.Button(bar3, text="Open Twitch", command=self.open_twitch_selected).pack(side="left")
        ttk.Button(bar3, text="Guess Twitch", command=self.guess_twitch).pack(side="left", padx=4)
        ttk.Button(bar3, text="Never link", command=self.never_link_selected).pack(side="left")
        self.count_lbl = ttk.Label(bar3, text="")
        self.count_lbl.pack(side="right")
        legend = tk.Label(bar3, text=" Friend ", bg=self.pal["friend"], fg=self.pal["fg"], font=("Segoe UI", 8))
        legend.pack(side="right", padx=(0, 10))
        self.legend = legend
        self.live_status = tk.StringVar(value="")
        ttk.Label(bar3, image=self.twitch_img, text=" live now", compound="left",
                  style="Hint.TLabel").pack(side="right", padx=(0, 10))

        self.status = tk.StringVar()
        statusbar = ttk.Frame(r)
        statusbar.pack(fill="x", padx=8, pady=(0, 8))
        self.spinner = Spinner(statusbar, color=self.pal["accent"], bg=self.pal["bg"])
        self.spinner.pack(side="left", padx=(0, 6))
        self.status_lbl = ttk.Label(statusbar, textvariable=self.status, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self._update_pause_label()
        self.set_status("Ready. Add an OpenXBL account or use Import to add players.")

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
            if tw_live:
                v = a["twitch_viewers"]
                tw_text = f"LIVE {v} - {a['twitch_name']}" if v is not None else f"LIVE - {a['twitch_name']}"
            elif a["twitch_never"] is not None:
                tw_text = "never link" + (f" ({a['twitch_never']})" if a["twitch_never"] else "")
            else:
                tw_text = (a["twitch_name"] or "") + (" (auto)" if a["twitch_source"] == "auto" else "")
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

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

    def _set_busy(self, name: str, on: bool):
        (self.busy.add if on else self.busy.discard)(name)
        if self.busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _update_pause_label(self):
        self.pause_btn.configure(text="Resume auto-refresh" if self.paused else "Pause auto-refresh")

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
        self.set_status("Twitch keys saved on this PC. Select a player and press 'Twitch...' to link a channel.")
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

    def guess_twitch(self):
        """Suggest Twitch channels with the same name as the gamertag (selected players, or every player without a
        Twitch channel). Players marked 'never link' are skipped."""
        if not self.tcreds.configured():
            return self.set_status("Twitch is not set up. Use 'Twitch setup...' first.", error=True)
        if "twitchguess" in self.busy:
            return
        rows = self.db.list_accounts(owner=self.xkey.active())
        sel = set(self._selected_ids())
        pick = [r for r in rows if r["id"] in sel] if sel else rows
        players = [(r["id"], r["gamertag"]) for r in pick if not r["twitch_id"] and r["twitch_never"] is None]
        if not players:
            return self.set_status("Nothing to guess: those players have a Twitch channel or are marked 'never link'.")
        self._set_busy("twitchguess", True)
        self.set_status(f"Looking for Twitch channels named like {len(players)} player(s)...")

        def work():
            try:
                self.q.put(("twitch_guess", (self.tclient.suggest(players), None)))
            except ProviderError as e:
                self.q.put(("twitch_guess", ([], str(e))))
            except Exception as e:
                self.q.put(("twitch_guess", ([], f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _on_twitch_guess(self, sugg, err):
        self._set_busy("twitchguess", False)
        if err:
            return self.set_status(f"Twitch guess failed: {err}", error=True)
        if not sugg:
            return self.set_status("No Twitch channels with the same name as those gamertags.")
        self.set_status(f"Found {len(sugg)} possible Twitch channel(s). Check and confirm the right ones.")
        win = tk.Toplevel(self.root)
        win.title("Possible Twitch channels (guesses)")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=700, justify="left", text=(
            "These channels only have the SAME NAME as the gamertag. They can be a different person. Open a channel "
            "to check, then select the right ones and press 'Link selected'. Only linked channels are tracked.")
        ).pack(fill="x", padx=10, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        tv = ttk.Treeview(box, columns=("player", "channel", "status"), show="headings", height=min(len(sugg), 12))
        for c, t, w in (("player", "Player", 180), ("channel", "Possible Twitch channel", 180), ("status", "Twitch now", 340)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("live", background=self.pal["friend"])
        rows = {}
        for g in sorted(sugg, key=lambda g: (not g["sot"], not g["live"], g["gamertag"].casefold())):
            status = (f"LIVE: {g['game'] or 'no game set'} ({g['viewers']} viewers)" if g["live"] else "offline")
            iid = tv.insert("", "end", values=(g["gamertag"], g["name"], status), tags=("live",) if g["live"] else ())
            rows[iid] = g
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)

        def link_selected():
            chosen = [rows[i] for i in tv.selection()]
            done = 0
            for g in chosen:
                try:
                    self.db.link_twitch(g["account_id"], g["id"], g["login"], g["name"])
                    done += 1
                except ValueError:                                   # marked 'never link' meanwhile
                    pass
            if chosen:
                self.set_status(f"Linked {done} Twitch channel(s) you confirmed.")
                win.destroy()
                self.reload()
                self.twitch_check_now(manual=True)

        def never_selected():
            chosen = {rows[i]["account_id"]: rows[i]["gamertag"] for i in tv.selection()}
            if not chosen:
                return
            reason = self._ask_tag("Never link", f"Never link a Twitch channel for {len(chosen)} player(s).\n"
                                   "Reason (optional, e.g. cheater):", ["cheater"], True)
            if reason is None:
                return
            self.db.set_twitch_never(list(chosen), reason)
            for i in list(tv.get_children()):
                if rows[i]["account_id"] in chosen:
                    tv.delete(i)
            self.set_status(f"Marked {len(chosen)} player(s) 'never link'.")
            self.reload()

        def open_channel():
            for i in tv.selection():
                webbrowser.open(f"https://www.twitch.tv/{rows[i]['login']}")

        tv.bind("<Double-1>", lambda e: open_channel())
        bar = ttk.Frame(win)
        bar.pack(pady=(4, 10))
        ttk.Button(bar, text="Link selected", command=link_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Open channel", command=open_channel).pack(side="left", padx=4)
        ttk.Button(bar, text="Never link player", command=never_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="left", padx=4)
        win.guess_tv, win.link_selected = tv, link_selected          # kept for tests
        self._place_dialog(win, 740, 160 + 24 * min(len(sugg), 12))

    def sot_streams(self):
        """Everyone live in Sea of Thieves on Twitch right now, so the user can spot a streamer they just met."""
        if not self.tcreds.configured():
            return self.set_status("Twitch is not set up. Use 'Twitch setup...' first.", error=True)
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
        tag_of = {a["id"]: a["gamertag"] for a in accounts}
        same = {}
        for a in recent:
            if a["twitch_never"] is None:
                for c in login_candidates(a["gamertag"]):
                    same.setdefault(c, a["gamertag"])

        def match(s):
            if s["id"] in linked:
                return "linked: " + tag_of.get(linked[s["id"]], "?")
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
        cols = (("match", "Your player", 160), ("channel", "Channel", 140), ("viewers", "Viewers", 60),
                ("uptime", "Live for", 65), ("lang", "Lang", 45), ("title", "Title", 330), ("tags", "Tags", 180))
        tv = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=16)
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
            tv.column(c, width=w, anchor="e" if c == "viewers" else "w")
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
            else:
                order = sorted(streams, key=lambda s: (not match(s), -s["viewers"]))
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
        bar.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Label(bar, text="Player:").pack(side="left")
        pvar = tk.StringVar()
        sel = self._selected_ids()
        if len(sel) == 1 and sel[0] in tag_of:
            pvar.set(tag_of[sel[0]])
        ttk.Combobox(bar, textvariable=pvar, values=list(by_tag), width=28).pack(side="left", padx=6)

        def on_pick(_=None):
            s = rows.get((tv.selection() or [None])[0])
            if s and s["login"].lower() in same and s["id"] not in linked:
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
                if win.winfo_exists():
                    pvar.set(player.gamertag)
                try:
                    self.db.link_twitch(aid, s["id"], s["login"], s["name"])
                except ValueError as e:
                    return self.set_status(str(e), error=True)
                linked[s["id"]] = aid
                self.set_status(f"Added {player.gamertag} (Xbox ID {player.xuid}) and linked Twitch channel {s['name']}.")
                if win.winfo_exists():
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
        ttk.Button(bar, text="Auto matches...", command=self.auto_matches_dialog).pack(side="right", padx=4)
        win.streams_tv, win.player_var, win.filter_var, win.link = tv, pvar, fvar, link   # kept for tests
        win.lang_var, win.vmin, win.vmax, win.mine, win.sort_by = lvar, vmin, vmax, mine, sort_by
        win.find_on_sotrep = find_on_sotrep
        self._place_dialog(win, 1040, 540)

    @staticmethod
    def _match_state(m) -> str:
        if m["auto_linked"]:
            return "linked automatically"
        if m["rejected"]:
            return "ruled out: you removed this link"
        if m["misses"]:
            return f"ruled out: streamed while the player was not in the game ({m['misses']}x)"
        if m["never"]:
            return "player marked 'never link'"
        if m["linked"]:
            return "player already has a Twitch channel"
        if m["channel_taken"]:
            return "channel is linked to another player"
        return f"collecting proof: {m['hits']} of {HITS_NEEDED}"

    def auto_matches_dialog(self):
        """What the auto-matcher has seen: similar-name pairs and the streams that proved or ruled them out."""
        rows = self.db.match_summary()
        win = tk.Toplevel(self.root)
        win.title("Auto matches")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=760, justify="left", text=(
            "The app watches players with a similar Twitch name. A pair is linked only after the player was in Sea of "
            f"Thieves during {HITS_NEEDED} different streams of that channel, and never when the channel streamed "
            "while the player was offline or in another game. Same name alone proves nothing. Uses your 20 most "
            "recent players (the ones whose Xbox status the app checks).")).pack(fill="x", padx=10, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        tv = ttk.Treeview(box, columns=("player", "channel", "hits", "misses", "state"), show="headings", height=12)
        for c, t, w in (("player", "Player", 170), ("channel", "Twitch channel", 160), ("hits", "In game", 65),
                        ("misses", "Not in game", 80), ("state", "State", 300)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("linked", background=self.pal["friend"])
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        by_iid = {}
        for m in rows:
            iid = tv.insert("", "end", tags=("linked",) if m["auto_linked"] else (), values=(
                m["gamertag"], m["display_name"], m["hits"], m["misses"] - m["rejected"], self._match_state(m)))
            by_iid[iid] = m
        if not rows:
            tv.insert("", "end", values=("", "", "", "", "nothing seen yet: needs Twitch setup + live checks"))

        def open_channel():
            for i in tv.selection():
                if i in by_iid:
                    webbrowser.open(f"https://www.twitch.tv/{by_iid[i]['login']}")

        tv.bind("<Double-1>", lambda e: open_channel())
        bar = ttk.Frame(win)
        bar.pack(pady=(4, 10))
        ttk.Button(bar, text="Open channel", command=open_channel).pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="left", padx=4)
        win.matches_tv = tv                                          # kept for tests
        self._place_dialog(win, 820, 420)
        return win

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
        self.set_status(f"Marked {len(ids)} player(s) 'never link'. Guess Twitch skips them.")
        self.reload()

    def open_twitch_selected(self):
        links = self.db.twitch_links(self._selected_ids())
        if not links:
            return self.set_status("Select a player that has a Twitch channel linked.", error=True)
        for l in links:
            webbrowser.open(f"https://www.twitch.tv/{l['login']}")

    def twitch_check_now(self, manual=False):
        """Auto-matcher round (only when there is fresh Xbox presence to compare), then live status of the links."""
        if self.twitch_busy or not self.tcreds.configured():
            return
        if not self.db.twitch_links() and not self.matcher.players():
            return
        self.twitch_busy = True
        self._set_busy("twitch", True)

        def work():
            made = []
            try:
                made = self.matcher.run()
                result = self.tcheck.check_accounts() if self.db.twitch_links() else {}
                self.q.put(("twitch", (result, None, manual, made)))
            except ProviderError as e:
                self.q.put(("twitch", ({}, str(e), manual, made)))
            except Exception as e:
                self.q.put(("twitch", ({}, f"Unexpected error: {e}", manual, made)))

        threading.Thread(target=work, daemon=True).start()

    def _on_twitch(self, result, err, manual, made=()):
        self.twitch_busy = False
        self._set_busy("twitch", False)
        if made:
            self.set_status("Found automatically: " + ", ".join(
                f"{m['gamertag']} = Twitch {m['name']} (in the game during {m['hits']} of their streams)" for m in made))
        if err:
            self.set_status(f"Twitch check failed: {err}", error=True)
        elif manual and not made:
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
    def check_live_selected(self):
        ids = self._selected_ids()
        if not ids:
            return messagebox.showinfo("Check live", "Select one or more players first (up to 20 at a time).")
        if not self.xkey.get() and not (self.tcreds.configured() and self.db.twitch_links(ids)):
            return self.set_status("Add an OpenXBL account first.", error=True)
        if "livecheck" in self.busy:
            return
        self._set_busy("livecheck", True)
        self.set_status(f"Checking {min(len(ids), 20)} selected player(s)...")

        def work():
            try:
                results = self.live.check_accounts(ids) if self.xkey.get() else self._blank_results(ids)
                tw, tw_err = {}, None
                links = {l["account_id"]: l for l in self.db.twitch_links(ids)}
                if links and self.tcreds.configured():
                    try:
                        tw = self.tcheck.check_accounts(ids)
                    except ProviderError as e:
                        tw_err = str(e)
                for r in results:
                    r["tw"], r["tw_err"], r["tw_link"] = tw.get(r["id"]), tw_err, links.get(r["id"])
                    if r["tw_link"] is not None:
                        r["tw_link"] = dict(r["tw_link"])
                self.q.put(("livecheck", (results, None)))
            except ProviderError as e:
                self.q.put(("livecheck", ([], str(e))))
            except Exception as e:  # never let a worker thread die silently
                self.q.put(("livecheck", ([], f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _blank_results(self, ids):
        rows = self.db.conn.execute(
            f"SELECT id, gamertag FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return [{"id": r["id"], "gamertag": r["gamertag"], "state": None, "title": None, "seen_at": None,
                 "broadcasting": False, "provider": None, "twitch": False, "viewers": None, "started": None,
                 "error": "no OpenXBL account added"} for r in rows]

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

    def _show_live_results(self, results):
        win = tk.Toplevel(self.root)
        win.title("Live check results")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=940, justify="left", text=(
            "Xbox column: Xbox's own broadcasting flag for that exact account. Xbox misses streams started from a PC "
            "and never gives the channel or title. Twitch column: only for players you linked to a channel, straight "
            "from Twitch (exact channel ID). Double-click a row to open its Twitch channel.")
        ).pack(fill="x", padx=10, pady=(10, 4))
        cols = ("player", "state", "broadcast", "twitch", "game")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=min(max(len(results), 1), 10))
        for c, t, w in (("player", "Player", 150), ("state", "Xbox says", 70), ("broadcast", "Xbox broadcasting", 250),
                        ("twitch", "Twitch", 360), ("game", "Playing / last online", 150)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("live", background=self.pal["friend"])
        logins = {}
        for n, r in enumerate(results):
            if r["error"]:
                bcast, state, game = f"not checked: {r['error']}", "-", ""
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
            tw = r.get("tw")
            is_live = bool(r["broadcasting"] or r["twitch"] or (tw and tw["live"]))
            iid = tv.insert("", "end", tags=("live",) if is_live else (), values=(
                r["gamertag"], state or "", bcast, self._twitch_text(r), game))
            if r.get("tw_link"):
                logins[iid] = r["tw_link"]["login"]
        tv.bind("<Double-1>", lambda e: [webbrowser.open(f"https://www.twitch.tv/{logins[i]}")
                                         for i in tv.selection() if i in logins])
        tv.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        win.results_tv = tv                          # kept for tests
        self._place_dialog(win, 1000, 140 + 26 * min(max(len(results), 1), 10))

    def _on_livecheck(self, results, err):
        self._set_busy("livecheck", False)
        if err:
            return self.set_status(f"Live check failed: {err}", error=True)
        live_n = sum(1 for r in results if r["broadcasting"] or r["twitch"] or (r.get("tw") and r["tw"]["live"]))
        self.set_status(f"Live check: {len(results)} player(s) asked, {live_n} live.")
        self.reload()                                # shows the Twitch icon for anyone confirmed live
        self._show_live_results(results)

    def toggle_pause(self):
        self.paused = not self.paused
        self.db.set_setting("paused", "1" if self.paused else "0")
        self._update_pause_label()
        self.set_status("Auto-refresh paused." if self.paused else "Auto-refresh resumed.")
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
        self.refresh_now(manual=True)

    def remove_account(self):
        name = self.xkey.active()
        if name and messagebox.askyesno("Remove account", f"Remove the saved key for '{name}'?\n"
                                        "Your player history is kept."):
            self.xkey.remove(name)
            self._refresh_accounts()
            self.set_status(f"Removed {name}.")

    def refresh_now(self, manual=True):
        prov = self._provider()
        if prov is None:
            return self.set_status("Not connected. Use 'Add account...', or use Import.", error=True)
        prov.force_friends = manual
        who = self.xkey.active()
        self.set_status(f"Refreshing as {who}...")
        if manual:
            self.live_check_now()
            self.twitch_check_now(manual=False)
        self._set_busy("sync", True)
        self.sync.run_async(prov, lambda out: self.q.put(("sync", out)))

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
            self.refresh_now(manual=False)
        self._schedule()

    def _finish(self, out: "SyncOutcome | None", imported=False):
        if out is None:
            return self.set_status("A refresh is already running.")
        if not imported:
            self._set_busy("sync", False)
        if out.ok:
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
                elif kind == "twitch_guess":
                    self._on_twitch_guess(*val)
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
                        if messagebox.askyesno("Update available", f"Version {info.version} is available "
                                               f"(you have {__version__}).\n\n{info.notes[:800]}\n\nOpen the release page?"):
                            webbrowser.open(info.url)
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
