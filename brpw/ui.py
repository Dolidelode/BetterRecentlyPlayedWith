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
from .sync import SyncOutcome, SyncService
from .themes import THEMES, clamp, icon_path, palette

MIN_INTERVAL = 30


def fmt_ts(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


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
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload()
        self.root.after(200, self._poll)
        self._schedule()
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
        st.configure("Hint.TLabel", foreground=P["muted"])
        r.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        if hasattr(self, "tree"):
            self.tree.tag_configure("odd", background=P["stripe"])
            self.status_lbl.configure(foreground="#ff7b72" if self.status_error else FG)

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

        bar2 = ttk.Frame(r)
        bar2.pack(fill="x", padx=8, pady=4)
        ttk.Label(bar2, text="Search").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.reload())
        ttk.Entry(bar2, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bar2, text="Import...", command=self.import_dialog).pack(side="left", padx=4)
        ttk.Button(bar2, text="Export...", command=self.export).pack(side="left")

        cols = ("gamertag", "xuid", "person", "last_seen", "origin", "current")
        self.tree = ttk.Treeview(r, columns=cols, show="headings", selectmode="extended")
        self.col_titles = {"gamertag": "Gamertag", "xuid": "XUID", "person": "Person (your group)",
                           "last_seen": "Last seen by app", "origin": "Source", "current": "Xbox list #"}
        self.sort_col, self.sort_desc = "current", False  # Xbox list order (#1 on top), then older history
        for c, w in (("gamertag", 190), ("xuid", 150), ("person", 140), ("last_seen", 130), ("origin", 70),
                     ("current", 85)):
            self.tree.heading(c, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="w")
        self._update_headings()
        self.tree.tag_configure("odd", background=self.pal["stripe"])
        sb = ttk.Scrollbar(r, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=4)
        self.tree.pack(fill="both", expand=True, padx=(8, 0), pady=4)
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())

        bar3 = ttk.Frame(r)
        bar3.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar3, text="Open SOTREP", command=self.open_selected).pack(side="left")
        ttk.Button(bar3, text="Group as one person...", command=self.group).pack(side="left", padx=4)
        ttk.Button(bar3, text="Ungroup", command=self.ungroup).pack(side="left")
        self.count_lbl = ttk.Label(bar3, text="")
        self.count_lbl.pack(side="right")

        self.status = tk.StringVar()
        self.status_lbl = ttk.Label(r, textvariable=self.status, anchor="w")
        self.status_lbl.pack(fill="x", padx=8, pady=(0, 8))
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
        "person": lambda a: (a["person"] or "").casefold(),
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

    def reload(self):
        sel = {self.tree.set(i, "xuid") + "|" + self.tree.set(i, "gamertag") for i in self.tree.selection()}
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.db.list_accounts(self.search.get()), key=self.SORT_KEYS[self.sort_col],
                      reverse=self.sort_desc)
        for n, a in enumerate(rows):
            iid = self.tree.insert("", "end", iid=str(a["id"]), tags=("odd",) if n % 2 else (), values=(
                a["gamertag"], a["xuid"] or "", a["person"] or "", fmt_ts(a["last_seen"]),
                "Live" if a["source"] in ("xbox-live", "openxbl") else "Import",
                str(a["pos"] + 1) if a["pos"] is not None else ""))
            if (a["xuid"] or "") + "|" + a["gamertag"] in sel:
                self.tree.selection_add(iid)
        self.tree.yview_moveto(top)  # a background refresh must not scroll the list back to the top
        self.count_lbl.configure(text=f"{self.db.count_accounts()} unique accounts")

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

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

    def group(self):
        ids = self._selected_ids()
        if len(ids) < 1:
            return messagebox.showinfo("Group", "Select one or more accounts first.")
        name = simpledialog.askstring("Group accounts", "Name of the person these accounts belong to:",
                                      parent=self.root)
        if name and name.strip():
            self.db.group_accounts(ids, name)
            self.reload()

    def ungroup(self):
        self.db.ungroup_accounts(self._selected_ids())
        self.reload()

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
        self.set_status(f"Switched to {self.xkey.active()}. Refreshing...")
        self.refresh_now()

    def remove_account(self):
        name = self.xkey.active()
        if name and messagebox.askyesno("Remove account", f"Remove the saved key for '{name}'?\n"
                                        "Your player history is kept."):
            self.xkey.remove(name)
            self._refresh_accounts()
            self.set_status(f"Removed {name}.")

    def refresh_now(self):
        prov = self._provider()
        if prov is None:
            return self.set_status("Not connected. Use 'Add account...', or use Import.", error=True)
        who = self.xkey.active()
        self.set_status(f"Refreshing as {who}...")
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
            self.refresh_now()
        self._schedule()

    def _finish(self, out: "SyncOutcome | None", imported=False):
        if out is None:
            return self.set_status("A refresh is already running.")
        if out.ok:
            who = "" if imported or not self.xkey.active() else f" as {self.xkey.active()}"
            self.set_status(("Imported: " if imported else f"Synced{who}: ") + out.message)
        else:
            msg = out.message + ("  -> check or re-add the account key." if out.reauth else "")
            self.set_status(f"Refresh failed (existing data kept): {msg}", error=True)
        self.reload()

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "sync":
                    self._finish(val)
                elif kind == "acct_added":
                    name = self.xkey.add(*val)
                    self._refresh_accounts()
                    self.set_status(f"Added '{name}'. Click 'Refresh now'.")
                elif kind == "acct_err":
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

    def _update_check(self, manual):
        try:
            self.q.put(("update", (updater.check(), None, manual)))
        except updater.UpdateError as e:
            self.q.put(("update", (None, str(e), manual)))

    def on_close(self):
        self.closing = True
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
