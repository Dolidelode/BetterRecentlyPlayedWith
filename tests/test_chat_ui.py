import tkinter as tk

import pytest

from brpw import chat_ui
from brpw.themes import palette


@pytest.fixture(scope="module")          # one window for these tests: Tk on Windows sometimes fails to start a second
def root():                               # one right after the first is destroyed ("Can't find a usable tk.tcl")
    from conftest import new_root
    r = new_root()
    r.withdraw()
    yield r
    r.destroy()


def test_only_xbox_pictures_are_fetched_and_as_small_png():
    raw = "https://images-eds-ssl.xboxlive.com/image?url=abc&format=jpeg"
    u = chat_ui.picture_url(raw, 40)
    assert u.startswith("https://images-eds-ssl.xboxlive.com/image?") and "format=png" in u and "w=40" in u
    assert "format=jpeg" not in u and "url=abc" in u
    for bad in ("http://images-eds-ssl.xboxlive.com/image?url=a", "https://evil.example/image?url=a", "", None):
        assert chat_ui.picture_url(bad) == ""


def test_pictures_load_in_the_background_round_and_are_kept(root, tmp_path):
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 20 for _ in range(20))          # 20x20 red, plain RGB
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 20, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    posted, fetched, ready = [], [], []

    def fetch(url):
        fetched.append(url)
        return data
    pics = chat_ui.Pictures(tmp_path / "pictures", posted.append, fetch=fetch)
    raw = "https://images-eds-ssl.xboxlive.com/image?url=abc&format=jpeg"
    assert pics.get(raw, lambda: ready.append(1)) is None                  # not there yet: downloads in the background
    pics.pool.shutdown(wait=True)
    for fn in posted:                                                     # what the app's queue does on the Tk thread
        fn()
    img = pics.get(raw)
    assert img is not None and ready == [1] and len(fetched) == 1
    assert img.transparency_get(0, 0) and not img.transparency_get(10, 10)     # round: corners see-through
    assert list((tmp_path / "pictures").glob("*.png"))                     # kept on disk for next time
    again = chat_ui.Pictures(tmp_path / "pictures", posted.append, fetch=lambda u: pytest.fail("no new download"))
    assert again.get(raw) is not None
    assert pics.get("https://evil.example/x.png") is None
    posted.clear()
    sized = chat_ui.Pictures(tmp_path / "pictures2", posted.append, fetch=fetch)
    assert sized.get(raw, size=32) is None                               # not a server size: fetched at 64, halved
    sized.pool.shutdown(wait=True)
    for fn in posted:
        fn()
    small = sized.get(raw, size=32)
    assert "w=64" in fetched[-1] and small.width() == 10                  # the 20-pixel sample, halved


def test_confirm_card_asks_inside_the_window(root):
    pal = palette(1)
    frame = tk.Frame(root, bg=pal["bg"], width=600, height=400)
    frame.pack()
    said = []
    card = chat_ui.confirm_card(frame, pal, "Delete it?", "Delete", lambda: said.append("yes"))
    assert card.winfo_toplevel() == root and card.text == "Delete it?"          # in the window, not a new one
    card.no()
    assert said == [] and not card.winfo_exists()
    card = chat_ui.confirm_card(frame, pal, "Delete it?", "Delete", lambda: said.append("yes"))
    second = chat_ui.confirm_card(frame, pal, "Really?", "Delete", lambda: said.append("second"))
    assert not card.winfo_exists()                                          # one card at a time
    second.yes()
    assert said == ["second"] and not second.winfo_exists()


def test_player_cards_select_like_a_list_and_find_the_twitch_pill(root):
    pal = palette(1)
    cards = chat_ui.PlayerCards(root, pal, ("gamertag", "hours", "xuid", "twitch", "current"))
    root.deiconify()
    root.geometry("900x500")
    cards.pack(fill="both", expand=True)
    for i, name in enumerate(["Salty", "Kraken", "Brine", "Lotus"]):
        cards.insert("", "end", iid=str(i), values=(name, "12 h", str(100 + i), "● 5  salty_tv" if i == 0 else "",
                                                   str(i + 1)), tags=("friend",) if i == 1 else ())
    root.update()
    row_y = lambda i: cards.bbox(str(i))[1] + 20
    click = lambda i, state=0: cards._press(type("E", (), {"x": 300, "y": row_y(i), "state": state})())
    click(0)
    assert cards.selection() == ("0",)
    click(2, state=0x0001)                                                  # Shift: a range
    assert cards.selection() == ("0", "1", "2")
    click(1, state=0x0004)                                                  # Ctrl: toggle one
    assert cards.selection() == ("0", "2")
    assert cards.set("0", "gamertag") == "Salty" and cards.item("1", "tags") == ("friend",)
    x, y, w, h = cards.bbox("0", "twitch")
    cards._last_y = y + 5
    assert cards.identify_column(x + w // 2) == "#4" and cards.identify_column(300) == "#1"   # the Twitch pill
    cards["displaycolumns"] = ("gamertag",)                                # hidden details aren't drawn
    root.update()
    assert cards["displaycolumns"] == ("gamertag",)
    cards.delete("0", "1")
    assert cards.get_children() == ("2", "3") and cards.selection() == ("2",)
    cards.destroy()
    root.withdraw()


def test_twitch_pictures_any_format_resized(root, tmp_path):
    import io
    from PIL import Image
    raw = "https://static-cdn.jtvnw.net/jtv_user_pictures/abc-profile_image-300x300.jpeg"
    assert chat_ui.twitch_picture_url(raw) == \
        "https://static-cdn.jtvnw.net/jtv_user_pictures/abc-profile_image-70x70.jpeg"
    for bad in ("http://static-cdn.jtvnw.net/jtv_user_pictures/a.png", "https://static-cdn.jtvnw.net/other/a.png",
                "https://evil.example/jtv_user_pictures/a.png"):
        assert chat_ui.twitch_picture_url(bad) == ""
    buf = io.BytesIO()
    Image.new("RGB", (70, 70), (200, 0, 0)).save(buf, "JPEG")                # Tk can't read JPEG by itself
    posted, fetched = [], []
    pics = chat_ui.Pictures(tmp_path, posted.append, fetch=lambda u: fetched.append(u) or buf.getvalue())
    assert pics.get(raw, size=40) is None
    pics.pool.shutdown(wait=True)
    for fn in posted:
        fn()
    img = pics.get(raw, size=40)
    assert img is not None and img.width() == 40 and fetched == [chat_ui.twitch_picture_url(raw)]
    assert img.transparency_get(0, 0) and not img.transparency_get(20, 20)    # round like the Xbox pictures


def test_cards_unfold_on_click_and_spots_run_their_action(root):
    pal = palette(1)
    hits = []

    def draw(c, iid, values, tags, x1, y1, x2, y2, sel, hov):
        if lst.expanded == iid:
            lst.spot(iid, (x1, y1, x1 + 50, y1 + 50), lambda: hits.append(iid))
    lst = chat_ui.CardList(root, pal, draw, row=60, big=200)
    root.deiconify()
    root.geometry("700x600")
    lst.pack(fill="both", expand=True)
    a, b = lst.insert("", "end", values=("a",)), lst.insert("", "end", values=("b",))
    root.update()
    lst.redraw()
    click = lambda x, y: lst._click(type("E", (), {"x": x, "y": y, "state": 0})())
    click(200, lst.tops[a] + 30)
    assert lst.expanded == a and lst.selection() == (a,) and lst.height(a) == 200
    assert lst.identify_row(lst.tops[b] + 30) == b and lst.tops[b] == lst.tops[a] + 200 + lst.gap
    lst.redraw()
    click(20, lst.tops[a] + 20)                                              # a button inside the open card
    assert hits == [a] and lst.expanded == a
    click(200, lst.tops[a] + 100)                                            # click it again: folds
    assert lst.expanded is None
    lst.destroy()
    root.withdraw()


def test_stream_previews_only_from_twitch(root):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (0, 90, 200)).save(buf, "JPEG")
    posted = []
    pv = chat_ui.Previews(posted.append, fetch=lambda u: buf.getvalue())
    url = "https://static-cdn.jtvnw.net/previews-ttv/live_user_salty-320x180.jpg"
    assert pv.get("https://evil.example/previews-ttv/x.jpg") is None and not posted
    assert pv.get(url, size=(192, 108)) is None
    pv.pool.shutdown(wait=True)
    for fn in posted:
        fn()
    img = pv.get(url, size=(192, 108))
    assert img is not None and (img.width(), img.height()) == (192, 108)
