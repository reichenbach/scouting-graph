"""Launch the review app.

    python -m yds_graph.desktop
    python -m yds_graph.desktop --smoke

The smoke flag runs the approve path once in a temporary folder and exits.
It does not open a browser. Packaged builds use the same flag.

This file is not named __main__.py. PyInstaller drops a module with that
name, and the frozen executable would not start.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path


def smoke() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="yds-review-smoke-"))
    os.environ["YDS_GRAPH_HOME"] = str(tmp)
    os.environ["YDS_GRAPH_STUB"] = "1"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("YDS_MODEL_BACKEND", None)
    try:
        return _smoke_in(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _smoke_in(tmp: Path) -> int:
    from .paths import prepare_desktop_home
    from . import service
    from .server import make_server

    prepare_desktop_home(tmp)
    started = service.start_sample("sample")
    if started.get("status") != "paused":
        print("SMOKE_FAIL start", started.get("status"), file=sys.stderr)
        return 1
    thread_id = started["thread_id"]
    pending = service.list_pending()
    if not any(item["thread_id"] == thread_id for item in pending):
        print("SMOKE_FAIL pending", file=sys.stderr)
        return 1
    approved = service.approve(thread_id)
    pdf = Path(approved.get("outbox_path") or "")
    if approved.get("status") != "delivered" or not pdf.is_file():
        print("SMOKE_FAIL approve", approved.get("status"), file=sys.stderr)
        return 1
    if b"yds-graph" not in pdf.read_bytes():
        print("SMOKE_FAIL pdf", file=sys.stderr)
        return 1
    rows = service.audit_rows(limit=5)
    if not rows or rows[0]["outcome"] != "DELIVERED" or rows[0]["thread_id"] != thread_id:
        print("SMOKE_FAIL audit", file=sys.stderr)
        return 1

    httpd = make_server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    if not body.get("ok") or body.get("mode") != "stub":
        print("SMOKE_FAIL health", file=sys.stderr)
        return 1
    print("SMOKE_OK")
    print(f"pdf={pdf.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--smoke" in args:
        return smoke()
    from .paths import activate_desktop_home
    from .secrets import apply_model_mode
    from .server import serve

    activate_desktop_home()
    apply_model_mode()
    print("YourDataScouting Review is starting. A browser window will open.", flush=True)
    return serve(open_browser=True)
