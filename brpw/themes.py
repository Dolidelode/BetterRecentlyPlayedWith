"""Nine themes: each pairs one icon design with an accent colour. The index matches assets/icons/theme<N>.png."""
import sys
from pathlib import Path
from typing import Optional

# name, accent, background, panel (fields/rows), text. Picked by eye from each icon.
THEMES = [
    ("Moonlit loop", "#1fd1b0", "#0f1a24", "#17283a", "#e4f4f2"),
    ("Compass wave", "#2aa6a0", "#0f1b1c", "#182c2d", "#efe6d0"),
    ("Sunset map", "#e0a040", "#2a1d12", "#3a2a1a", "#f3e6c8"),
    ("Clean mono", "#2ec4c4", "#0e1217", "#1b222b", "#ffffff"),
    ("Night ship", "#2f9bd6", "#0b1a2a", "#132b44", "#e3eefa"),
    ("Ghost teal", "#3fd0a0", "#0f1a17", "#18302a", "#e2f5ee"),
    ("Gold compass", "#c99a3c", "#1c1510", "#2b2118", "#f0e2c0"),
    ("Deep teal", "#22c7d6", "#0b1626", "#14243a", "#e6eef5"),
    ("Twilight", "#7b6cf0", "#150f2a", "#231a44", "#ece8ff"),
]
DEFAULT_THEME = 8


def asset_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)  # set by PyInstaller one-file builds
    return Path(base) / "assets" if base else Path(__file__).resolve().parent.parent / "assets"


def icon_path(n: int) -> Path:
    return asset_dir() / "icons" / f"theme{n}.png"


def clamp(value: Optional[str]) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_THEME
    return n if 1 <= n <= len(THEMES) else DEFAULT_THEME


def _mix(a: str, b: str, t: float) -> str:
    """Blend colour a towards b by t (0..1)."""
    ca, cb = [int(x[i:i + 2], 16) for x in (a, b) for i in (1, 3, 5)][:3], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def palette(n: int) -> dict:
    name, accent, bg, panel, fg = THEMES[clamp(n) - 1]
    return {"name": name, "accent": accent, "bg": bg, "panel": panel, "fg": fg,
            "head": _mix(panel, accent, 0.22), "stripe": _mix(panel, bg, 0.35), "muted": _mix(fg, bg, 0.45)}
