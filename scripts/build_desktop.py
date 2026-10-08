"""Build an unsigned YourDataScouting review app for this operating system.

PyInstaller packs the local page, the sample data, and the package into
dist/. The archive lands in dist-pack/. Run the Linux binary with --smoke
before treating the archive as good.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NAME = "YourDataScouting"


def _add_data(src: Path, dest: str) -> str:
    return f"{src}{os.pathsep}{dest}"


def build() -> None:
    import PyInstaller.__main__

    entry = ROOT / "scripts" / "desktop_entry.py"
    args = [
        str(entry),
        "--paths",
        str(ROOT),
        "--name",
        NAME,
        "--noconfirm",
        "--clean",
        "--onedir",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--collect-submodules",
        "yds_graph",
        "--collect-submodules",
        "langgraph",
        "--collect-all",
        "reportlab",
        "--collect-submodules",
        "keyring",
        "--hidden-import",
        "sqlite3",
        "--hidden-import",
        "ormsgpack",
        "--add-data",
        _add_data(ROOT / "sample_data", "sample_data"),
        "--add-data",
        _add_data(ROOT / "inbox" / "sample.csv", "inbox"),
        "--add-data",
        _add_data(ROOT / "yds_graph" / "desktop" / "static", "yds_graph/desktop/static"),
    ]
    # A console on Linux keeps `YourDataScouting --smoke` printable in CI.
    # macOS and Windows hide the console so the reviewer sees the browser.
    if sys.platform in ("darwin", "win32"):
        args.append("--windowed")
    PyInstaller.__main__.run(args)


def output_dir() -> Path:
    dist = ROOT / "dist"
    if sys.platform == "darwin":
        return dist / f"{NAME}.app"
    return dist / NAME


def archive_name() -> str:
    if sys.platform == "darwin":
        return f"{NAME}-macos.zip"
    if sys.platform == "win32":
        return f"{NAME}-windows.zip"
    return f"{NAME}-linux.zip"


def pack() -> Path:
    folder = output_dir()
    if not folder.exists():
        raise SystemExit(f"missing build output: {folder}")
    out_dir = ROOT / "dist-pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = archive_name()
    dest = out_dir / name
    if dest.exists():
        dest.unlink()
    base = out_dir / name.removesuffix(".zip")
    written = Path(shutil.make_archive(str(base), "zip", root_dir=folder.parent, base_dir=folder.name))
    size = written.stat().st_size
    print(f"packed {written} ({size} bytes)")
    return written


def main() -> int:
    build()
    pack()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
