import os
from pathlib import Path

from . import APP_NAME


def data_dir() -> Path:
    """Per-user data folder, outside the install folder. Override with BRPW_DATA_DIR."""
    override = os.environ.get("BRPW_DATA_DIR")
    if override:
        base = Path(override)
    elif os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / APP_NAME
    elif os.environ.get("XDG_DATA_HOME"):
        base = Path(os.environ["XDG_DATA_HOME"]) / APP_NAME
    else:
        base = Path.home() / ".local" / "share" / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base
