"""Test support: fake HTTP server (stdlib) and the conformance cases location.

The cases live in ``clients/conformance/cases.json`` in the monorepo. The public mirror
(``bernisoftware/bzapper-python``) only receives ``clients/python`` — hence the copy at
``tests/fixtures/cases.json``, written by ``clients/conformance/generate.py`` (never edit
it by hand). A test locks both copies together when both exist (monorepo only).
"""

from __future__ import annotations

import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PACKAGE_ROOT = TESTS_DIR.parent  # clients/python (or the root of the public mirror)
VENDORED_CASES = TESTS_DIR / "fixtures" / "cases.json"
# Monorepo only: <root>/clients/python/tests. Outside it (mirror, built wheel check)
# these paths do not exist and the tests that depend on them are skipped.
MONOREPO_CASES = PACKAGE_ROOT.parent / "conformance" / "cases.json"


def cases_path() -> pathlib.Path:
    return VENDORED_CASES


def load_cases() -> Dict[str, Any]:
    with cases_path().open(encoding="utf-8") as fh:
        return json.load(fh)


class FakeServer:
    """Local server that answers the queued responses in order and records the requests.

    An extra request (empty queue) gets 418 ``UNEXPECTED_REQUEST`` — a status the SDK
    does not retry — and is recorded so the test can flag it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path, _, qs = self.path.partition("?")
                record = {
                    "method": self.command,
                    "raw_path": path,
                    "raw_query": qs,
                    "query": parse_qsl(qs, keep_blank_values=True),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": raw,
                }
                with outer._lock:
                    outer.requests.append(record)
                    resp: Optional[Dict[str, Any]] = (
                        outer._responses.pop(0) if outer._responses else None
                    )
                if resp is None:
                    resp = {
                        "status": 418,
                        "headers": {},
                        "body": {"code": "UNEXPECTED_REQUEST", "message": "UNEXPECTED_REQUEST"},
                    }
                body = resp.get("body")
                if body is None:
                    payload, ctype = b"", None
                elif isinstance(body, str):
                    payload, ctype = body.encode("utf-8"), "text/plain; charset=utf-8"
                else:
                    payload, ctype = json.dumps(body).encode("utf-8"), "application/json"
                self.send_response(int(resp["status"]))
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (resp.get("headers") or {}).items():
                    self.send_header(key, str(value))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, format: str, *args: Any) -> None:  # keep stderr quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self, responses: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._responses = list(responses)
            self.requests = []


def json_body(record: Dict[str, Any]) -> Any:
    raw = record["body"]
    return json.loads(raw.decode("utf-8")) if raw else None
