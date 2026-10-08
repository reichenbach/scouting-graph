"""Where the review app keeps its files.

`config.home()` stays the checkout unless YDS_GRAPH_HOME is set. The CLI and
the make targets never call this module, so they keep writing next to the
code. The app calls `activate_desktop_home` once at startup, which points
YDS_GRAPH_HOME at a per-user folder and seeds the sample files there.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


APP_DIR_NAME = "YourDataScouting"
LINUX_DIR_NAME = "yourdatascouting"


def bundle_root() -> Path:
    """Folder that holds the shipped sample data.

    A PyInstaller build unpacks data files under sys._MEIPASS. A checkout
    keeps them next to this package.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def static_dir() -> Path:
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS")) / "yds_graph" / "desktop" / "static"
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().parent / "static"


def platform_app_data_dir() -> Path:
    """Per-user folder for this operating system. Does not create it.

    YDS_APP_DATA overrides the location. Tests use that. A reviewer does not
    need to set it.
    """
    override = os.environ.get("YDS_APP_DATA", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        root = os.environ.get("APPDATA", "").strip()
        base = Path(root) if root else Path.home() / "AppData" / "Roaming"
        return base / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / LINUX_DIR_NAME


def prepare_desktop_home(home: Path) -> Path:
    """Create the app folders and copy any missing sample files.

    Files that are already there are left alone, so a second launch does not
    replace a CSV the reviewer added.
    """
    root = Path(home).expanduser().resolve()
    for name in ("inbox", "outbox", "errors", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _seed_missing(root)
    return root


def activate_desktop_home() -> Path:
    """Point the process at the app data folder.

    An explicit YDS_GRAPH_HOME wins. That is how the tests and a person who
    set the variable keep a chosen directory. With nothing set, this process
    uses the per-user folder and does not write into the current directory.
    """
    existing = os.environ.get("YDS_GRAPH_HOME", "").strip()
    if existing:
        return prepare_desktop_home(Path(existing))
    root = prepare_desktop_home(platform_app_data_dir())
    os.environ["YDS_GRAPH_HOME"] = str(root)
    return root


def _seed_missing(home: Path) -> None:
    source = bundle_root()
    sample_src = source / "sample_data"
    sample_dest = home / "sample_data"
    if sample_src.is_dir():
        for path in sample_src.rglob("*"):
            if not path.is_file():
                continue
            dest = sample_dest / path.relative_to(sample_src)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    csv_src = source / "inbox" / "sample.csv"
    csv_dest = home / "inbox" / "sample.csv"
    if csv_src.is_file() and not csv_dest.exists():
        csv_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(csv_src, csv_dest)
