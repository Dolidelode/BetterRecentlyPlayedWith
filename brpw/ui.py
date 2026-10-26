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
                        lookup_gamertag)
from .live import HOURLY_BUDGET, LIVE_SCOPE, LiveChecker
from .sync import SyncOutcome, SyncService
from .themes import THEMES, clamp, icon_path, palette, twitch_icon_path

MIN_INTERVAL = 30


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
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()
        self.root.after(200, self._poll)
        self._schedule()
        self._schedule_live(15_000)
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
        r.geometry("820x520")
        r.minsize(640, 360)
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

        cols = ("gamertag", "xuid", "tags", "last_seen", "origin", "current")
        self.tree = ttk.Treeview(self.players_tab, columns=cols, show="tree headings", selectmode="extended")
        self.col_titles = {"gamertag": "Gamertag", "xuid": "XUID", "tags": "Tags",
                           "last_seen": "Last seen by app", "origin": "Source", "current": "Xbox list #"}
        self.sort_col, self.sort_desc = "current", False  # Xbox list order (#1 on top), then older history
        self.tree.column("#0", width=34, minwidth=34, stretch=False, anchor="center")  # Twitch icon, only when live
        self.tree.heading("#0", text="")
        for c, w in (("gamertag", 180), ("xuid", 140), ("tags", 140), ("last_seen", 125), ("origin", 60),
                     ("current", 100)):
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
        ttk.Button(bar3, text="Remove tag...", command=self.untag_selected).pack(side="left")
        ttk.Button(bar3, text="Encounters...", command=self.encounters_dialog).pack(side="left", padx=4)
        ttk.Button(bar3, text="Check live", command=self.check_live_selected).pack(side="left")
        self.count_lbl = ttk.Label(bar3, text="")
        self.count_lbl.pack(side="right")
        legend = tk.Label(bar3, text=" Friend ", bg=self.pal["friend"], fg=self.pal["fg"], font=("Segoe UI", 8))
        legend.pack(side="right", padx=(0, 10))
        self.legend = legend
        self.live_status = tk.StringVar(value="")
        ttk.Label(bar3, image=self.twitch_img, text=" broadcasting (Xbox)", compound="left",
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
        live_ids = {r["id"] for r in self.live.live_now()}   # Xbox-confirmed Twitch broadcasts, fresh only
        rows = sorted(self.db.list_accounts(self.search.get(), self.xkey.active()),
                      key=self.SORT_KEYS[self.sort_col], reverse=self.sort_desc)
        for n, a in enumerate(rows):
            friend = a["relation"] in ("Friend", "In your list")
            iid = self.tree.insert(
                "", "end", iid=str(a["id"]), image=self.twitch_img if a["id"] in live_ids else "",
                tags=(("friend",) if friend else ()) + (("odd",) if n % 2 else ()), values=(
                    a["gamertag"], a["xuid"] or "", a["tags"] or "", fmt_ts(a["last_seen"]),
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
        return result["v"] or None

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

    # ---- on-demand live check ----
    def check_live_selected(self):
        ids = self._selected_ids()
        if not ids:
            return messagebox.showinfo("Check live", "Select one or more players first (up to 20 at a time).")
        if not self.xkey.get():
            return self.set_status("Add an OpenXBL account first.", error=True)
        if "livecheck" in self.busy:
            return
        self._set_busy("livecheck", True)
        self.set_status(f"Checking {min(len(ids), 20)} selected player(s) with Xbox...")

        def work():
            try:
                self.q.put(("livecheck", (self.live.check_accounts(ids), None)))
            except ProviderError as e:
                self.q.put(("livecheck", ([], str(e))))
            except Exception as e:  # never let a worker thread die silently
                self.q.put(("livecheck", ([], f"Unexpected error: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _show_live_results(self, results):
        win = tk.Toplevel(self.root)
        win.title("Live check results")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=840, justify="left", text=(
            "Straight from Xbox for each selected player, right now (Xbox's own broadcasting flag for that exact "
            "account). Xbox names the service only when its broadcast record does, and never gives the channel "
            "or stream title. Offline players cannot be broadcasting.")).pack(fill="x", padx=10, pady=(10, 4))
        cols = ("player", "state", "broadcast", "viewers", "game")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=min(max(len(results), 1), 10))
        for c, t, w in (("player", "Player", 160), ("state", "Xbox says", 75), ("broadcast", "Broadcasting", 340),
                        ("viewers", "Viewers", 60), ("game", "Playing / last online", 190)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("live", background=self.pal["friend"])
        for r in results:
            if r["error"]:
                twitch, state, game = f"not checked: {r['error']}", "-", ""
            else:
                state = r["state"]
                since = f", since {time.strftime('%H:%M', time.localtime(r['started']))}" if r.get("started") else ""
                if r["twitch"]:
                    twitch = f"LIVE on Twitch (Xbox's broadcast record says Twitch{since})"
                elif r["broadcasting"]:
                    twitch = f"BROADCASTING per Xbox ({r['provider'] or 'service not stated'}{since})"
                else:
                    twitch = "not broadcasting"
                game = r["title"] or ""
            tv.insert("", "end", tags=("live",) if r["broadcasting"] or r["twitch"] else (), values=(
                r["gamertag"], state or "", twitch, r["viewers"] if r["viewers"] is not None else "", game))
        tv.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        win.results_tv = tv                          # kept for tests
        self._place_dialog(win, 880, 130 + 26 * min(max(len(results), 1), 10))

    def _on_livecheck(self, results, err):
        self._set_busy("livecheck", False)
        if err:
            return self.set_status(f"Live check failed: {err}", error=True)
        live_n = sum(1 for r in results if r["broadcasting"] or r["twitch"])
        self.set_status(f"Live check: {len(results)} player(s) asked, {live_n} broadcasting according to Xbox.")
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
