"""PyInstaller entry for the review app.

`python -m yds_graph.desktop` calls the same main. This file exists so the
frozen executable has a normal script name.
"""

from __future__ import annotations

from yds_graph.desktop.app import main


if __name__ == "__main__":
    raise SystemExit(main())
