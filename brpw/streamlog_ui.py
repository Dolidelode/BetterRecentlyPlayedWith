"""Window of the stream log (owner/dev tool). Start with `python streamlog.py` or BRPW-StreamLog.exe."""
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk

from . import sotrep
from .paths import data_dir
from .providers import OpenXblKey, ProviderError, find_gamertag
from .shared import SharedClient, SharedConfig, http_rest
from .streamlog import POLL_EVERY, StreamLog, keep_awake
from .themes import clamp, palette
from .twitch import TwitchClient, TwitchCreds

MASTER_EVERY = 30 * 60
VIEWS = {"To do (new, not in master list)": lambda r, m: r["status"] == "new" and r["twitch_id"] not in m,
         "All": lambda r, m: True,
         "In master list": lambda r, m: r["twitch_id"] in m,
         "Linked here": lambda r, m: r["status"] == "linked",
         "Checked (nothing found yet)": lambda r, m: r["status"] == "checked",
         "Skipped": lambda r, m: r["status"] == "skip"}


def ago(t, now):
    if not t:
        return ""
    s = int(now - t)
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


class StreamLogApp:
    def __init__(self, root, log: StreamLog, tclient, shared_cfg, xkey, shared_http=http_rest, find=find_gamertag):
        self.root, self.log, self.tclient, self.shared_cfg, self.xkey = root, log, tclient, shared_cfg, xkey
        self.shared_http, self.find = shared_http, find
        self.q: "queue.Queue" = queue.Queue()
        self.master: dict = {}               # twitch_id -> gamertag in the master list
        self.running, self.busy, self.closing = True, set(), False
        self.after_ids = {}
        self.sort = {"col": "last_seen", "desc": True}
        self.pal = palette(clamp(None))
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self._poll_queue)
        self.fill()
        self.download_master()
        self.round_now()
        self._set_awake()

    # ---- layout
    def _build(self):
        from .ui import App                  # same look as the main app
        r = self.root
        r.title("BRPW Stream log (owner tool)")
        r.geometry("1150x620")
        App._apply_style(self)
        top = ttk.Frame(r)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.run_btn = ttk.Button(top, text="Pause logging", command=self.toggle)
        self.run_btn.pack(side="left")
        ttk.Button(top, text="Check now", command=self.round_now).pack(side="left", padx=4)
        self.awake = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Keep the PC awake while logging", variable=self.awake,
                        command=self._set_awake).pack(side="left", padx=10)
        self.state = tk.StringVar()
        ttk.Label(top, textvariable=self.state, style="Hint.TLabel").pack(side="right")
        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Label(bar, text="Show").pack(side="left")
        self.view = tk.StringVar(value=next(iter(VIEWS)))
        cb = ttk.Combobox(bar, textvariable=self.view, values=list(VIEWS), state="readonly", width=30)
        cb.pack(side="left", padx=6)
        ttk.Label(bar, text="Search").pack(side="left", padx=(10, 4))
        self.search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search, width=24).pack(side="left")
        for v in (self.view, self.search):
            v.trace_add("write", lambda *a: self.fill())
        self.counts = tk.StringVar()
        ttk.Label(bar, textvariable=self.counts).pack(side="right")
        box = ttk.Frame(r)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        self.cols = (("status", "Status", 170), ("name", "Channel", 150), ("last_seen", "Last live", 110),
                     ("first_seen", "First seen", 110), ("peak", "Peak viewers", 85), ("seen", "Rounds", 60),
                     ("language", "Lang", 45), ("title", "Last title", 360))
        self.tv = ttk.Treeview(box, columns=[c[0] for c in self.cols], show="headings", selectmode="extended")
        for c, t, w in self.cols:
            self.tv.heading(c, text=t, command=lambda c=c: self.sort_by(c))
            self.tv.column(c, width=w, anchor="e" if c in ("peak", "seen") else "w", stretch=c == "title")
        self.tv.tag_configure("master", background=self.pal["friend"])
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv.pack(side="left", fill="both", expand=True)
        self.tv.bind("<Double-1>", lambda e: self.open_sotrep())
        self.tv.bind("<Return>", lambda e: self.open_sotrep())
        acts = ttk.Frame(r)
        acts.pack(side="bottom", fill="x", padx=8, pady=(4, 8), before=box)   # buttons stay visible
        for text, cmd in (("Open SOTREP", self.open_sotrep), ("Open Twitch", self.open_twitch),
                          ("Add to master list...", self.add_to_master), ("Mark checked", lambda: self.mark("checked")),
                          ("Skip", lambda: self.mark("skip")), ("Back to new", lambda: self.mark("new"))):
            ttk.Button(acts, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Label(acts, text="Double-click opens SOTREP's search for that channel", style="Hint.TLabel").pack(
            side="right")

    # ---- table
    def _status_text(self, r):
        if r["twitch_id"] in self.master:
            return "◆ " + self.master[r["twitch_id"]]
        return {"linked": f"linked: {r['gamertag']}", "checked": "checked", "skip": "skip"}.get(r["status"], "new")

    def sort_by(self, col):
        self.sort.update(desc=not self.sort["desc"] if self.sort["col"] == col else col in ("last_seen", "peak", "seen"),
                         col=col)
        self.fill()

    def fill(self):
        now = time.time()
        sel = set(self.tv.selection())
        top = self.tv.yview()[0]
        self.tv.delete(*self.tv.get_children())
        keep, f = VIEWS[self.view.get()], self.search.get().strip().casefold()
        rows = [r for r in self.log.channels() if keep(r, self.master)]
        if f:
            rows = [r for r in rows if any(f in (x or "").casefold() for x in
                                           (r["name"], r["login"], r["title"], r["gamertag"], self._status_text(r)))]
        key = {"status": lambda r: self._status_text(r).casefold(), "name": lambda r: r["name"].casefold(),
               "title": lambda r: (r["title"] or "").casefold(), "language": lambda r: r["language"] or ""}.get(
            self.sort["col"], lambda r: r[self.sort["col"]] or 0)
        for r in sorted(rows, key=key, reverse=self.sort["desc"]):
            self.tv.insert("", "end", iid=r["twitch_id"], tags=("master",) if r["twitch_id"] in self.master else (),
                           values=(self._status_text(r), r["name"], ago(r["last_seen"], now), ago(r["first_seen"], now),
                                   r["peak"], r["seen"], r["language"] or "", r["title"] or ""))
        self.tv.selection_set([i for i in sel if self.tv.exists(i)])
        self.tv.yview_moveto(top)
        c = self.log.counts(self.master)
        self.counts.set(f"{len(rows)} shown  |  {c['total']} channels, {c['todo']} to do, {c['master']} in master list, "
                        f"{c['linked']} linked here, {c['checked']} checked, {c['skip']} skipped")
        self._show_state()

    def _show_state(self):
        last, first = self.log.last_round(), self.log.first_round()
        if not self.tclient.creds.load():
            text = "Twitch is not set up: use 'Twitch setup...' in the main app first."
        elif last is None:
            text = "Waiting for the first round..."
        elif last["error"]:
            text = f"Last round failed ({ago(last['at'], time.time())}): {last['error']}"
        else:
            text = f"Last round {ago(last['at'], time.time())}: {last['streams']} live"
        since = f"  |  logging since {time.strftime('%Y-%m-%d %H:%M', time.localtime(first))}" if first else ""
        self.state.set(("" if self.running else "PAUSED  |  ") + text + since + f"  |  every {POLL_EVERY // 60} min")

    def _selected(self):
        return [r for r in self.log.channels() if r["twitch_id"] in set(self.tv.selection())]

    # ---- background work
    def _bg(self, name, work, done):
        if name in self.busy:
            return
        self.busy.add(name)

        def run():
            try:
                res, err = work(), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:
                res, err = None, f"Unexpected error: {e}"
            self.q.put((name, done, res, err))
        threading.Thread(target=run, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                name, done, res, err = self.q.get_nowait()
                self.busy.discard(name)
                done(res, err)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(200, self._poll_queue)

    def _later(self, name, ms, fn):
        if self.after_ids.get(name):
            self.root.after_cancel(self.after_ids[name])
        self.after_ids[name] = None if self.closing else self.root.after(ms, fn)

    def round_now(self):
        """One round: everyone live in Sea of Thieves right now."""
        if self.tclient.creds.load():
            def done(streams, err):
                if err:
                    self.log.record_error(err)
                else:
                    self.log.record(streams)
                self.fill()
            self._bg("round", self.tclient.game_streams, done)
        else:
            self._show_state()
        self._later("round", POLL_EVERY * 1000, self._tick)

    def _tick(self):
        if self.running:
            self.round_now()
        else:
            self._later("round", POLL_EVERY * 1000, self._tick)

    def toggle(self):
        self.running = not self.running
        self.run_btn.configure(text="Pause logging" if self.running else "Start logging")
        self._set_awake()
        if self.running:
            self.round_now()
        self._show_state()

    def _set_awake(self):
        keep_awake(self.running and self.awake.get())

    def download_master(self):
        svc = self.shared_cfg.service()
        if svc:
            def done(rows, err):
                if not err:
                    self.master = {m["twitch_id"]: m["gamertag"] for m in rows}
                    self.fill()
            self._bg("master", SharedClient(svc["url"], svc["key"], http=self.shared_http).master, done)
        self._later("master", MASTER_EVERY * 1000, self.download_master)

    # ---- actions
    def open_sotrep(self):
        for r in self._selected()[:10]:
            webbrowser.open(sotrep.twitch_search_url(r["login"]))

    def open_twitch(self):
        for r in self._selected()[:10]:
            webbrowser.open(f"https://www.twitch.tv/{r['login']}")

    def mark(self, status):
        rows = self._selected()
        self.log.set_status([r["twitch_id"] for r in rows], status)
        self.fill()

    def add_to_master(self, gamertag=None, confirm=True):
        rows = self._selected()
        svc, secret = self.shared_cfg.service(), self.shared_cfg.admin_key()
        if len(rows) != 1:
            return messagebox.showinfo("Add to master list", "Select one channel.", parent=self.root)
        if not (svc and secret):
            return messagebox.showinfo("Add to master list", "Only on the list owner's PC (Owner setup in the main "
                                       "app's Shared list window).", parent=self.root)
        try:
            key = self.xkey.get()
        except Exception:
            key = None
        if not key:
            return messagebox.showinfo("Add to master list", "Add an OpenXBL account in the main app first; it is "
                                       "used to look up the gamertag's Xbox ID.", parent=self.root)
        ch = rows[0]
        typed = gamertag if gamertag is not None else simpledialog.askstring(
            "Add to master list", f"Xbox gamertag of Twitch channel {ch['name']}\n(e.g. from SOTREP):", parent=self.root)
        if not typed or not typed.strip():
            return
        owner = SharedClient(svc["url"], secret, http=self.shared_http)

        def found(res, err):
            if err:
                return messagebox.showerror("Add to master list", err, parent=self.root)
            player, near = res
            if player is None:
                return messagebox.showinfo("Add to master list", f"No Xbox account named exactly '{typed}'."
                                           + (f"\nSimilar: {', '.join(near)}" if near else ""), parent=self.root)
            if confirm and not messagebox.askyesno("Add to master list", (
                    f"Add to the master list for everyone?\n\nXbox: {player.gamertag} (XUID {player.xuid})\n"
                    f"Twitch: {ch['name']}"), parent=self.root):
                return
            entry = {"xuid": player.xuid, "gamertag": player.gamertag, "twitch_id": ch["twitch_id"],
                     "twitch_login": ch["login"], "twitch_name": ch["name"]}

            def published(_, err2):
                if err2:
                    return messagebox.showerror("Add to master list", err2, parent=self.root)
                self.log.set_status([ch["twitch_id"]], "linked", player.gamertag, player.xuid)
                self.master[ch["twitch_id"]] = player.gamertag
                self.fill()
            self._bg("publish", lambda: owner.publish([entry]), published)

        self._bg("lookup", lambda: self.find(key, typed.strip()), found)

    def on_close(self):
        self.closing = True
        keep_awake(False)
        for i in self.after_ids.values():
            if i:
                self.root.after_cancel(i)
        self.root.destroy()


def main():
    log = StreamLog(data_dir() / "stream_log.db")
    root = tk.Tk()
    StreamLogApp(root, log, TwitchClient(TwitchCreds(data_dir() / "twitch.json")), SharedConfig(data_dir()),
                 OpenXblKey(data_dir() / "openxbl_keys.json"))
    root.mainloop()
    log.close()
