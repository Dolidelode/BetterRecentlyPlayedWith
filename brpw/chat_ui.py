"""Drawn widgets for Messages: rounded buttons, a rounded search/compose box, a chat with rounded bubbles and a
conversation list with avatars. Tk has no rounded widgets, so these are drawn on canvases."""
import time
import tkinter as tk
import zlib
from tkinter import font as tkfont
from tkinter import ttk


def _mix(a: str, b: str, t: float) -> str:
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def round_rect(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """A rectangle with rounded corners (a smoothed polygon). A spline only rounds about half the corner it is
    given, so the corner points sit further in than r."""
    r = max(0, min(r * 1.7, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2,
           x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, splinesteps=16, **kw)


AVATAR_COLORS = ("#e0794c", "#d6a13b", "#5fb36b", "#3aa7a3", "#4f8fdc", "#8a6fd8", "#c763a8", "#d65c5c")


def avatar_color(seed: str) -> str:
    return AVATAR_COLORS[zlib.crc32(str(seed).encode()) % len(AVATAR_COLORS)]


PICTURE_HOST = "images-eds-ssl.xboxlive.com"       # Xbox's own picture server; no other address is ever fetched
PICTURE_SIZE = 40
SERVER_SIZES = (40, 64)          # sizes Xbox's picture server makes (24, 32, 48 answer "Bad Request")


def picture_url(raw: str, size: int = PICTURE_SIZE) -> str:
    """Xbox's gamerpic address (displayPicRaw) as a small PNG, or "" when it isn't on Xbox's picture server."""
    import urllib.parse
    try:
        p = urllib.parse.urlsplit(str(raw or ""))
    except ValueError:
        return ""
    if p.scheme != "https" or p.netloc != PICTURE_HOST:
        return ""
    q = dict(urllib.parse.parse_qsl(p.query))
    q.update(format="png", w=str(size), h=str(size))           # replaces the jpeg format the address comes with
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))


def make_round(img: tk.PhotoImage) -> tk.PhotoImage:
    """Make the corners outside the circle transparent (Tk can do that per pixel; no image library needed)."""
    w, h = img.width(), img.height()
    r = min(w, h) / 2
    for y in range(h):
        dy = y + 0.5 - h / 2
        for x in range(w):
            dx = x + 0.5 - w / 2
            if dx * dx + dy * dy > r * r:
                img.transparency_set(x, y, True)
    return img


class Pictures:
    """Profile pictures by address: downloaded in the background (4 at a time), cut round, kept in memory and on
    disk (folder), so they show at once next time. get() returns the picture or None and calls on_ready later.
    post(fn) must run fn on the Tk thread (the app's queue)."""

    def __init__(self, folder, post, fetch=None):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.folder, self.post = folder, post
        self.fetch = fetch or self._download
        self.images, self.waiting, self.failed = {}, {}, set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4)

    @staticmethod
    def _download(url):
        import urllib.request
        from .providers import UA
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            data = r.read(200_000)
        if not data.startswith(b"\x89PNG"):
            raise ValueError("not a PNG")
        return data

    def _file(self, url):
        import hashlib
        return self.folder / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".png") if self.folder else None

    def get(self, raw, on_ready=None, size=PICTURE_SIZE):
        """Sizes the server doesn't make are fetched at 64 and shrunk by a whole factor (32 -> 64 / 2)."""
        server = size if size in SERVER_SIZES else 64
        url = picture_url(raw, server)
        k = (url, max(1, server // max(1, size)))
        if not url or k in self.failed:
            return None
        if k in self.images:
            return self.images[k]
        f = self._file(url)
        if f is not None and f.exists():
            try:
                self.images[k] = self._make(tk.PhotoImage(file=str(f)), k[1])
                return self.images[k]
            except (tk.TclError, OSError):
                pass
        with self.lock:
            first = k not in self.waiting
            self.waiting.setdefault(k, [])
            if on_ready:
                self.waiting[k].append(on_ready)
        if first:
            self.pool.submit(self._load, k, f)
        return None

    @staticmethod
    def _make(img, factor):
        return make_round(img.subsample(factor) if factor > 1 else img)

    def _load(self, k, f):
        try:
            data = self.fetch(k[0])
        except Exception:
            data = None
        if data and f is not None:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            except OSError:
                pass
        self.post(lambda: self._ready(k, data))

    def _ready(self, k, data):
        with self.lock:
            calls = self.waiting.pop(k, [])
        if not data:
            self.failed.add(k)
            return
        try:
            self.images[k] = self._make(tk.PhotoImage(data=data), k[1])
        except tk.TclError:
            self.failed.add(k)
            return
        for c in calls:
            try:
                c()
            except tk.TclError:                    # the window that asked is gone
                pass


def draw_avatar(c: tk.Canvas, cx, cy, rad, name, seed, pal, img=None, dim=False):
    """The profile picture when there is one, else a coloured circle with the first letter."""
    if img is not None:
        return c.create_image(cx, cy, image=img)
    col = avatar_color(seed)
    c.create_oval(cx - rad, cy - rad, cx + rad, cy + rad, fill=_mix(col, pal["panel"], 0.6 if dim else 0.15),
                  outline="")
    return c.create_text(cx, cy, text=(str(name)[:1] or "?").upper(), fill="#ffffff",
                         font=("Segoe UI", max(9, int(rad * 0.65)), "bold"))


def confirm_card(parent, pal, text, yes_label, on_yes, danger=True):
    """Ask inside the window instead of a separate dialog: a rounded card at the top of parent with the question,
    a Yes button (red for deleting) and Cancel. Only one card per parent."""
    old = getattr(parent, "_confirm_card", None)
    if old is not None and old.winfo_exists():
        old.destroy()
    bg = parent.cget("bg")
    card = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
    body = _mix(pal["panel"], pal["fg"], 0.1)
    width = 420
    msg = tk.Label(card, text=text, bg=body, fg=pal["fg"], font=("Segoe UI", 10), wraplength=width - 40,
                   justify="left", anchor="w")
    btns = tk.Frame(card, bg=body)

    def close(_=None):
        if card.winfo_exists():
            card.destroy()

    def yes():
        close()
        on_yes()
    yes_btn = RoundButton(btns, yes_label, yes, pal, body, kind="danger" if danger else "accent", padx=16, pady=7)
    yes_btn.pack(side="right")
    RoundButton(btns, "Cancel", close, pal, body, padx=16, pady=7).pack(side="right", padx=(0, 8))
    card.update_idletasks()
    mh = msg.winfo_reqheight()
    height = mh + 82
    card.configure(width=width, height=height)
    round_rect(card, 2, 2, width - 2, height - 2, 16, fill=body, outline=pal["accent"], width=1.5)
    card.create_window(20, 18, window=msg, anchor="nw", width=width - 40)
    card.create_window(width - 18, height - 16, window=btns, anchor="se")
    card.place(relx=0.5, y=14, anchor="n")
    card.tk.call("raise", card._w)                      # (a Canvas.lift would raise a drawing, not the widget)
    parent._confirm_card = card
    card.yes, card.no, card.text = yes, close, text                     # for tests
    yes_btn.focus_set()
    return card


def when_label(ts) -> str:
    if not ts:
        return ""
    d, now = time.localtime(ts), time.localtime()
    if d[:3] == now[:3]:
        return time.strftime("%H:%M", d)
    if time.time() - ts < 6 * 86400:
        return time.strftime("%a", d)
    return time.strftime("%d %b" + (" %Y" if d.tm_year != now.tm_year else ""), d)


class RoundButton(tk.Canvas):
    """A pill-shaped button. kind: "accent" (filled), "soft" (subtle) or "danger"."""

    def __init__(self, parent, text, command, pal, bg, kind="soft", font=("Segoe UI", 9), padx=14, pady=6,
                 image=None):
        self.pal, self.kind, self.command, self.text, self.image = pal, kind, command, text, image
        self.font = tkfont.Font(font=font)
        self.padx, self.pady = padx, pady
        h = self.font.metrics("linespace") + 2 * pady
        super().__init__(parent, width=self._width(), height=h, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.disabled = self.hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.invoke())
        self._draw()

    def _colors(self):
        P = self.pal
        if self.disabled:
            return _mix(P["panel"], P["bg"], 0.3), P["muted"]
        if self.kind == "accent":
            return (P["fg"] if self.hover else P["accent"]), P["bg"]
        if self.kind == "danger":
            return ("#d65c5c" if self.hover else _mix(P["panel"], "#d65c5c", 0.35)), P["fg"]
        return (P["accent"] if self.hover else _mix(P["panel"], P["fg"], 0.08)), (P["bg"] if self.hover else P["fg"])

    def _width(self):
        return self.font.measure(self.text) + 2 * self.padx + (self.image.width() + 8 if self.image else 0)

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        fill, fg = self._colors()
        round_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=fill, outline="")
        if self.image:
            iw = self.image.width()
            self.create_image(self.padx - 6 + iw / 2, h / 2, image=self.image)
            self.create_text(self.padx - 6 + iw + 8, h / 2, text=self.text, fill=fg, font=self.font, anchor="w")
        else:
            self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    def _set_hover(self, on):
        self.hover = on and not self.disabled
        self._draw()

    def invoke(self):
        if not self.disabled and self.command:
            self.command()

    def state(self, spec=None):                       # the subset of ttk's state() the app uses
        for s in spec or []:
            if s == "disabled":
                self.disabled = True
            elif s == "!disabled":
                self.disabled = False
        self.configure(cursor="arrow" if self.disabled else "hand2")
        self._draw()
        return ("disabled",) if self.disabled else ()

    def set_text(self, text):
        self.text = text
        self.configure(width=self._width())
        self._draw()

    def set_image(self, image):
        self.image = image
        self.configure(width=self._width())
        self._draw()

    def set_kind(self, kind):
        self.kind = kind
        self._draw()

    def cget(self, key):
        return self.text if key == "text" else super().cget(key)


class PillSelect(RoundButton):
    """A pill showing the current choice with ▾; a click opens a menu of the choices (like a read-only combobox).
    variable holds the choice; on_change runs after the user picks one. extra: (label, command) items below."""

    def __init__(self, parent, pal, bg, variable, values=(), on_change=None, extra=(), prefix="", font=("Segoe UI", 9),
                 kind="soft", padx=14, pady=6, width=None, image_for=None):
        self.variable, self.values, self.on_change, self.extra, self.prefix = variable, list(values), on_change, \
            list(extra), prefix
        self.image_for = image_for                     # value -> a small picture (or None), for the pill and menu
        self.fixed_width = width
        super().__init__(parent, self._label(), self._open, pal, bg, kind=kind, font=font, padx=padx, pady=pady)
        variable.trace_add("write", lambda *a: self._sync())
        self._sync()

    def _label(self):
        return f"{self.prefix}{self.variable.get()}  ▾"

    def _sync(self):
        try:
            self.image = self.image_for(self.variable.get()) if self.image_for else None
            self.set_text(self._label())
            if self.fixed_width:
                self.configure(width=self.fixed_width)
                self._draw()
        except tk.TclError:
            pass

    def _open(self):
        P = self.pal
        m = tk.Menu(self, tearoff=0, bg=P["panel"], fg=P["fg"], selectcolor=P["fg"], bd=0,
                    activebackground=P["accent"], activeforeground=P["bg"], font=("Segoe UI", 9))
        for v in self.values:
            img = self.image_for(v) if self.image_for else None
            if img is not None:
                m.add_radiobutton(label="  " + v, value=v, variable=self.variable, command=self._picked, image=img,
                                  compound="left")
            else:
                m.add_radiobutton(label=v, value=v, variable=self.variable, command=self._picked)
        if self.extra:
            if self.values:
                m.add_separator()
            for label, cmd in self.extra:
                m.add_command(label=label, command=cmd)
        self.menu = m
        m.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + 2)

    def _picked(self):
        if self.on_change:
            self.on_change()

    def configure(self, cnf=None, **kw):                # accepts values=... like a combobox
        if "values" in kw:
            self.values = list(kw.pop("values") or ())
        return super().configure(cnf, **kw) if (cnf or kw) else None
    config = configure

    def cget(self, key):
        return tuple(self.values) if key == "values" else super().cget(key)


class RoundBox(tk.Canvas):
    """A rounded field holding a widget (an Entry or a Text); it fills the width of its parent."""

    def __init__(self, parent, pal, bg, make, height=36, radius=14):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0)
        self.pal, self.radius = pal, radius
        self.fill = _mix(pal["panel"], pal["fg"], 0.07)
        self.inner = make(self, self.fill)
        self.win = self.create_window(radius, 6, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._layout)
        self.inner.bind("<FocusIn>", lambda e: self._ring(True), add="+")
        self.inner.bind("<FocusOut>", lambda e: self._ring(False), add="+")
        self.focused = False

    def _ring(self, on):
        self.focused = on
        self._layout()

    def _layout(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        self.delete("bg")
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=self.fill, tags="bg",
                   outline=self.pal["accent"] if self.focused else "", width=1.5)
        self.tag_lower("bg")
        self.itemconfigure(self.win, width=w - 2 * self.radius, height=h - 12)


class ChatView(tk.Frame):
    """Messages as rounded bubbles: yours on the right in the accent colour, theirs on the left; a day label when
    the day changes; deleted messages as an outlined "Message deleted"."""

    def __init__(self, parent, pal, on_menu=None):
        super().__init__(parent, bg=pal["bg"])
        self.pal, self.on_menu = pal, on_menu
        self.canvas = tk.Canvas(self, bg=pal["bg"], highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.font = tkfont.Font(family="Segoe UI", size=10)
        self.italic = tkfont.Font(family="Segoe UI", size=10, slant="italic")
        self.small = tkfont.Font(family="Segoe UI", size=7)
        self.day_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.messages, self.other, self.note, self._width, self._job = [], "", "", 0, None
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    def _resized(self, e):
        if abs(e.width - self._width) > 2:
            self._width = e.width
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(40, self.layout)

    def set(self, messages, other_xuid, note=""):
        self.messages, self.other, self.note = list(messages), str(other_xuid), note
        self.layout(bottom=True)

    def texts(self):
        return ["Message deleted" if m.get("deleted") else m["text"] for m in self.messages]

    @staticmethod
    def _day(at):
        d, today = time.localtime(at), time.localtime()
        if d[:3] == today[:3]:
            return "Today"
        if d[:3] == time.localtime(time.time() - 86400)[:3]:
            return "Yesterday"
        return time.strftime("%A %d %B" + (" %Y" if d.tm_year != today.tm_year else ""), d)

    def layout(self, bottom=False):
        self._job = None
        c, P = self.canvas, self.pal
        at_end = bottom or (c.yview()[1] > 0.98)
        c.delete("all")
        width = max(320, c.winfo_width())
        maxw = int(width * 0.64)
        y = 16
        if self.note:
            c.create_text(width / 2, y + 10, text=self.note, fill=P["muted"], font=self.day_font)
            y += 32
        day = None
        theirs = _mix(P["panel"], P["fg"], 0.10)
        for i, m in enumerate(self.messages):
            d = self._day(m["at"]) if m.get("at") else ""
            if d and d != day:
                tw = self.day_font.measure(d) + 22
                round_rect(c, width / 2 - tw / 2, y, width / 2 + tw / 2, y + 22, 11, fill=_mix(P["bg"], P["fg"], 0.07))
                c.create_text(width / 2, y + 11, text=d, fill=P["muted"], font=self.day_font)
                y += 34
                day = d
            mine = m["sender"] != self.other
            deleted = m.get("deleted")
            tag = f"m{i}"
            text = "Message deleted" if deleted else m["text"]
            t = c.create_text(0, 0, text=text, anchor="nw", width=maxw - 28, tags=(tag,),
                              font=self.italic if deleted else self.font,
                              fill=P["muted"] if deleted else (P["bg"] if mine else P["fg"]))
            x1, y1, x2, y2 = c.bbox(t)
            bw, bh = (x2 - x1) + 28, (y2 - y1) + 16
            left = width - 16 - bw if mine else 16
            if deleted:
                bub = round_rect(c, left, y, left + bw, y + bh, 16, fill=P["bg"], outline=P["muted"], dash=(4, 3),
                                 tags=(tag,))
            else:
                bub = round_rect(c, left, y, left + bw, y + bh, 16, fill=P["accent"] if mine else theirs, outline="",
                                 tags=(tag,))
            c.tag_lower(bub, t)
            c.move(t, left + 14 - x1, y + 8 - y1)
            y += bh
            if m.get("at"):
                c.create_text(left + bw - 6 if mine else left + 6, y + 3, anchor="ne" if mine else "nw",
                              text=time.strftime("%H:%M", time.localtime(m["at"])), fill=P["muted"], font=self.small)
                y += 16
            y += 6
            if self.on_menu:
                c.tag_bind(tag, "<Button-3>", lambda e, m=m: self.on_menu(m, e))
        c.configure(scrollregion=(0, 0, width, y + 12))
        if at_end:
            c.yview_moveto(1.0)


class ConversationList(tk.Frame):
    """Conversations as rows: a round avatar with the first letter (a green dot when online), the name (bold when
    unread, grey when muted), the last message and its time, an accent dot for unread. Rows can also be a section
    heading or a note. Click selects, right-click asks on_menu."""

    ROW = 62

    def __init__(self, parent, pal, on_select, on_menu=None, pictures=None):
        super().__init__(parent, bg=pal["panel"])
        self.pal, self.on_select, self.on_menu, self.pictures = pal, on_select, on_menu, pictures
        self._redraw_job = None
        self.canvas = tk.Canvas(self, bg=pal["panel"], highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.name_font = tkfont.Font(family="Segoe UI", size=10)
        self.bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.small = tkfont.Font(family="Segoe UI", size=8)
        self.head = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.avatar_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        self.rows, self.selected, self.hover = [], None, None
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Button-3>", self._right)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"), add="+")

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    def set(self, rows):
        """rows: dicts with kind "chat" (xuid, gamertag, preview, at, unread, muted, state, mine) or
        "header"/"note" (text)."""
        self.rows = list(rows)
        self.draw()

    def _index(self, y):
        top = 0
        for i, r in enumerate(self.rows):
            h = self.ROW if r.get("kind", "chat") == "chat" else 30
            if top <= y < top + h:
                return i
            top += h
        return None

    def _row_at(self, e):
        i = self._index(self.canvas.canvasy(e.y))
        return (i, self.rows[i]) if i is not None and self.rows[i].get("kind", "chat") == "chat" else (None, None)

    def _motion(self, e):
        i, _ = self._row_at(e)
        self._set_hover(i)

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.draw()

    def _click(self, e):
        _, r = self._row_at(e)
        if r:
            self.select(r["xuid"])

    def _right(self, e):
        _, r = self._row_at(e)
        if r and self.on_menu:
            self.on_menu(r, e)

    def select(self, xuid, notify=True):
        self.selected = xuid
        self.draw()
        r = next((r for r in self.rows if r.get("kind", "chat") == "chat" and r["xuid"] == xuid), None)
        if r is not None:
            self.see(xuid)
            if notify:
                self.on_select(r)
        return r

    def see(self, xuid):
        top, total = 0, 0
        for r in self.rows:
            h = self.ROW if r.get("kind", "chat") == "chat" else 30
            if r.get("kind", "chat") == "chat" and r["xuid"] == xuid:
                top = total
            total += h
        if total:
            v0, v1 = self.canvas.yview()
            if not (v0 * total <= top <= v1 * total - self.ROW):
                self.canvas.yview_moveto(max(0, top - 40) / total)

    def draw(self):
        c, P = self.canvas, self.pal
        c.delete("all")
        width = max(200, c.winfo_width())
        y = 0
        for i, r in enumerate(self.rows):
            kind = r.get("kind", "chat")
            if kind != "chat":
                c.create_text(16, y + 16, anchor="w", text=r["text"], fill=P["muted"],
                              font=self.head if kind == "header" else self.small)
                y += 30
                continue
            sel, hov = r["xuid"] == self.selected, i == self.hover
            if sel or hov:
                round_rect(c, 6, y + 3, width - 6, y + self.ROW - 3, 14,
                           fill=_mix(P["panel"], P["accent"], 0.38 if sel else 0.16), outline="")
            name = r["gamertag"] if not str(r["gamertag"]).isdigit() else "Unknown player"
            cx, cy, rad = 32, y + self.ROW / 2, 20
            img = self.pictures.get(r.get("pic"), self._picture_arrived) if self.pictures else None
            draw_avatar(c, cx, cy, rad, name, r["xuid"], P, img, dim=r.get("muted"))
            if str(r.get("state", "")).casefold() == "online":
                c.create_oval(cx + 9, cy + 9, cx + 19, cy + 19, fill="#4cc764",
                              outline=_mix(P["panel"], P["accent"], 0.38) if sel else P["panel"], width=2)
            unread, muted = r.get("unread") and not r.get("muted"), r.get("muted")
            right = width - 14
            when = when_label(r.get("at"))
            c.create_text(right, y + 20, anchor="e", text=when, fill=P["accent"] if unread else P["muted"],
                          font=self.small)
            tx = cx + rad + 12
            avail = right - tx - self.small.measure(when) - 10
            nf = self.bold if unread else self.name_font
            label = name + ("  · muted" if muted else "")
            c.create_text(tx, y + 20, anchor="w", text=self._fit(label, nf, avail),
                          fill=P["muted"] if muted else P["fg"], font=nf)
            prev = (("You: " if r.get("mine") else "") + (r.get("preview") or "")).replace("\n", " ")
            c.create_text(tx, y + 41, anchor="w", text=self._fit(prev, self.small, right - tx - 18),
                          fill=P["fg"] if unread else P["muted"], font=self.small)
            if unread:
                c.create_oval(right - 9, y + 36, right - 1, y + 44, fill=P["accent"], outline="")
            y += self.ROW
        c.configure(scrollregion=(0, 0, width, max(y, 1)))

    def _picture_arrived(self):
        if self._redraw_job is None and self.winfo_exists():         # many arrive together: one redraw
            self._redraw_job = self.after(80, self._redraw)

    def _redraw(self):
        self._redraw_job = None
        self.draw()

    @staticmethod
    def _fit(text, f, width):
        if f.measure(text) <= width:
            return text
        while text and f.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"


class PlayerCards(tk.Frame):
    """The player list as cards: the profile picture, the name with tag chips and a Friend chip, a line with hours,
    last seen and source, and on the right the Twitch pill and the Xbox list position. Only the cards in view are
    drawn, so thousands of players stay fast.

    It answers the part of ttk.Treeview's interface the app uses (insert, delete, get_children, set, item,
    selection..., tag_configure, see, yview, bbox, identify_row/column/region, bind, ["displaycolumns"]), so the rest
    of the app treats it like the table it replaces. "Columns" are the card's fields; hiding one hides it on the
    cards. identify_column gives "#0" on the picture and the twitch field's number on the Twitch pill."""

    ROW = 66
    TOP = 6

    def __init__(self, parent, pal, columns, bg=None):
        super().__init__(parent, bg=bg or pal["bg"])
        self.pal = pal
        self._cols = tuple(columns)
        self._display = list(columns)
        self._order, self._data = [], {}
        self._sel, self._anchor, self._focus = [], None, ""
        self._tags = {}
        self._colcfg = {c: {"width": 100} for c in ("#0",) + self._cols}
        self._hover = None
        self._job = None
        self._zones = {}                                   # iid -> {"#0"|"twitch"|"name": (x1, y1, x2, y2)}
        self.canvas = tk.Canvas(self, bg=bg or pal["bg"], highlightthickness=0, bd=0, takefocus=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview_cmd)
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.name_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.meta_font = tkfont.Font(family="Segoe UI", size=9)
        self.chip_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.pill_font = tkfont.Font(family="Segoe UI", size=9)
        self.letter_font = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        c = self.canvas
        c.bind("<Configure>", lambda e: self._schedule())
        c.bind("<Button-1>", self._press)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", self._wheel))
        c.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"), add="+")
        c.bind("<Up>", lambda e: self._step(-1, e))
        c.bind("<Down>", lambda e: self._step(1, e))

    # ---- the Treeview-like interface ----
    def __getitem__(self, key):
        if key == "columns":
            return self._cols
        if key == "displaycolumns":
            return tuple(self._display)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == "displaycolumns":
            self._display = [c for c in value if c in self._cols]
            self._schedule()
        else:
            super().__setitem__(key, value)

    def insert(self, parent, index, iid=None, image="", tags=(), values=(), **kw):
        iid = str(iid if iid is not None else len(self._order))
        self._data[iid] = {"values": dict(zip(self._cols, [str(v) for v in values])), "tags": tuple(tags),
                           "image": image}
        self._order.append(iid)
        self._schedule()
        return iid

    def delete(self, *iids):
        gone = set(iids)
        self._order = [i for i in self._order if i not in gone]
        for i in gone:
            self._data.pop(i, None)
        self._sel = [i for i in self._sel if i not in gone]
        self._schedule()

    def get_children(self, item=""):
        return tuple(self._order)

    def set(self, iid, column=None, value=None):
        d = self._data[str(iid)]["values"]
        if column is None:
            return dict(d)
        if value is None:
            return d.get(column, "")
        d[column] = str(value)
        self._schedule()

    def item(self, iid, option=None, **kw):
        d = self._data[str(iid)]
        if kw:
            d.update({k: v for k, v in kw.items() if k in ("image", "tags")})
            self._schedule()
            return None
        if option == "image":
            img = d["image"]
            return (str(img),) if img else ""
        if option == "tags":
            return d["tags"]
        if option == "values":
            return tuple(d["values"].get(c, "") for c in self._cols)
        return {"image": d["image"], "tags": d["tags"], "values": self.item(iid, "values")}

    def exists(self, iid):
        return str(iid) in self._data

    def selection(self):
        return tuple(self._sel)

    def _norm(self, items):
        if isinstance(items, (list, tuple)):
            return [str(i) for i in items if str(i) in self._data]
        return [str(items)] if str(items) in self._data else []

    def selection_set(self, *items):
        items = items[0] if len(items) == 1 else list(items)
        self._sel = self._norm(items)
        self._anchor = self._sel[0] if self._sel else None
        self._schedule()
        self.event_generate("<<TreeviewSelect>>")

    def selection_add(self, *items):
        items = items[0] if len(items) == 1 else list(items)
        for i in self._norm(items):
            if i not in self._sel:
                self._sel.append(i)
        self._schedule()

    def selection_remove(self, *items):
        items = items[0] if len(items) == 1 else list(items)
        gone = set(self._norm(items))
        self._sel = [i for i in self._sel if i not in gone]
        self._schedule()

    def focus(self, item=None):
        if item is None:
            return self._focus
        self._focus = str(item)
        return None

    def tag_configure(self, tag, option=None, **kw):
        cfg = self._tags.setdefault(tag, {})
        if kw:
            cfg.update(kw)
            self._schedule()
            return None
        return cfg.get(option, "") if option else dict(cfg)

    def heading(self, column, option=None, **kw):
        return None if kw else ""

    def column(self, column, option=None, **kw):
        cfg = self._colcfg.setdefault(column, {"width": 100})
        if kw:
            cfg.update(kw)
            return None
        return cfg.get(option) if option else dict(cfg)

    def bind(self, sequence=None, func=None, add=None):
        return self.canvas.bind(sequence, func, "+")             # after the list's own handling

    def yview(self, *args):
        if args:
            return self._yview_cmd(*args)
        return self.canvas.yview()

    def yview_moveto(self, fraction):
        self.canvas.yview_moveto(fraction)
        self._schedule()

    def see(self, iid):
        iid = str(iid)
        if iid not in self._data:
            return
        self.update_idletasks()
        i = self._order.index(iid)
        total = self.TOP + len(self._order) * self.ROW + self.TOP
        top, bottom = self.canvas.canvasy(0), self.canvas.canvasy(self.canvas.winfo_height())
        y1, y2 = self.TOP + i * self.ROW, self.TOP + (i + 1) * self.ROW
        if y1 < top or y2 > bottom:
            self.canvas.yview_moveto(max(0, y1 - 20) / max(1, total))
        self._draw()

    def bbox(self, iid, column=None):
        iid = str(iid)
        if iid not in self._data:
            return ""
        self._draw()
        z = self._zones.get(iid)
        if not z:
            return ""
        key = "#0" if column == "#0" else "twitch" if column == "twitch" else "row" if column is None else "name"
        x1, y1, x2, y2 = z.get(key) or z["row"]
        top = self.canvas.canvasy(0)
        return int(x1), int(y1 - top), int(x2 - x1), int(y2 - y1)

    def identify_row(self, y):
        i = int((self.canvas.canvasy(y) - self.TOP) // self.ROW)
        return self._order[i] if 0 <= i < len(self._order) and self.canvas.canvasy(y) >= self.TOP else ""

    def identify_column(self, x):
        y = getattr(self, "_last_y", None)
        row = self.identify_row(y) if y is not None else ""
        z = self._zones.get(row) or {}
        for key, col in (("#0", "#0"), ("twitch", "twitch")):
            if key in z and z[key][0] <= x <= z[key][2]:
                if col == "#0":
                    return "#0"
                return f"#{self._display.index('twitch') + 1}" if "twitch" in self._display else "#1"
        return "#1"

    def identify_region(self, x, y):
        return "cell" if self.identify_row(y) else "nothing"

    # ---- drawing ----
    def _schedule(self):
        if self._job is None:
            try:
                self._job = self.after_idle(self._draw)
            except tk.TclError:
                pass

    def _yview_cmd(self, *args):
        self.canvas.yview(*args)
        self._draw()

    def _scrolled(self, first, last):
        self.sb.set(first, last)
        if getattr(self, "_drawn_top", None) != round(float(first), 4):
            self._schedule()

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")
        self._draw()

    def _set_hover(self, iid):
        if iid != self._hover:
            self._hover = iid
            self._schedule()

    def _motion(self, e):
        self._last_y = e.y
        self._set_hover(self.identify_row(e.y) or None)

    def _press(self, e):
        self._last_y = e.y
        self.canvas.focus_set()
        iid = self.identify_row(e.y)
        if not iid:
            return
        ctrl, shift = bool(e.state & 0x0004), bool(e.state & 0x0001)
        if shift and self._anchor in self._order:
            a, b = sorted((self._order.index(self._anchor), self._order.index(iid)))
            self._sel = self._order[a:b + 1]
        elif ctrl:
            self._sel = [i for i in self._sel if i != iid] if iid in self._sel else self._sel + [iid]
            self._anchor = iid
        else:
            self._sel, self._anchor = [iid], iid
        self._focus = iid
        self._draw()
        self.event_generate("<<TreeviewSelect>>")

    def _step(self, d, e):
        if not self._order:
            return
        cur = self._order.index(self._focus) if self._focus in self._order else -1
        nxt = self._order[max(0, min(len(self._order) - 1, cur + d))]
        self._sel, self._anchor, self._focus = [nxt], nxt, nxt
        self.see(nxt)
        self.event_generate("<<TreeviewSelect>>")
        return "break"

    def _draw(self):
        self._job = None
        c, P = self.canvas, self.pal
        try:
            width = max(420, c.winfo_width())
        except tk.TclError:
            return
        total = self.TOP * 2 + len(self._order) * self.ROW
        c.configure(scrollregion=(0, 0, width, max(total, 1)))
        c.delete("all")
        self._zones = {}
        top = c.canvasy(0)
        self._drawn_top = round(c.yview()[0], 4)
        first = max(0, int((top - self.TOP) // self.ROW))
        last = min(len(self._order), first + int(c.winfo_height() // self.ROW) + 3)
        show = set(self._display)
        friend_bg = self._tags.get("friend", {}).get("background") or P["friend"]
        for i in range(first, last):
            iid = self._order[i]
            d = self._data[iid]
            v, tags = d["values"], d["tags"]
            y = self.TOP + i * self.ROW
            x1, y1, x2, y2 = 10, y + 3, width - 10, y + self.ROW - 3
            sel, hov, friend = iid in self._sel, iid == self._hover, "friend" in tags
            base = _mix(P["bg"], friend_bg, 0.55) if friend else _mix(P["bg"], P["panel"], 0.85)
            fill = _mix(base, P["accent"], 0.42) if sel else _mix(base, P["fg"], 0.06) if hov else base
            round_rect(c, x1, y1, x2, y2, 12, fill=fill, outline=P["accent"] if sel else "", width=1.5)
            if friend:
                round_rect(c, x1, y1 + 10, x1 + 4, y2 - 10, 2, fill=P["accent"], outline="")
            cy = (y1 + y2) / 2
            cx, rad = x1 + 34, 21
            img = d["image"]
            if img:
                c.create_image(cx, cy, image=img)
            else:
                draw_avatar(c, cx, cy, rad, v.get("gamertag", "?"), v.get("xuid") or v.get("gamertag", ""), P)
            zones = {"row": (x1, y1, x2, y2), "#0": (cx - rad - 4, y1, cx + rad + 4, y2)}
            # right side: Xbox list position, then the Twitch pill
            right = x2 - 14
            pos = v.get("current", "")
            if "current" in show and pos:
                tw = self.chip_font.measure(f"#{pos}") + 16
                round_rect(c, right - tw, cy - 11, right, cy + 11, 11, fill=_mix(fill, P["fg"], 0.1), outline="")
                c.create_text(right - tw / 2, cy, text=f"#{pos}", fill=P["muted"], font=self.chip_font)
                right -= tw + 10
            tw_text = v.get("twitch", "")
            if "twitch" in show:
                if tw_text:
                    live = tw_text.startswith("●")
                    label = self._fit(tw_text, self.pill_font, 210)
                    pw = self.pill_font.measure(label) + 24
                    round_rect(c, right - pw, cy - 13, right, cy + 13, 13, outline="",
                               fill="#9146ff" if live else _mix(fill, P["fg"], 0.1))
                    c.create_text(right - pw / 2, cy, text=label, fill="#ffffff" if live else P["fg"],
                                  font=self.pill_font)
                    zones["twitch"] = (right - pw, y1, right, y2)
                    right -= pw + 10
                else:
                    zones["twitch"] = (right - 60, y1, right, y2)        # an empty field is still the Twitch place
            # middle: name, chips, a line of details
            tx = cx + rad + 14
            name = v.get("gamertag", "")
            avail = right - tx - 10
            c.create_text(tx, cy - 10, anchor="w", text=self._fit(name, self.name_font, max(60, avail)),
                          fill="#ffffff" if sel else P["fg"], font=self.name_font)
            chip_x = tx + min(self.name_font.measure(name), max(60, avail)) + 10
            chips = (["Friend"] if friend else []) + ([t.strip() for t in v.get("tags", "").split(",") if t.strip()]
                                                     if "tags" in show else [])
            for chip in chips:
                cw = self.chip_font.measure(chip) + 14
                if chip_x + cw > right - 6:
                    break
                round_rect(c, chip_x, cy - 19, chip_x + cw, cy - 1, 9, outline="",
                           fill=P["accent"] if chip == "Friend" else _mix(fill, P["fg"], 0.12))
                c.create_text(chip_x + cw / 2, cy - 10, text=chip, font=self.chip_font,
                              fill=P["bg"] if chip == "Friend" else P["fg"])
                chip_x += cw + 6
            meta = []
            hours = v.get("hours", "")
            if "hours" in show and hours:
                meta.append("hours private" if hours == "private" else f"{hours} in Sea of Thieves")
            if "last_seen" in show and v.get("last_seen"):
                meta.append("seen " + v["last_seen"])
            if "origin" in show and v.get("origin") == "Import":
                meta.append("imported")
            if "xuid" in show and v.get("xuid"):
                meta.append("XUID " + v["xuid"])
            c.create_text(tx, cy + 12, anchor="w", text=self._fit("  ·  ".join(meta), self.meta_font, max(60, avail)),
                          fill=_mix(P["muted"], "#ffffff", 0.35) if sel else P["muted"], font=self.meta_font)
            zones["name"] = (tx, y1, max(tx + 40, right), y2)
            self._zones[iid] = zones

    @staticmethod
    def _fit(text, f, width):
        if f.measure(text) <= width:
            return text
        while text and f.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"
