"""Auto-matcher: links a player to a Twitch channel only after repeated proof that they are the same person.

A similar name is never enough (many gamertags have a same-named Twitch channel owned by someone else). A name pair
only becomes a candidate; the proof is timing:

* hit:  the channel is live in Sea of Thieves AND Xbox says the player is online in Sea of Thieves.
* miss: the channel is live in Sea of Thieves AND Xbox says the player is offline or playing something else.

Each Twitch stream (its start time) counts once: seen in the game at any moment during that stream makes it a hit
(streamers load in late and leave early), and "not in the game" only counts after the stream has run MISS_AFTER.
A pair is linked automatically after HITS_NEEDED different streams with hits and not a single miss; one miss rules
the pair out for good, and so does the user removing an auto link.
Xbox presence comes from the regular live checks (20 most recent players) plus one targeted check per round for
any player in the history whose look-alike channel is live right now; Twitch from Get Streams.
"""
import re
import time
from typing import Callable, List

HITS_NEEDED = 3                # different streams online together before linking
PRESENCE_MAX_AGE = 6 * 60      # an Xbox presence older than this is not used as evidence
MISS_AFTER = 10 * 60           # "not in the game" only counts once the stream has been live this long
SOT = "sea of thieves"
USER_REJECTED = -1             # session value of a "the user removed this auto link" record

_AFFIXES = ("ttv", "tv", "live", "yt", "twitch", "official", "plays", "gaming", "its", "the", "real", "xx", "x")


def core(name: str) -> str:
    """Lower-case letters and digits, without streamer decorations like ttv_, _live, xX..Xx, its..."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    changed = True
    while changed:
        changed = False
        for a in _AFFIXES:
            if n.startswith(a) and len(n) - len(a) >= 4:
                n, changed = n[len(a):], True
            if n.endswith(a) and len(n) - len(a) >= 4:
                n, changed = n[:-len(a)], True
    return n


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similar(gamertag: str, channel: str) -> bool:
    """Could this channel name belong to this gamertag? Only decides what is worth watching, never links."""
    g, t = core(gamertag), core(channel)
    if len(g) < 4 or len(t) < 4:
        return False
    if g == t:
        return True
    if min(len(g), len(t)) >= 5 and (g in t or t in g) and abs(len(g) - len(t)) <= 4:
        return True
    return min(len(g), len(t)) >= 6 and _distance(g, t) <= 1


def in_sot(state, title) -> bool:
    return (state or "").casefold() == "online" and SOT in (title or "").casefold()


TARGETED_PER_ROUND = 10        # players asked about per round = 1 OpenXBL request
KEEP_FOR_REGULAR = 6           # never use the last few hourly OpenXBL requests for targeted checks


class AutoMatcher:
    def __init__(self, db, client, now: Callable[[], float] = time.time, live=None):
        self.db, self.client, self.now, self.live = db, client, now, live

    def targets(self, streams) -> List[int]:
        """Players anywhere in the history whose similar-name channel is live now, whose Xbox status is not fresh,
        and whose pair is still undecided. Asking Xbox about them right now turns that stream into evidence."""
        t = int(self.now())
        decided = {(m["account_id"], m["twitch_id"]) for m in self.db.match_summary() if m["misses"]}
        with self.db.lock:
            rows = self.db.conn.execute(
                """SELECT a.id, a.gamertag, s.checked_at FROM accounts a LEFT JOIN live_status s ON s.account_id=a.id
                   WHERE a.xuid IS NOT NULL
                     AND NOT EXISTS(SELECT 1 FROM twitch_links l WHERE l.account_id=a.id)
                     AND NOT EXISTS(SELECT 1 FROM twitch_never n WHERE n.account_id=a.id)""").fetchall()
        taken = {l["twitch_id"] for l in self.db.twitch_links()}
        out = []
        for r in rows:
            if r["checked_at"] is not None and t - r["checked_at"] < PRESENCE_MAX_AGE:
                continue                                          # fresh already: run() will use it
            if any(s["id"] not in taken and (r["id"], s["id"]) not in decided
                   and (similar(r["gamertag"], s["login"]) or similar(r["gamertag"], s["name"])) for s in streams):
                out.append(r["id"])
        return out[:TARGETED_PER_ROUND]

    def refresh_targets(self, streams) -> int:
        """One targeted Xbox request for the players above, if the hourly OpenXBL budget allows it."""
        if self.live is None or self.live.budget_left() <= KEEP_FOR_REGULAR:
            return 0
        ids = self.targets(streams)
        if ids:
            self.live.check_accounts(ids, limit=TARGETED_PER_ROUND)
        return len(ids)

    def players(self) -> List[dict]:
        """Players with a fresh Xbox presence and no Twitch link and no 'never link' mark."""
        t = int(self.now())
        with self.db.lock:
            return [dict(r) for r in self.db.conn.execute(
                """SELECT a.id, a.gamertag, s.state, s.title FROM live_status s JOIN accounts a ON a.id=s.account_id
                   WHERE s.checked_at >= ?
                     AND NOT EXISTS(SELECT 1 FROM twitch_links l WHERE l.account_id=a.id)
                     AND NOT EXISTS(SELECT 1 FROM twitch_never n WHERE n.account_id=a.id)""",
                (t - PRESENCE_MAX_AGE,))]

    def run(self, streams=None) -> List[dict]:
        """One round. Returns the links made in this round ({account_id, gamertag, login, name, hits})."""
        if streams is None:
            streams = self.client.game_streams(max_pages=3)
        try:
            self.refresh_targets(streams)
        except Exception:                                         # Xbox trouble must not stop the Twitch side
            pass
        players = self.players()
        if not players:
            return []
        now = int(self.now())
        for p in players:
            for s in streams:
                if not (similar(p["gamertag"], s["login"]) or similar(p["gamertag"], s["name"])):
                    continue
                if (p["state"] or "").casefold() == "away":       # idle console: says nothing either way
                    continue
                started = int(s.get("started") or 0)
                hit = in_sot(p["state"], p["title"])
                if not hit and (not started or now - started < MISS_AFTER):
                    continue
                self.db.add_match_evidence(p["id"], s["id"], s["login"], s["name"], started, 1 if hit else 0, now)
        made = []
        for m in self.db.match_summary():
            if m["misses"] == 0 and m["hits"] >= HITS_NEEDED and not m["linked"] and not m["never"] \
                    and not m["channel_taken"]:
                try:
                    self.db.link_twitch(m["account_id"], m["twitch_id"], m["login"], m["display_name"], source="auto")
                except ValueError:
                    continue
                made.append({"account_id": m["account_id"], "gamertag": m["gamertag"], "login": m["login"],
                             "name": m["display_name"], "hits": m["hits"]})
        return made
