"""Your recently-played list in a Discord channel, through a webhook (Server settings > Integrations > Webhooks >
New webhook > Copy webhook URL). Only that address is used; it is saved in discord.json in the app-data folder,
never in the database or the repository (anyone with the address can post in that channel).

By default one message is kept up to date: the first post remembers its message ID and later posts edit it
(Discord's documented "edit webhook message"), so the channel isn't flooded. Nothing is sent when the list hasn't
changed since the last post."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

from .providers import UA, ProviderError

WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/"
                        r"([A-Za-z0-9_\-]+)/?$")
COLOR = 0x1FC7C7                  # the app's teal
MAX_LINES = 30                    # Discord allows 4096 characters in an embed's text; 30 short lines fit


def check_url(url: str) -> str:
    """The webhook address, cleaned; ProviderError when it isn't a Discord webhook address."""
    url = (url or "").strip()
    m = WEBHOOK_RE.match(url.split("?")[0])
    if not m:
        raise ProviderError("That isn't a Discord webhook address. In Discord: Server settings > Integrations > "
                            "Webhooks > New webhook > Copy webhook URL.")
    return f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"


class DiscordConfig:
    """discord.json: url, auto (post after every refresh), keep_one (edit one message), limit, message_id and
    the fingerprint of the last post."""

    DEFAULTS = {"url": "", "auto": False, "keep_one": True, "limit": 15, "message_id": "", "last": ""}

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.data = dict(self.DEFAULTS, **{k: v for k, v in (d if isinstance(d, dict) else {}).items()
                                           if k in self.DEFAULTS})

    def __getitem__(self, k):
        return self.data[k]

    def update(self, **kw):
        if "url" in kw and kw["url"] != self.data["url"]:
            kw.setdefault("message_id", "")                  # a new channel: start a new message there
            kw.setdefault("last", "")
        self.data.update(kw)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        try:
            self.path.chmod(0o600)                           # only you (where the system supports it)
        except OSError:
            pass


def _esc(text: str) -> str:
    """Discord markdown in a gamertag or title shouldn't format the message."""
    return re.sub(r"([\\*_~`|>\[\]()])", r"\\\1", str(text))


def lines(players: List[dict]) -> List[str]:
    out = []
    for p in players[:MAX_LINES]:
        bits = [f"`#{p['pos']}`" if p.get("pos") else "•", f"**[{_esc(p['gamertag'])}]({p['sotrep']})**"]
        if p.get("friend"):
            bits.append("· friend")
        if p.get("hours") and p["hours"] != "private":
            bits.append(f"· {p['hours']}")
        if p.get("twitch"):
            live = f" 🔴 LIVE {p['viewers']:,}" if p.get("twitch_live") and p.get("viewers") is not None else \
                " 🔴 LIVE" if p.get("twitch_live") else ""
            bits.append(f"· [twitch.tv/{_esc(p['twitch'])}]({p['twitch_url']}){live}")
        out.append(" ".join(bits))
    return out


def payload(account: str, players: List[dict], now: Optional[float] = None) -> dict:
    now = now or time.time()
    body = "\n".join(lines(players)) or "_Nobody in the list yet._"
    return {"username": "Better Recently Played With", "allowed_mentions": {"parse": []},
            "embeds": [{"title": f"⚓ Recently played with — {account or 'your account'}"[:256],
                        "description": body[:4000], "color": COLOR,
                        "footer": {"text": f"{len(players)} player(s) · Better Recently Played With"},
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}]}


def fingerprint(players: List[dict]) -> str:
    """What the post shows, without times that change by themselves: no new post when nothing changed."""
    key = [(p.get("pos"), p["gamertag"], p.get("hours"), p.get("twitch"), bool(p.get("twitch_live")),
            p.get("friend")) for p in players[:MAX_LINES]]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()


def _call(method: str, url: str, body: Optional[dict], timeout: int = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = ProviderError({401: "Discord refused the webhook (deleted or wrong address?).",
                             404: "Discord doesn't know this webhook or message any more.",
                             429: "Discord asks to slow down; the next refresh tries again."}.get(
            e.code, f"Discord answered HTTP {e.code}."))
        err.code = e.code
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Network problem reaching Discord: {e}")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def send(url: str, body: dict, message_id: str = "", call: Optional[Callable] = None) -> str:
    """Post (or, with message_id, edit that message). Returns the message ID. A message that was deleted in
    Discord is simply posted again."""
    url, call = check_url(url), call or _call
    if message_id and str(message_id).isdigit():
        try:
            call("PATCH", f"{url}/messages/{message_id}", body)
            return str(message_id)
        except ProviderError as e:
            if getattr(e, "code", 0) != 404:
                raise
    res = call("POST", f"{url}?wait=true", body)          # wait=true: Discord answers with the message (its ID)
    return str(res.get("id") or "")


def post_list(cfg: DiscordConfig, account: str, players: List[dict], force: bool = False,
              call: Optional[Callable] = None) -> str:
    """Post the list as configured. Returns "sent", "edited" or "unchanged"."""
    if not cfg["url"]:
        raise ProviderError("No Discord webhook set up yet.")
    fp = fingerprint(players) + "|" + account
    if not force and fp == cfg["last"]:
        return "unchanged"
    old = cfg["message_id"] if cfg["keep_one"] else ""
    mid = send(cfg["url"], payload(account, players), old, call=call)
    cfg.update(message_id=mid if cfg["keep_one"] else "", last=fp)
    return "edited" if old and mid == old else "sent"
