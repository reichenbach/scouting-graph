"""The review app's service layer, without a browser.

Proves the per-user folder is opt-in, a paused run can be approved into a
PDF plus a DELIVERED audit row, and a reject writes HOLD and no PDF.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yds_graph import config
from yds_graph.desktop import paths, secrets, service
from yds_graph.desktop.errors import ServiceError

REPO = Path(__file__).resolve().parent.parent


class MemoryKeyring:
    def __init__(self):
        self.saved = {}

    def get_password(self, service_name, account):
        return self.saved.get((service_name, account))

    def set_password(self, service_name, account, password):
        self.saved[(service_name, account)] = password

    def delete_password(self, service_name, account):
        self.saved.pop((service_name, account), None)


def _pdfs(home: Path) -> list[Path]:
    outbox = home / "outbox"
    return list(outbox.rglob("*.pdf")) if outbox.exists() else []


def test_unset_home_stays_on_the_checkout(monkeypatch):
    monkeypatch.delenv("YDS_GRAPH_HOME", raising=False)
    assert config.home() == config.REPO_ROOT


def test_import_does_not_redirect_home(monkeypatch):
    monkeypatch.delenv("YDS_GRAPH_HOME", raising=False)
    assert "YDS_GRAPH_HOME" not in os.environ
    assert config.home() == config.REPO_ROOT


def test_linux_app_data_dir(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("YDS_APP_DATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: Path("/home/reviewer"))
    assert paths.platform_app_data_dir() == Path("/home/reviewer/.local/share/yds-review")


def test_xdg_app_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("YDS_APP_DATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.platform_app_data_dir() == tmp_path / "xdg" / "yds-review"


def test_macos_app_data_dir(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.delenv("YDS_APP_DATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: Path("/Users/reviewer"))
    found = paths.platform_app_data_dir()
    assert found == Path("/Users/reviewer/Library/Application Support/YDS Review")


def test_windows_app_data_dir(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.delenv("YDS_APP_DATA", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\reviewer\AppData\Roaming")
    found = paths.platform_app_data_dir()
    assert found == Path(r"C:\Users\reviewer\AppData\Roaming") / "YDS Review"


def test_app_data_override(monkeypatch, tmp_path):
    monkeypatch.setenv("YDS_APP_DATA", str(tmp_path / "custom"))
    assert paths.platform_app_data_dir() == (tmp_path / "custom").resolve()


def test_activate_keeps_explicit_home(home, monkeypatch, tmp_path):
    monkeypatch.setenv("YDS_APP_DATA", str(tmp_path / "should-not-use"))
    root = paths.activate_desktop_home()
    assert root == home.resolve()
    assert os.environ["YDS_GRAPH_HOME"] == str(home)
    assert not (tmp_path / "should-not-use").exists()
    assert config.home() == home.resolve()


def test_activate_uses_app_data_when_home_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("YDS_GRAPH_HOME", raising=False)
    monkeypatch.setenv("YDS_APP_DATA", str(tmp_path / "app"))
    root = paths.activate_desktop_home()
    assert root == (tmp_path / "app").resolve()
    assert os.environ["YDS_GRAPH_HOME"] == str(root)
    assert (root / "inbox" / "sample.csv").is_file()
    assert (root / "sample_data" / "docs" / "methodology.md").is_file()
    assert config.checkpoint_db_path().is_relative_to(root)
    assert config.audit_db_path().is_relative_to(root)
    assert config.outbox_dir().is_relative_to(root)


def test_seed_does_not_overwrite_a_reviewer_file(home):
    target = home / "inbox" / "sample.csv"
    target.write_text("keep\n", encoding="utf-8")
    paths.prepare_desktop_home(home)
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_seed_fills_a_missing_sample_file(home):
    missing = home / "sample_data" / "docs" / "methodology.md"
    missing.unlink()
    paths.prepare_desktop_home(home)
    assert missing.is_file()
    assert missing.read_text(encoding="utf-8") == (
        REPO / "sample_data" / "docs" / "methodology.md"
    ).read_text(encoding="utf-8")


def test_approve_delivers_pdf_and_audit_row(home):
    started = service.start_sample("sample")
    assert started["status"] == "paused"
    thread_id = started["thread_id"]
    review = started["review"]
    assert review["paused_because"]
    assert "OPP-11" in review["draft_notes"]
    assert "Nothing has been delivered" in review["paused_because"]

    pending = service.list_pending()
    assert [item["thread_id"] for item in pending] == [thread_id]
    assert service.get_review(thread_id)["source_file"] == "sample.csv"

    approved = service.approve(thread_id)
    assert approved["status"] == "delivered"
    pdf = Path(approved["outbox_path"])
    assert pdf.is_file()
    assert pdf.is_relative_to(home / "outbox")
    blob = pdf.read_bytes()
    assert b"yds-graph 0.2.0" in blob
    assert b"sample.csv" in blob

    rows = service.audit_rows(limit=5)
    assert rows[0]["outcome"] == "DELIVERED"
    assert rows[0]["thread_id"] == thread_id
    assert rows[0]["kind"] == "report"
    assert rows[0]["coach"] == "sample"
    assert rows[0]["source_file"] == "sample.csv"
    assert rows[0]["pipeline_version"] == config.PIPELINE_VERSION
    assert rows[0]["artifact"] == str(pdf)
    assert service.list_pending() == []


def test_reject_holds_and_writes_the_reason(home):
    started = service.start_sample("sample")
    thread_id = started["thread_id"]
    rejected = service.reject(thread_id, "velocity looks off in game two")
    assert rejected["status"] == "held"
    assert _pdfs(home) == []
    note = (home / "errors" / "sample.csv.hold.md").read_text(encoding="utf-8")
    assert "velocity looks off in game two" in note
    rows = service.audit_rows()
    assert rows[0]["outcome"] == "HOLD"
    assert rows[0]["thread_id"] == thread_id
    assert "velocity looks off in game two" in rows[0]["reason"]
    assert service.list_pending() == []


def test_reject_requires_a_reason(home):
    started = service.start_sample("sample")
    with pytest.raises(ServiceError, match="reason"):
        service.reject(started["thread_id"], "  ")
    assert service.list_pending()[0]["thread_id"] == started["thread_id"]
    assert _pdfs(home) == []


def test_bad_schema_is_held_without_a_review(home):
    result = service.start_report(home / "sample_data" / "bad_schema.csv", "sample")
    assert result["status"] == "held"
    assert "review" not in result
    assert result["coach_notes"]
    assert service.list_pending() == []
    assert _pdfs(home) == []
    rows = service.audit_rows()
    assert rows[0]["outcome"] == "HOLD"


def test_folder_label_rejects_a_path(home):
    with pytest.raises(ServiceError):
        service.start_sample("../out")
    assert service.list_pending() == []


def test_upload_and_empty_file(home):
    raw = (home / "inbox" / "sample.csv").read_bytes()
    started = service.save_upload("week export.csv", base64.b64encode(raw).decode("ascii"), "sample")
    assert started["status"] == "paused"
    assert (home / "inbox" / "week_export.csv").is_file()
    with pytest.raises(ServiceError):
        service.save_upload("notes.txt", base64.b64encode(b"hello").decode("ascii"))
    with pytest.raises(ServiceError):
        service.save_upload("empty.csv", base64.b64encode(b"   \n").decode("ascii"))


def test_open_path_stays_in_the_outbox(home, monkeypatch):
    launched = {}

    def record(path):
        launched["path"] = path
        return True

    monkeypatch.setattr(service, "_launch", record)
    pdf = home / "outbox" / "sample" / "note.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    opened = service.open_outbox_file(str(pdf))
    assert opened["opened"] is True
    assert launched["path"] == pdf.resolve()

    secret = home / "secrets.env"
    secret.write_text("ANTHROPIC_API_KEY=not-for-the-page\n", encoding="utf-8")
    with pytest.raises(ServiceError, match="outbox"):
        service.open_outbox_file(str(secret))
    with pytest.raises(ServiceError, match="outbox"):
        service.open_outbox_file(str(home / "outbox" / ".." / "secrets.env"))
    assert launched["path"] == pdf.resolve()


def test_key_file_is_local_and_not_echoed(home, monkeypatch):
    monkeypatch.delenv("YDS_MODEL_BACKEND", raising=False)
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    key = "sk-ant-api03-desktop-test-key"
    info = secrets.save_settings("anthropic", key)
    stored = (home / "secrets.env").read_text(encoding="utf-8")
    assert key in stored
    assert (home / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert info["effective_mode"] == "anthropic"
    assert info["key_storage"] == "file"
    assert key not in json.dumps(info)
    assert "settings.json" in str(secrets.settings_path())
    assert key not in (home / "settings.json").read_text(encoding="utf-8")
    assert os.environ["ANTHROPIC_API_KEY"] == key
    assert os.environ["YDS_GRAPH_STUB"] == ""

    stub = secrets.save_settings("stub")
    assert stub["effective_mode"] == "stub"
    assert stub["key_saved"] is True
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ["YDS_GRAPH_STUB"] == "1"
    assert secrets.read_api_key() == key


def test_placeholder_key_is_refused(home, monkeypatch):
    monkeypatch.delenv("YDS_MODEL_BACKEND", raising=False)
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    with pytest.raises(ServiceError, match="placeholder"):
        secrets.save_settings("anthropic", "sk-ant-...")
    assert not (home / "secrets.env").exists()
    assert secrets.public_settings()["effective_mode"] == "stub"


def test_keyring_is_preferred_and_can_be_cleared(home, monkeypatch):
    monkeypatch.delenv("YDS_MODEL_BACKEND", raising=False)
    ring = MemoryKeyring()
    monkeypatch.setattr(secrets, "_load_keyring", lambda: ring)
    key = "sk-ant-api03-keychain-test-key"
    (home / "secrets.env").write_text("ANTHROPIC_API_KEY=stale\n", encoding="utf-8")
    info = secrets.save_settings("anthropic", key)
    assert info["key_storage"] == "keychain"
    assert not (home / "secrets.env").exists()
    assert ring.get_password(secrets.SERVICE_NAME, secrets.ACCOUNT_NAME) == key
    assert key not in json.dumps(secrets.public_settings())

    cleared = secrets.save_settings("anthropic", clear_key=True)
    assert cleared["mode"] == "stub"
    assert cleared["key_saved"] is False
    assert secrets.read_api_key() is None
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_lookup_stays_offline(home):
    result = service.lookup_question("How is chase rate defined in a report?")
    assert result["status"] == "done"
    assert result["answer"]
    assert result["passages"]


def test_page_is_local_and_has_the_review_actions():
    static = REPO / "yds_graph" / "desktop" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    style = (static / "style.css").read_text(encoding="utf-8")
    for text in (html, script, style):
        assert "https://" not in text
        assert "http://" not in text
    assert "<title>YDS Review</title>" in html
    assert "<h1>YDS Review</h1>" in html
    assert "HITL" not in html
    assert "Approve" in html
    assert "Reject" in html
    assert "Open the PDF" in html
    assert "Use the sample file" in html
    assert "Audit" in html


def test_human_review_is_not_branded_hitl():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    walk = (REPO / "docs" / "REVIEW_WALKTHROUGH.md").read_text(encoding="utf-8")
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "HITL" not in readme
    assert "HITL" not in walk
    assert "make review-demo" in readme
    assert "review-demo:" in makefile
    assert "hitl-demo is deprecated" in makefile
    assert not (REPO / "docs" / "HITL_WALKTHROUGH.md").exists()


def _http(base: str, method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_approve_path(home):
    from yds_graph.desktop.server import make_server

    httpd = make_server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        status, health = _http(base, "GET", "/api/health")
        assert status == 200 and health["ok"] is True and health["mode"] == "stub"

        status, page = None, None
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
            status = response.status
        assert status == 200 and "Open the PDF" in page

        code, started = _http(base, "POST", "/api/runs", {"use_sample": True, "coach": "sample"})
        assert code == 200, started
        assert started["status"] == "paused"
        thread_id = started["thread_id"]

        code, pending = _http(base, "GET", "/api/pending")
        assert code == 200
        assert any(item["thread_id"] == thread_id for item in pending)

        code, approved = _http(base, "POST", f"/api/runs/{thread_id}/approve", {})
        assert code == 200, approved
        assert approved["status"] == "delivered"
        pdf = Path(approved["outbox_path"])
        assert pdf.is_file()
        assert b"yds-graph 0.2.0" in pdf.read_bytes()

        code, rows = _http(base, "GET", "/api/audit?limit=5")
        assert code == 200
        assert rows[0]["outcome"] == "DELIVERED"
        assert rows[0]["thread_id"] == thread_id

        code, blocked = _http(base, "POST", "/api/open", {"path": str(home / "secrets.env")})
        assert code == 400
        assert "error" in blocked

        code, missing = _http(base, "POST", f"/api/runs/{thread_id}/approve", {})
        assert code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_smoke_command_exits_clean(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "YDS_GRAPH_STUB": "1",
        "PYTHONPATH": str(REPO),
    }
    (tmp_path / "home").mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "yds_graph.desktop", "--smoke"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "SMOKE_OK" in completed.stdout
    assert "ANTHROPIC_API_KEY" not in completed.stdout
