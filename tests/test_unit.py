"""Unit tests of the transport (BRIEF §3–5) that the conformance cases do not cover alone:
network failure, retry policy, user idempotency key, argument errors, encoding, options.
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from bzapper import (
    AuthenticationError,
    BzapperError,
    Client,
    NetworkError,
    PartnerClient,
    RateLimitError,
    ServerError,
    ValidationError,
)
from bzapper import _http

from _support import FakeServer, json_body

KEY = "bz_live_unit"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ok(body: Any = None, status: int = 200, headers: Dict[str, str] = None) -> Dict[str, Any]:
    return {"status": status, "headers": headers or {}, "body": body}


def fail(status: int, code: str, headers: Dict[str, str] = None) -> Dict[str, Any]:
    return {"status": status, "headers": headers or {},
            "body": {"code": code, "message": f"msg {code}", "locale": "pt-BR"}}


class ServerTestCase(unittest.TestCase):
    server: FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def client(self, **kw: Any) -> Client:
        c = Client(KEY, base_url=self.server.base_url, **kw)
        self.sleeps: List[float] = []
        c._sleep = self.sleeps.append
        return c


class TestNetworkError(unittest.TestCase):
    def test_server_down(self) -> None:
        c = Client(KEY, base_url=f"http://127.0.0.1:{_free_port()}")
        sleeps: List[float] = []
        c._sleep = sleeps.append
        sent: List[Dict[str, str]] = []
        original = c._send

        def spy(method: str, url: str, headers: Any, data: Any) -> Any:
            sent.append(dict(headers))
            return original(method, url, headers, data)

        c._send = spy  # type: ignore[method-assign]
        with self.assertRaises(NetworkError) as ctx:
            c.get_instance("inst_1")
        err = ctx.exception
        self.assertIsInstance(err, BzapperError)
        self.assertEqual(err.status_code, 0)
        self.assertEqual(err.status, 0)
        self.assertEqual(err.code, "NETWORK_ERROR")
        # 1 attempt + 2 retries, all with the SAME X-Request-Id — the one in the error.
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual({h["X-Request-Id"] for h in sent}, {err.request_id})

    def test_max_retries_zero(self) -> None:
        c = Client(KEY, base_url=f"http://127.0.0.1:{_free_port()}", max_retries=0)
        c._sleep = lambda s: self.fail("must not wait")
        with self.assertRaises(NetworkError):
            c.list_instances()


class TestIdempotency(ServerTestCase):
    def test_user_key_used_verbatim(self) -> None:
        self.server.reset([ok({"id": "c1"}, 201)])
        self.client().create_contact("+5511999990000", idempotency_key="pedido-4471 / ç")
        self.assertEqual(self.server.requests[0]["headers"]["idempotency-key"], "pedido-4471 / ç")

    def test_user_key_verbatim_on_send_and_repeated_on_retry(self) -> None:
        self.server.reset([fail(503, "unavailable"), ok({"message_id": "m1"}, 202)])
        c = self.client()
        c.send_text("+5511999990000", "oi", idempotency_key="pedido-1")
        keys = [r["headers"]["idempotency-key"] for r in self.server.requests]
        self.assertEqual(keys, ["pedido-1", "pedido-1"])

    def test_auto_key_on_writes_only(self) -> None:
        self.server.reset([ok(None, 204), ok({"data": []})])
        c = self.client()
        c.delete_webhook("wh_1")
        c.list_webhooks()
        self.assertRegex(self.server.requests[0]["headers"]["idempotency-key"], r"^[0-9a-f-]{36}$")
        self.assertNotIn("idempotency-key", self.server.requests[1]["headers"])

    def test_partner_client_user_key(self) -> None:
        self.server.reset([ok({"api_key": "bz_live_x"})])
        p = PartnerClient("bz_partner_x", base_url=self.server.base_url)
        p.exchange_code("cc_1", idempotency_key="troca-1")
        self.assertEqual(self.server.requests[0]["headers"]["idempotency-key"], "troca-1")


class TestRetries(ServerTestCase):
    def test_500_is_not_retried(self) -> None:
        self.server.reset([fail(500, "internal_error")])
        with self.assertRaises(ServerError):
            self.client().list_pools()
        self.assertEqual(len(self.server.requests), 1)

    def test_4xx_is_not_retried(self) -> None:
        self.server.reset([fail(422, "invalid_body")])
        with self.assertRaises(ValidationError) as ctx:
            self.client().create_tag("vip")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(ctx.exception.body["code"], "invalid_body")
        self.assertEqual(ctx.exception.locale, "pt-BR")

    def test_retry_after_is_honored_and_capped(self) -> None:
        self.server.reset([
            fail(429, "rate_limited", {"Retry-After": "3"}),
            fail(429, "rate_limited", {"Retry-After": "600"}),
            ok({"data": []}),
        ])
        self.client().list_pools()
        self.assertEqual(self.sleeps, [3.0, 60.0])

    def test_exhausted_429_has_retry_after(self) -> None:
        self.server.reset([fail(429, "rate_limited", {"Retry-After": "2"})] * 3)
        with self.assertRaises(RateLimitError) as ctx:
            self.client().list_pools()
        self.assertEqual(ctx.exception.retry_after, 2)
        self.assertEqual(len(self.server.requests), 3)

    def test_backoff_bounds(self) -> None:
        for attempt, base in [(0, 0.5), (1, 1.0), (2, 2.0), (4, 8.0), (10, 8.0)]:
            for _ in range(20):
                d = _http.backoff(attempt)
                self.assertGreaterEqual(d, base)
                self.assertLessEqual(d, base * 1.25)

    def test_retry_after_http_date(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertTrue(20 <= _http.retry_delay(0, stamp) <= 31)
        self.assertIsNone(_http.parse_retry_after("soon"))


class TestErrors(ServerTestCase):
    def test_request_id_prefers_the_response_header(self) -> None:
        self.server.reset([fail(401, "unauthorized", {"X-Request-Id": "req-9"})])
        with self.assertRaises(AuthenticationError) as ctx:
            self.client().get_me()
        self.assertEqual(ctx.exception.request_id, "req-9")
        self.assertIn("req-9", str(ctx.exception))

    def test_error_fallback_to_body_error(self) -> None:
        self.server.reset([ok({"error": "legacy_code"}, 400)])
        with self.assertRaises(ValidationError) as ctx:
            self.client().get_me()
        self.assertEqual(ctx.exception.code, "legacy_code")
        self.assertEqual(ctx.exception.message, "legacy_code")

    def test_invalid_success_body(self) -> None:
        self.server.reset([ok("<html>proxy</html>")])
        with self.assertRaises(BzapperError) as ctx:
            self.client().get_me()
        self.assertEqual(ctx.exception.code, "INVALID_RESPONSE")
        self.assertEqual(ctx.exception.status_code, 200)
        self.assertTrue(ctx.exception.request_id)

    def test_204_returns_none(self) -> None:
        self.server.reset([ok(None, 204)])
        self.assertIsNone(self.client().delete_contact("c1"))


class TestArguments(ServerTestCase):
    def test_empty_key(self) -> None:
        with self.assertRaises(ValueError):
            Client("")
        with self.assertRaises(ValueError):
            PartnerClient("")

    def test_invalid_path_params_never_hit_the_network(self) -> None:
        self.server.reset([])
        c = self.client()
        for bad in ("", ".", ".."):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    c.get_contact(bad)
                with self.assertRaises(ValueError):
                    c.archive_chat(bad, "inst_1", True)
                with self.assertRaises(ValueError):
                    PartnerClient("bz_partner_x", base_url=self.server.base_url).get_connection(bad)
        self.assertEqual(self.server.requests, [])

    def test_path_segment_encoding(self) -> None:
        self.server.reset([ok({}), ok({})])
        c = self.client()
        c.get_group("120363000@g.us", "inst_1")
        c.pay_invoice("in 1/2")
        self.assertEqual(self.server.requests[0]["raw_path"], "/groups/120363000@g.us")
        self.assertEqual(self.server.requests[1]["raw_path"], "/me/invoices/in%201%2F2/pay")


class TestEncoding(ServerTestCase):
    def test_query_types(self) -> None:
        self.server.reset([ok({"data": []})])
        self.client().list_contacts(
            tags=["vip", "lead"],
            has_email=False,
            created_after=datetime(2026, 9, 1, 9, 0, tzinfo=timezone(timedelta(hours=-3))),
            search="ana maria",
        )
        r = self.server.requests[0]
        self.assertEqual(
            dict(r["query"]),
            {"tags": "vip,lead", "has_email": "false",
             "created_after": "2026-09-01T12:00:00Z", "search": "ana maria"},
        )
        self.assertNotIn("+", r["raw_query"])

    def test_omitted_fields_are_not_sent(self) -> None:
        self.server.reset([ok({})])
        self.client().update_contact("c1", name="Ana")
        self.assertEqual(json_body(self.server.requests[0]), {"name": "Ana"})

    def test_options_headers(self) -> None:
        self.server.reset([ok({})])
        Client(KEY, base_url=self.server.base_url, locale="pt-BR", project_id="proj_1").get_me()
        h = self.server.requests[0]["headers"]
        self.assertEqual(h["accept-language"], "pt-BR")
        self.assertEqual(h["x-project-id"], "proj_1")
        self.assertNotIn("content-type", h)

    def test_legacy_positional_base_url(self) -> None:
        c = Client(self.server.base_url, KEY)
        self.assertEqual(c.api_key, KEY)
        self.assertEqual(c.base_url, self.server.base_url)

    def test_default_options(self) -> None:
        c = Client(KEY)
        self.assertEqual(c.base_url, "https://api.bzapper.com.br")
        self.assertEqual(c.max_retries, 2)
        self.assertEqual(c.timeout, 30)


class TestUpload(ServerTestCase):
    def test_upload_from_bytes(self) -> None:
        self.server.reset([ok({"url": "https://cdn/x.png"})])
        out = self.client().upload_campaign_media(b"\x89PNG", "banner.png")
        r = self.server.requests[0]
        self.assertTrue(r["headers"]["content-type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'filename="banner.png"', r["body"])
        self.assertIn(b"Content-Type: image/png", r["body"])
        self.assertIn(b"\x89PNG", r["body"])
        self.assertEqual(out, {"url": "https://cdn/x.png"})

    def test_upload_from_path(self) -> None:
        self.server.reset([ok({"logo_url": "https://cdn/l.webp"})])
        with tempfile.NamedTemporaryFile(suffix=".webp") as fh:
            fh.write(b"RIFF....WEBP")
            fh.flush()
            self.client().upload_project_logo("proj_1", fh.name)
        r = self.server.requests[0]
        self.assertEqual(r["raw_path"], "/projects/proj_1/logo")
        self.assertIn(b"RIFF....WEBP", r["body"])
        self.assertTrue(r["headers"]["idempotency-key"])


class TestImportAndRotate(ServerTestCase):
    """O que os casos gerados não alcançam nas duas escritas novas."""

    def test_import_contacts_drops_empty_row_fields(self) -> None:
        self.server.reset([ok({"total": 2, "created": 2, "updated": 0, "skipped": 0, "failed": 0})])
        out = self.client().import_contacts(
            [
                {"phone": "+5511999990000", "name": "Ana", "email": None, "tags": ["lead"]},
                {"phone": "+5511888880000", "address": {"city": "São Paulo", "state": "SP"}},
            ]
        )
        r = self.server.requests[0]
        self.assertEqual(r["raw_path"], "/contacts/import")
        self.assertEqual(
            json_body(r),
            {
                "contacts": [
                    {"phone": "+5511999990000", "name": "Ana", "tags": ["lead"]},
                    {"phone": "+5511888880000", "address": {"city": "São Paulo", "state": "SP"}},
                ]
            },
        )
        self.assertTrue(r["headers"]["idempotency-key"])
        self.assertEqual(out["created"], 2)

    def test_import_contacts_dry_run_flag(self) -> None:
        self.server.reset([ok({"dry_run": True, "total": 1, "created": 1,
                               "updated": 0, "skipped": 0, "failed": 0})])
        self.client().import_contacts([{"phone": "+5511999990000"}], dry_run=True)
        self.assertEqual(
            json_body(self.server.requests[0]),
            {"contacts": [{"phone": "+5511999990000"}], "dry_run": True},
        )

    def test_rotate_key_without_a_grace_period_sends_no_body(self) -> None:
        self.server.reset([ok({"api_key": "bz_live_new", "key": {"id": "k2"}})])
        out = self.client().rotate_key("k1")
        r = self.server.requests[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["raw_path"], "/keys/k1/rotate")
        self.assertIsNone(json_body(r))
        self.assertNotIn("content-type", r["headers"])
        self.assertTrue(r["headers"]["idempotency-key"])
        self.assertEqual(out["api_key"], "bz_live_new")

    def test_rotate_key_revoke_now_sends_zero(self) -> None:
        self.server.reset([ok({"api_key": "bz_live_new", "key": {"id": "k2"},
                               "old_key_expires_at": None})])
        out = self.client().rotate_key("k1", revoke_in_seconds=0)
        self.assertEqual(json_body(self.server.requests[0]), {"revoke_in_seconds": 0})
        self.assertIsNone(out["old_key_expires_at"])

    def test_rotate_key_validates_the_path_segment(self) -> None:
        self.server.reset([])
        with self.assertRaises(ValueError):
            self.client().rotate_key("")
        with self.assertRaises(ValueError):
            self.client().rotate_key("..")
        self.assertEqual(self.server.requests, [], "nada vai para a rede")


if __name__ == "__main__":
    unittest.main()
