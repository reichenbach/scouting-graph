"""Local page for the review app.

The page is served on 127.0.0.1 only. There is no remote host and no extra
web framework. The browser is just the window.
"""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .. import config
from . import secrets, service
from .errors import ServiceError
from .paths import static_dir


_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

_MAX_BODY = 32 * 1024 * 1024


def _log(message: str) -> None:
    try:
        path = config.home() / "desktop.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except Exception:
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        try:
            _log("http " + (fmt % args))
        except Exception:
            return

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = urlparse(self.path).path
        if path in _STATIC:
            self._static(path)
            return
        if path == "/api/health":
            info = secrets.public_settings()
            self._json({"ok": True, "mode": info["effective_mode"], "version": info["version"]})
            return
        if path == "/api/settings":
            self._json(secrets.public_settings())
            return
        if path == "/api/pending":
            self._call(service.list_pending)
            return
        if path == "/api/audit":
            query = urlparse(self.path).query
            limit = 50
            for part in query.split("&"):
                if part.startswith("limit="):
                    limit = part.split("=", 1)[1]
            self._call(lambda: service.audit_rows(limit))
            return
        if path.startswith("/api/runs/"):
            thread_id = unquote(path[len("/api/runs/") :]).strip("/")
            if "/" in thread_id or not thread_id:
                self._json({"error": "That run is not waiting for review."}, 404)
                return
            self._call(lambda: service.get_review(thread_id))
            return
        self._json({"error": "That page is not part of this app."}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ServiceError as exc:
            self._json({"error": str(exc)}, exc.status)
            return
        if path == "/api/settings":
            self._call(
                lambda: secrets.save_settings(
                    mode=str(body.get("mode") or "stub"),
                    api_key=body.get("api_key"),
                    clear_key=bool(body.get("clear_key")),
                )
            )
            return
        if path == "/api/runs":
            self._call(lambda: self._start_from_body(body))
            return
        if path == "/api/ask":
            self._call(lambda: service.ask(str(body.get("question") or "")))
            return
        if path == "/api/lookup":
            self._call(lambda: service.lookup_question(str(body.get("question") or "")))
            return
        if path == "/api/open":
            self._call(lambda: service.open_outbox_file(str(body.get("path") or "")))
            return
        if path.startswith("/api/runs/") and path.endswith("/approve"):
            thread_id = unquote(path[len("/api/runs/") : -len("/approve")]).strip("/")
            self._call(lambda: service.approve(thread_id))
            return
        if path.startswith("/api/runs/") and path.endswith("/reject"):
            thread_id = unquote(path[len("/api/runs/") : -len("/reject")]).strip("/")
            self._call(lambda: service.reject(thread_id, str(body.get("reason") or "")))
            return
        self._json({"error": "That action is not part of this app."}, 404)

    def _start_from_body(self, body: dict) -> dict:
        coach = str(body.get("coach") or "sample")
        if body.get("use_sample"):
            return service.start_sample(coach)
        return service.save_upload(
            str(body.get("filename") or ""),
            str(body.get("content_base64") or ""),
            coach,
        )

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > _MAX_BODY:
            raise ServiceError("That upload is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("The request was not readable.") from exc
        if not isinstance(data, dict):
            raise ServiceError("The request was not readable.")
        return data

    def _call(self, func) -> None:
        try:
            result = func()
        except ServiceError as exc:
            self._json({"error": str(exc)}, exc.status)
            return
        except Exception:
            _log(traceback.format_exc())
            self._json(
                {"error": "Something went wrong on this computer. Nothing was delivered."},
                500,
            )
            return
        self._json(result)

    def _static(self, path: str) -> None:
        name, content_type = _STATIC[path]
        file_path = static_dir() / name
        if not file_path.is_file():
            self._json({"error": "The app page is missing from this build."}, 500)
            return
        body = file_path.read_bytes()
        self._bytes(body, 200, content_type)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._bytes(body, status, "application/json; charset=utf-8")

    def _bytes(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(open_browser: bool = True) -> int:
    import webbrowser

    httpd = make_server()
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/"
    try:
        (config.home() / "desktop.url").write_text(url + "\n", encoding="utf-8")
    except OSError:
        pass
    _log(f"listening {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            _log("browser did not open")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0
