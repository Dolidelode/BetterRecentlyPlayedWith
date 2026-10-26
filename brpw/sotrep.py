"""SOTREP link generation.

Verified from the site's own script: /search/<slug>, slug = "xbox-" + gamertag lower-cased,
whitespace -> "-", everything outside [a-z0-9-] removed. This is lossy, so the result is a
search link, not a guaranteed profile link.
"""
import re

BASE = "https://www.sotrep.com"


def slug(gamertag: str) -> str:
    s = re.sub(r"\s+", "-", gamertag.strip().lower())
    return "xbox-" + re.sub(r"[^a-z0-9\-]", "", s)


def profile_url(gamertag: str) -> str:
    return f"{BASE}/search/{slug(gamertag)}"
