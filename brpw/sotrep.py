"""SOTREP link generation.

Verified from the site's own script: /search/<slug>, slug = "xbox-" + gamertag lower-cased,
whitespace -> "-", everything outside [a-z0-9-] removed. This is lossy, so the result is a
search link, not a guaranteed profile link. Twitch searches use "twitch-" + login lower-cased
with "_" -> "-" (the site turns "-" back into "_").
"""
import re

BASE = "https://www.sotrep.com"


def slug(gamertag: str) -> str:
    s = re.sub(r"\s+", "-", gamertag.strip().lower())
    return "xbox-" + re.sub(r"[^a-z0-9\-]", "", s)


def profile_url(gamertag: str) -> str:
    return f"{BASE}/search/{slug(gamertag)}"


def twitch_slug(login: str) -> str:
    return "twitch-" + login.strip().lower().replace("_", "-")


def twitch_search_url(login: str) -> str:
    """SOTREP search for a Twitch channel: shows the Xbox player(s) SOTREP knows for that channel."""
    return f"{BASE}/search/{twitch_slug(login)}"
