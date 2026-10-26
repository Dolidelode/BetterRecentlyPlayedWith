"""Update check against public GitHub Releases. Notifies and links to the release page only;
nothing is downloaded or executed automatically (first release: version check + link)."""
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from . import REPO, __version__

API = f"https://api.github.com/repos/{REPO}/releases/latest"  # excludes drafts and prereleases


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    notes: str
    url: str


def parse_version(v: str) -> Tuple[int, ...]:
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", str(v).strip())
    if not m:
        raise UpdateError(f"Invalid version: {v!r}")
    parts = tuple(int(x) for x in m.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def parse_release(payload: dict, current: str = __version__) -> Optional[UpdateInfo]:
    if not isinstance(payload, dict) or "tag_name" not in payload:
        raise UpdateError("Unexpected response from GitHub.")
    if payload.get("prerelease") or payload.get("draft"):
        return None
    tag = payload["tag_name"]
    if not is_newer(tag, current):
        return None
    url = str(payload.get("html_url", ""))
    if not url.startswith(f"https://github.com/{REPO}/"):
        raise UpdateError("Release link does not point at the official repository.")
    return UpdateInfo(tag.lstrip("v"), payload.get("body") or "", url)


def check(timeout: int = 10) -> Optional[UpdateInfo]:
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "BetterRecentlyPlayedWith"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_release(json.loads(r.read().decode("utf-8")))
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"Could not check for updates: {e}")
