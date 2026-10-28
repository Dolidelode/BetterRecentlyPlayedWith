"""Shared Twitch list windows (mixed into App): download the master list, send own links as suggestions, and the
owner-only review screen. Everything runs in the app; nothing opens a browser except the Open Twitch/SOTREP buttons."""
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import sotrep
from .providers import ProviderError
from .shared import SharedClient, new_sender_id

SYNC_EVERY = 6 * 3600 * 1000          # master list download interval (ms)
SHARED_MARK = " ◆"                # shown after a Twitch name that came from the master list


class SharedListMixin:
    # ---- plumbing
    def _shared_client(self, admin=False):
        svc = self.shared_cfg.service()
        if not svc:
            return None
        if admin:
            key = self.shared_cfg.admin_key()
            return SharedClient(svc["url"], key, http=self.shared_http) if key else None
        return SharedClient(svc["url"], svc["key"], http=self.shared_http)

    def is_list_owner(self) -> bool:
        return bool(self.shared_cfg.service() and self.shared_cfg.admin_key())

    def _bg(self, name, work, done):
        """Run work() in a thread, then done(result, error_text) on the Tk thread."""
        self._set_busy(name, True)

        def run():
            try:
                res, err = work(), None
            except ProviderError as e:
                res, err = None, str(e)
            except Exception as e:                      # never let a worker thread die silently
                res, err = None, f"Unexpected error: {e}"

            def finish():
                self._set_busy(name, False)
                done(res, err)
            self.q.put(("call", finish))

        threading.Thread(target=run, daemon=True).start()

    def sender_id(self) -> str:
        sid = self.db.get_setting("share_id")
        if not sid:
            sid = new_sender_id()
            self.db.set_setting("share_id", sid)
        return sid

    # ---- download the master list
    def _schedule_shared(self, delay_ms=SYNC_EVERY):
        if getattr(self, "shared_after_id", None):
            self.root.after_cancel(self.shared_after_id)
            self.shared_after_id = None
        if not self.closing:
            self.shared_after_id = self.root.after(delay_ms, self._shared_tick)

    def _shared_tick(self):
        self.shared_after_id = None
        if self.db.get_setting("shared_sync", "1") == "1":
            self.shared_sync()
        self._schedule_shared()

    def shared_sync(self, then=None, quiet=True):
        client = self._shared_client()
        if client is None:
            return
        if getattr(self, "shared_busy", False):
            return
        self.shared_busy = True

        def done(rows, err):
            self.shared_busy = False
            if err:
                if not quiet:
                    self.set_status(f"Shared list download failed: {err}", error=True)
            else:
                before = self.db.shared_count()
                self.db.replace_shared(rows)
                self.db.set_setting("shared_synced", str(int(time.time())))
                if not quiet or self.db.shared_count() != before:
                    self.set_status(f"Shared list: {len(rows)} Twitch channel(s) in the master list.")
                self.reload()
            self._refresh_shared_win()
            if then:
                then(err)

        self._bg("shared", client.master, done)

    # ---- the "Shared list..." window
    def shared_dialog(self):
        win = getattr(self, "_shared_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return win
        win = tk.Toplevel(self.root)
        self._shared_win = win
        win.title("Shared Twitch list")
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        ttk.Label(win, style="Hint.TLabel", wraplength=540, justify="left", text=(
            "Everyone using this app gets the same master list of Xbox players and their Twitch channels. "
            "Players in your history get their channel from it automatically (marked ◆). Your own links always "
            "win, and removing a ◆ link keeps it removed for you.\n\n"
            "Linked someone the master list doesn't have? Send your links: the list owner checks them before they "
            "are added. What is sent: gamertag, XUID, Twitch channel, the name below (optional) and a random ID of "
            "this copy of the app. Nothing else.")).pack(fill="x", padx=14, pady=(14, 6))
        state = tk.StringVar()
        ttk.Label(win, textvariable=state, wraplength=540, justify="left").pack(fill="x", padx=14, pady=4)
        form = ttk.Frame(win)
        form.pack(fill="x", padx=14, pady=4)
        ttk.Label(form, text="Your name (optional, shown to the list owner)").pack(side="left")
        name_var = tk.StringVar(value=self.db.get_setting("share_name", ""))
        ttk.Entry(form, textvariable=name_var, width=22).pack(side="left", padx=6)
        name_var.trace_add("write", lambda *a: self.db.set_setting("share_name", name_var.get().strip()[:40]))
        auto_var = tk.BooleanVar(value=self.db.get_setting("shared_sync", "1") == "1")
        ttk.Checkbutton(win, text="Download the master list automatically (at start and every 6 hours)",
                        variable=auto_var, command=lambda: self.db.set_setting(
                            "shared_sync", "1" if auto_var.get() else "0")).pack(anchor="w", padx=14)
        row = ttk.Frame(win)
        row.pack(pady=(8, 4))
        send_btn = ttk.Button(row, text="Send my links", command=self.send_links)
        ttk.Button(row, text="Download now", command=lambda: self.shared_sync(quiet=False)).pack(side="left", padx=4)
        send_btn.pack(side="left", padx=4)
        owner = ttk.Frame(win)
        owner.pack(fill="x", padx=14, pady=(8, 4))
        owner_lbl = ttk.Label(owner, text="List owner:")
        review_btn = ttk.Button(owner, text="Review suggestions...", command=self.review_dialog)
        publish_btn = ttk.Button(owner, text="Publish my links", command=self.publish_links)
        bottom = ttk.Frame(win)
        bottom.pack(side="bottom", pady=(4, 12))
        setup_btn = ttk.Button(bottom, text="Owner setup...", command=self.shared_setup_dialog)
        close_btn = ttk.Button(bottom, text="Close", command=win.destroy)
        close_btn.pack(side="left", padx=4)
        # Owner setup is only shown on the owner's PC; anyone else reaches it with Ctrl+Shift+O
        win.bind("<Control-Shift-O>", lambda e: self.shared_setup_dialog())
        win.bind("<Control-Shift-o>", lambda e: self.shared_setup_dialog())
        win.setup_btn, win.close_btn = setup_btn, close_btn
        win.state_var, win.send_btn, win.owner_frame = state, send_btn, owner      # kept for tests
        win.review_btn, win.publish_btn, win.name_var, win.owner_lbl = review_btn, publish_btn, name_var, owner_lbl
        self._refresh_shared_win()
        self._place_dialog(win, 580, 400)
        return win

    def _refresh_shared_win(self):
        win = getattr(self, "_shared_win", None)
        if win is None or not win.winfo_exists():
            return
        configured = self.shared_cfg.service() is not None
        synced = self.db.get_setting("shared_synced")
        new = self.db.links_to_share()
        if not configured:
            text = "This copy of the app has no shared list set up."
        else:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(synced))) if synced else "not yet"
            text = (f"Master list: {self.db.shared_count()} channel(s), downloaded {when}.\n"
                    f"Your links the master list doesn't have yet (not sent before): {len(new)}.")
        win.state_var.set(text)
        win.send_btn.configure(text=f"Send my links ({len(new)})", state="normal" if configured and new else "disabled")
        for w in (win.owner_lbl, win.review_btn, win.publish_btn, win.setup_btn):
            w.pack_forget()
        if self.is_list_owner():
            mine = self.db.links_to_share(include_sent=True)
            win.publish_btn.configure(text=f"Publish my links ({len(mine)})", state="normal" if mine else "disabled")
            for w in (win.owner_lbl, win.review_btn, win.publish_btn):
                w.pack(side="left", padx=4)
            win.setup_btn.pack(side="left", padx=4, before=win.close_btn)

    def send_links(self, confirm=True):
        client = self._shared_client()
        rows = self.db.links_to_share()
        if client is None or not rows:
            return self.set_status("Nothing to send: all your links are already in the master list or sent.")
        if confirm and not messagebox.askyesno("Send links", (
                f"Send {len(rows)} link(s) to the list owner for review?\n\n"
                "Sent: gamertag, XUID, Twitch channel, your optional name and a random ID of this copy."),
                parent=getattr(self, "_shared_win", None) or self.root):
            return
        sid, name = self.sender_id(), self.db.get_setting("share_name", "")

        def done(n, err):
            if err:
                return self.set_status(f"Sending failed: {err}", error=True)
            self.db.mark_sent(rows)
            self.set_status(f"Sent {n} link(s). The list owner reviews them; approved ones arrive with the next download.")
            self._refresh_shared_win()

        self._bg("sharesend", lambda: client.submit(rows, sid, name), done)

    # ---- owner tools
    def publish_links(self):
        client = self._shared_client(admin=True)
        rows = self.db.links_to_share(include_sent=True)
        if client is None or not rows:
            return
        if not messagebox.askyesno("Publish", f"Add or replace {len(rows)} of your own link(s) in the master list?",
                                   parent=getattr(self, "_shared_win", None) or self.root):
            return

        def done(n, err):
            if err:
                return self.set_status(f"Publishing failed: {err}", error=True)
            self.set_status(f"Published {n} link(s) to the master list.")
            self.shared_sync(quiet=False)

        self._bg("sharepub", lambda: client.publish(rows), done)

    def remove_from_master(self):
        client = self._shared_client(admin=True)
        xuids = [a["xuid"] for a in self.db.account_details(self._selected_ids()) if a["xuid"]]
        found = list(self.db.shared_entry(xuids))
        if client is None or not found:
            return self.set_status("None of the selected players are in the master list.")
        if not messagebox.askyesno("Master list", f"Remove {len(found)} player(s) from the master list for everyone?"):
            return

        def done(_, err):
            if err:
                return self.set_status(f"Removing failed: {err}", error=True)
            self.set_status(f"Removed {len(found)} player(s) from the master list.")
            self.shared_sync(quiet=False)

        self._bg("shareremove", lambda: client.remove(found), done)

    def _check_text(self, s, by_pair, by_xuid, master, mine):
        """Hints for one suggestion, from data already here (no requests)."""
        out = []
        m = master.get(s["xuid"])
        if m is not None and m["twitch_id"] != s["twitch_id"]:
            out.append(f"master has {m['display_name']}")
        own = mine.get(s["xuid"])
        if own is not None:
            if own.get("twitch_id") == s["twitch_id"]:
                out.append("same as your link")
            elif own.get("twitch_id"):
                out.append(f"your link: {own['twitch_name']}")
            elif own.get("never") is not None:
                out.append("you marked never link")
            else:
                out.append("in your history" + (f" as {own['gamertag']}" if own["gamertag"] != s["gamertag"] else ""))
        senders = by_pair.get((s["xuid"], s["twitch_id"]), 1)
        if senders > 1:
            out.append(f"sent by {senders}")
        if by_xuid.get(s["xuid"], 1) > 1:
            out.append("other channels suggested too")
        return ", ".join(out)

    def review_dialog(self):
        client = self._shared_client(admin=True)
        if client is None:
            return self.set_status("Only the list owner can review suggestions (Owner setup...).", error=True)
        win = tk.Toplevel(self.root)
        win.title("Review suggestions")
        win.configure(bg=self.pal["bg"])
        ttk.Label(win, style="Hint.TLabel", wraplength=1000, justify="left", text=(
            "Suggestions from other copies of the app. Nothing reaches the master list until you approve it. "
            "'Check Twitch' asks Twitch whether the channel still exists under that name. Blocking a sender rejects "
            "all their waiting suggestions and refuses new ones from that copy.")).pack(fill="x", padx=10, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        cols = (("gamertag", "Gamertag", 150), ("xuid", "XUID", 130), ("twitch", "Twitch", 140),
                ("sender", "Sender", 120), ("record", "Sender's record", 140), ("check", "Notes", 330))
        tv = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=14)
        for c, t, w in cols:
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.tag_configure("warn", foreground="#ff7b72")
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        state = tk.StringVar(value="Loading...")
        ttk.Label(win, textvariable=state).pack(fill="x", padx=10)
        subs = {}

        def fill(data):
            pending, stats = data
            tv.delete(*tv.get_children())
            subs.clear()
            master = self.db.shared_entry([s["xuid"] for s in pending])
            mine = {}
            for a in self.db.list_accounts():
                if a["xuid"]:
                    own = a["twitch_source"] != "shared"         # a master-list link is not "your link"
                    mine[a["xuid"]] = {"gamertag": a["gamertag"], "twitch_id": a["twitch_id"] if own else None,
                                       "twitch_name": a["twitch_name"], "never": a["twitch_never"]}
            by_pair, by_xuid = {}, {}
            for s in pending:
                by_pair[(s["xuid"], s["twitch_id"])] = by_pair.get((s["xuid"], s["twitch_id"]), 0) + 1
            for x, _t in by_pair:
                by_xuid[x] = by_xuid.get(x, 0) + 1
            for s in pending:
                st = stats.get(s["submitter"], {})
                note = self._check_text(s, by_pair, by_xuid, master, mine)
                warn = st.get("rejected", 0) > st.get("approved", 0) or "master has" in note or "your link:" in note
                iid = tv.insert("", "end", tags=("warn",) if warn else (), values=(
                    s["gamertag"], s["xuid"], s["twitch_name"] or s["twitch_login"],
                    s["submitter_name"] or s["submitter"][:8],
                    f"{st.get('approved', 0)} ok, {st.get('rejected', 0)} rejected", note))
                subs[iid] = s
            state.set(f"{len(pending)} suggestion(s) waiting." if pending else "No suggestions waiting.")

        def load():
            state.set("Loading...")
            self._bg("review", lambda: (client.pending(), client.sender_stats()),
                     lambda data, err: state.set(f"Loading failed: {err}") if err else
                     (fill(data) if win.winfo_exists() else None))

        def chosen():
            return [subs[i] for i in tv.selection() if i in subs]

        def act(label, work):
            sel = chosen()
            if not sel:
                return state.set("Select one or more suggestions first.")

            def done(_, err):
                if err:
                    return state.set(f"{label} failed: {err}")
                self.set_status(f"{label}: {len(sel)} suggestion(s).")
                self.shared_sync(quiet=True)
                if win.winfo_exists():
                    load()
            self._bg("review", lambda: work(sel), done)

        def approve():
            sel = chosen()
            dup = {}
            for s in sel:
                dup.setdefault(s["xuid"], set()).add(s["twitch_id"])
            if any(len(v) > 1 for v in dup.values()):
                return state.set("You selected different channels for the same player. Approve only one of them.")
            act("Approved", client.approve)

        def block():
            sel = chosen()
            if not sel:
                return state.set("Select a suggestion from the sender to block.")
            senders = {s["submitter"]: s["submitter_name"] or s["submitter"][:8] for s in sel}
            if not messagebox.askyesno("Block sender", "Block " + ", ".join(senders.values()) +
                                       "?\nAll their waiting suggestions are rejected and new ones refused.",
                                       parent=win):
                return
            act("Blocked sender, rejected", lambda _sel: [client.block(sid, "blocked in review") for sid in senders])

        def check_twitch():
            sel = chosen() or list(subs.values())
            if not self.tcreds.configured():
                return state.set("Twitch is not set up on this PC (Twitch setup...).")

            def done(found, err):
                if err:
                    return state.set(f"Twitch check failed: {err}")
                for iid, s in subs.items():
                    if s not in sel or not tv.exists(iid):
                        continue
                    u = found.get(s["twitch_login"].lower())
                    if u is None:
                        res = "Twitch: no channel with this name"
                    elif u.id != s["twitch_id"]:
                        res = "Twitch: this name belongs to a DIFFERENT channel now"
                    else:
                        res = "Twitch OK"
                    note = tv.set(iid, "check")
                    tv.set(iid, "check", res + (", " + note if note else ""))
                    if res != "Twitch OK":
                        tv.item(iid, tags=("warn",))
                state.set(f"Checked {len(sel)} channel(s) on Twitch.")

            self._bg("review", lambda: self.tclient.users_by_login([s["twitch_login"] for s in sel]), done)

        def open_twitch():
            for s in chosen():
                webbrowser.open(f"https://www.twitch.tv/{s['twitch_login']}")

        def open_sotrep():
            for s in chosen():
                webbrowser.open(sotrep.profile_url(s["gamertag"]))

        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=10, pady=(4, 10), before=box)      # buttons first: always visible
        ttk.Button(bar, text="Approve", command=approve).pack(side="left")
        ttk.Button(bar, text="Reject", command=lambda: act("Rejected", lambda sel: client.set_status(
            [s["id"] for s in sel], "rejected"))).pack(side="left", padx=4)
        ttk.Button(bar, text="Block sender...", command=block).pack(side="left", padx=4)
        ttk.Button(bar, text="Check Twitch", command=check_twitch).pack(side="left", padx=(16, 4))
        ttk.Button(bar, text="Open Twitch", command=open_twitch).pack(side="left", padx=4)
        ttk.Button(bar, text="Open SOTREP", command=open_sotrep).pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Refresh", command=load).pack(side="right", padx=4)
        tv.bind("<Double-1>", lambda e: open_twitch())
        win.review_tv, win.state_var, win.load, win.approve, win.block = tv, state, load, approve, block  # tests
        win.check_twitch, win.subs = check_twitch, subs
        win.minsize(820, 300)
        self._place_dialog(win, 1080, 520)
        load()
        return win

    def shared_setup_dialog(self):
        """Only needed by the list owner (or someone running their own list)."""
        win = tk.Toplevel(self.root)
        win.title("Shared list: owner setup")
        win.configure(bg=self.pal["bg"])
        win.transient(self.root)
        ttk.Label(win, style="Hint.TLabel", wraplength=540, justify="left", text=(
            "Only for the owner of a shared list. Normal users don't need this: their copy already knows where the "
            "list is.\n\nProject URL and publishable key: leave empty to use the list built into this program, or "
            "paste them from your own Supabase project (Settings > API Keys). Secret key: only on YOUR PC. It turns "
            "on the owner tools. It is stored in your app-data folder, never in the repository or the program file. "
            "See docs/SHARED_LIST.md.")).pack(fill="x", padx=14, pady=(14, 6))
        form = ttk.Frame(win)
        form.pack(padx=14, pady=6)
        svc = self.shared_cfg.own_service() or {}       # the built-in address is never shown
        url_var, key_var, sec_var = tk.StringVar(value=svc.get("url", "")), tk.StringVar(value=svc.get("key", "")), \
            tk.StringVar()
        for n, (label, var, show) in enumerate((("Project URL", url_var, ""), ("Publishable key", key_var, ""),
                                                ("Secret key (owner)", sec_var, "*"))):
            ttk.Label(form, text=label).grid(row=n, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=var, width=48, show=show).grid(row=n, column=1, padx=8)
        state = tk.StringVar(value="Owner tools are ON on this PC." if self.shared_cfg.admin_key()
                             else "Owner tools are off on this PC.")
        ttk.Label(win, textvariable=state, style="Hint.TLabel", wraplength=540).pack(fill="x", padx=14, pady=4)
        row = ttk.Frame(win)
        row.pack(pady=(4, 14))

        def save():
            url, key, sec = url_var.get().strip(), key_var.get().strip(), sec_var.get().strip()
            built_in = None
            if not url and not key and not self.shared_cfg.own_service():
                built_in = self.shared_cfg.service()        # the bundled list; used, but never saved or shown
            if built_in:
                url, key = built_in["url"], built_in["key"]
            elif not url.startswith("https://") or not key:
                return state.set("Paste the project URL (https://...) and the publishable key.")
            if sec and sec == key:
                return state.set("The secret key must be the SECRET one, not the publishable key.")
            state.set("Testing...")
            probe = SharedClient(url, sec or key, http=self.shared_http)

            def done(_, err):
                if err:
                    return state.set(f"Not saved: {err}")
                if not built_in:
                    self.shared_cfg.save_service(url, key)
                if sec:
                    self.shared_cfg.save_admin_key(sec)
                state.set("Saved." + (" Owner tools are ON on this PC." if self.shared_cfg.admin_key() else ""))
                self._refresh_shared_win()
                self.shared_sync(quiet=False)

            self._bg("sharesetup", (lambda: probe.pending()) if sec else probe.master, done)

        def owner_off():
            self.shared_cfg.save_admin_key(None)
            state.set("Secret key removed from this PC. Owner tools are off.")
            self._refresh_shared_win()

        ttk.Button(row, text="Save and test", command=save).pack(side="left", padx=4)
        ttk.Button(row, text="Remove secret key", command=owner_off).pack(side="left", padx=4)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=4)
        win.url_var, win.key_var, win.sec_var, win.save, win.state_var = url_var, key_var, sec_var, save, state
        self._place_dialog(win, 580, 330)
        return win
