"""The recently-played list as plain data, for places outside the window: the terminal version (cli.py) and Discord
(discord.py). The same players, order and Twitch channels the app shows."""
import time
from typing import List, Optional

from . import sotrep
from .db import Database

STATUS_MAX_AGE = 10 * 60          # a Twitch "live" older than this isn't trusted (as in the app)


def hours(minutes, stats_at) -> str:
    if minutes is not None:
        return f"{int(minutes) // 60:,} h" if minutes >= 60 else f"{int(minutes)} min"
    return "private" if stats_at else ""


def seen(ts, now=None) -> str:
    """'now', '5 min ago', '3 h ago', '2 days ago', else the date."""
    if not ts:
        return ""
    s = max(0, int((now or time.time()) - ts))
    return ("now" if s < 90 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400
            else f"{s // 86400} days ago" if s < 86400 * 30 else time.strftime("%Y-%m-%d", time.localtime(ts)))


def recent(db: Database, owner: Optional[str], limit: Optional[int] = 30, current_only: bool = True,
           hide_crew: Optional[bool] = None, now: Optional[float] = None) -> List[dict]:
    """The players of this account's recently-played list, in Xbox's order (then the older history when
    current_only=False), each with hours, when seen, the Twitch channel (your own link or the shared master list)
    and the SOTREP link. Crewmates are left out when the app hides them (hide_crew=None: the app's setting)."""
    now = now or time.time()
    if hide_crew is None:
        hide_crew = db.get_setting("hide_crew", "1") == "1"
    rows = db.list_accounts(owner=owner or "")
    rows = sorted(rows, key=lambda a: (a["pos"] is None, a["pos"] if a["pos"] is not None else 0, -a["last_seen"]))
    out = []
    for a in rows:
        if current_only and a["pos"] is None:
            continue
        tags = [t.strip() for t in (a["tags"] or "").split(",") if t.strip()]
        if hide_crew and db.CREW_TAG in [t.casefold() for t in tags]:
            continue
        live = bool(a["twitch_live"]) and a["twitch_checked"] is not None and now - a["twitch_checked"] <= STATUS_MAX_AGE
        out.append({
            "pos": a["pos"] + 1 if a["pos"] is not None else None, "gamertag": a["gamertag"], "xuid": a["xuid"] or "",
            "seen": a["last_seen"], "seen_text": seen(a["last_seen"], now),
            "hours": hours(a["sot_minutes"], a["stats_at"]), "tags": tags,
            "friend": a["relation"] in ("Friend", "In your list"),
            "twitch": a["twitch_login"] or "", "twitch_name": a["twitch_name"] or a["twitch_login"] or "",
            "twitch_shared": a["twitch_source"] == "shared", "twitch_live": live,
            "viewers": a["twitch_viewers"] if live else None,
            "twitch_url": f"https://www.twitch.tv/{a['twitch_login']}" if a["twitch_login"] else "",
            "sotrep": sotrep.profile_url(a["gamertag"])})
        if limit and len(out) >= limit:
            break
    return out
