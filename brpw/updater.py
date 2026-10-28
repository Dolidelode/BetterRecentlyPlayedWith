"""Update check against public GitHub Releases, and an in-app update the user starts with a click.

Nothing is downloaded or run without the user asking. "Update now" downloads the program file and SHA256SUMS.txt from
the official repository's release (HTTPS, github.com only), refuses a file whose SHA-256 differs, then swaps the
program file: Windows lets a running .exe be renamed, so the old one becomes <name>.old (removed at the next start)
and the new one takes its place. The checksum guards against a broken or swapped download; it comes from the same
release, so it can not protect against a compromised GitHub account.
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import REPO, __version__

API = f"https://api.github.com/repos/{REPO}/releases/latest"  # excludes drafts and prereleases
EXE_NAME = "BetterRecentlyPlayedWith.exe"
SUMS_NAME = "SHA256SUMS.txt"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
MAX_EXE = 200 * 1024 * 1024


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    notes: str
    url: str
    exe_url: str = ""
    sums_url: str = ""


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
    assets = {}
    for a in payload.get("assets") or []:
        link = str(a.get("browser_download_url", ""))
        if a.get("name") in (EXE_NAME, SUMS_NAME) and link.startswith(DOWNLOAD_PREFIX):
            assets[a["name"]] = link
    return UpdateInfo(tag.lstrip("v"), payload.get("body") or "", url, assets.get(EXE_NAME, ""),
                      assets.get(SUMS_NAME, ""))


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


# ---- in-app update (only when the user clicks "Update now")
def running_exe() -> Optional[Path]:
    """The program file when running as the built .exe; None when running from source."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def _fetch(url: str, limit: int, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "BetterRecentlyPlayedWith"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
    except Exception as e:
        raise UpdateError(f"Download failed: {e}")
    if len(data) > limit:
        raise UpdateError("Download is larger than expected; not used.")
    return data


def expected_sha256(sums: str, name: str = EXE_NAME) -> str:
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9A-Fa-f]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError("The release has no checksum for the program file.")


def download(info: UpdateInfo, target: Path, fetch: Callable[[str, int], bytes] = _fetch) -> Path:
    """Download the new program file next to `target` as <name>.new, after checking its SHA-256."""
    for u in (info.exe_url, info.sums_url):
        if not u.startswith(DOWNLOAD_PREFIX):
            raise UpdateError("This release has no program file for updating in the app.")
    want = expected_sha256(fetch(info.sums_url, 10_000).decode("utf-8", "replace"))
    data = fetch(info.exe_url, MAX_EXE)
    if hashlib.sha256(data).hexdigest() != want:
        raise UpdateError("The downloaded file does not match the release checksum; not used.")
    if not data.startswith(b"MZ"):
        raise UpdateError("The downloaded file is not a Windows program; not used.")
    new = target.with_name(target.name + ".new")
    try:
        new.write_bytes(data)
    except OSError as e:
        raise UpdateError(f"Could not save the update next to the program ({e}). Use the release page instead.")
    return new


def install(new: Path, target: Path) -> None:
    """Put the new program file in place. The running one is renamed to <name>.old (removed at the next start)."""
    old = target.with_name(target.name + ".old")
    try:
        if old.exists():
            old.unlink()
        os.replace(target, old)
    except OSError as e:
        raise UpdateError(f"Could not replace the program file ({e}). Use the release page instead.")
    try:
        os.replace(new, target)
    except OSError as e:
        os.replace(old, target)                    # put the old one back
        raise UpdateError(f"Could not replace the program file ({e}). Use the release page instead.")


def cleanup(target: Optional[Path]) -> None:
    """Remove what an earlier update left behind. Silently retried at the next start if still in use."""
    if target is None:
        return
    for suffix in (".old", ".new"):
        try:
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def restart_env() -> dict:
    """Environment for starting the new program file: a one-file build must not reuse this run's unpacked files."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
