"""bZapper Connect: cliente do parceiro, apps conectados e webhook do parceiro.

Os testes interceptam ``urlopen`` (e não ``_request``) para conferir o que de
fato vai para a rede: método, caminho, query, header de autenticação e corpo.
O ``PartnerClient`` reaproveita o encanamento do ``Client`` por composição, então
o mesmo patch em ``bzapper.client`` cobre os dois.

Como em ``test_delegation.py``, TODO método público do ``PartnerClient`` é
invocado por introspecção: um método novo cuja delegação para ``_request`` não
feche quebra aqui, antes de chegar ao PyPI.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import io
import json
import unittest
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Optional
from unittest import mock

import bzapper
from bzapper import BzapperError, Client, PartnerClient
from bzapper.client import USER_AGENT
from bzapper.webhooks import (
    CONNECT_EVENT_TYPES,
    EVENT_TYPES,
    ConnectionRef,
    SignatureError,
    Webhooks,
    construct_event,
    verify,
)

SECRET = "bz_partner_test"


class _Capture:
    """Substitui ``urlopen``: guarda a requisição e devolve ``status``/``payload``."""

    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.requests: List[Any] = []
        self.errors: List[urllib.error.HTTPError] = []
        self.payload = payload
        self.status = status

    def __call__(self, req: Any, timeout: Optional[float] = None) -> Any:
        self.requests.append(req)
        raw = b"" if self.payload is None else json.dumps(self.payload).encode()
        if self.status >= 400:
            err = urllib.error.HTTPError(
                req.full_url, self.status, "err", {}, io.BytesIO(raw)  # type: ignore[arg-type]
            )
            self.errors.append(err)
            raise err
        resp = io.BytesIO(raw)
        resp.__enter__ = lambda: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        return resp

    @property
    def last(self) -> Any:
        return self.requests[-1]

    @property
    def body(self) -> Dict[str, Any]:
        return json.loads(self.last.data.decode())

    @property
    def url(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.last.full_url)

    @property
    def query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(self.url.query)

    def header(self, name: str) -> Optional[str]:
        # urllib.request.Request normaliza o nome com capitalize().
        return self.last.get_header(name.capitalize())


class ConnectTestCase(unittest.TestCase):
    payload: Any = {"id": "conn_1", "status": "pending_account"}
    status = 200

    def setUp(self) -> None:
        self.capture = _Capture(self.payload, self.status)
        patcher = mock.patch("bzapper.client.urllib.request.urlopen", self.capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [e.close() for e in self.capture.errors])
        self.partner = PartnerClient(SECRET, base_url="https://api.example")


class TestPartnerClient(ConnectTestCase):
    def test_exportado_no_pacote(self) -> None:
        self.assertIn("PartnerClient", bzapper.__all__)

    def test_secret_obrigatorio(self) -> None:
        with self.assertRaises(ValueError):
            PartnerClient("")

    def test_base_url_padrao_e_producao(self) -> None:
        self.assertEqual(PartnerClient(SECRET).base_url, "https://api.bzapper.com.br")

    def test_me(self) -> None:
        self.partner.me()
        self.assertEqual(self.capture.last.get_method(), "GET")
        self.assertEqual(self.capture.url.path, "/partner/me")
        self.assertEqual(self.capture.header("Authorization"), f"Bearer {SECRET}")
        self.assertEqual(self.capture.header("X-Bzapper-Client"), USER_AGENT)

    def test_create_connect_session(self) -> None:
        customer = {
            "name": "Ana Souza",
            "email": "ana@boxy.com",
            "phone": "+5511988887777",
            "company": "Boxy Pharma",
            "country": "BR",
        }
        self.partner.create_connect_session("cust-42", customer, locale="pt-BR")
        self.assertEqual(self.capture.last.get_method(), "POST")
        self.assertEqual(self.capture.url.geturl(), "https://api.example/partner/connect-sessions")
        self.assertEqual(self.capture.header("Authorization"), f"Bearer {SECRET}")
        self.assertEqual(self.capture.header("Content-Type"), "application/json")
        self.assertEqual(
            self.capture.body,
            {"external_id": "cust-42", "customer": customer, "locale": "pt-BR"},
        )

    def test_create_connect_session_sem_locale_nao_manda_a_chave(self) -> None:
        self.partner.create_connect_session("cust-42", {"name": "Ana", "email": "a@b.co"})
        self.assertNotIn("locale", self.capture.body)

    def test_exchange_code(self) -> None:
        self.capture.payload = {"id": "conn_1", "status": "active", "api_key": "bz_live_x"}
        res = self.partner.exchange_code("cc_91ab")
        self.assertEqual(self.capture.last.get_method(), "POST")
        self.assertEqual(self.capture.url.path, "/partner/connect/exchange")
        self.assertEqual(self.capture.body, {"code": "cc_91ab"})
        self.assertEqual(res["api_key"], "bz_live_x")

    def test_list_connections_com_filtros(self) -> None:
        self.capture.payload = {"data": [{"id": "conn_1"}]}
        res = self.partner.list_connections(external_id="cust-42", status="suspended")
        self.assertEqual(self.capture.last.get_method(), "GET")
        self.assertEqual(self.capture.url.path, "/partner/connections")
        self.assertEqual(
            self.capture.query, {"external_id": ["cust-42"], "status": ["suspended"]}
        )
        self.assertIsNone(self.capture.last.data)
        self.assertEqual(res["data"][0]["id"], "conn_1")

    def test_list_connections_sem_filtros(self) -> None:
        self.partner.list_connections()
        self.assertEqual(self.capture.url.query, "")

    def test_get_connection(self) -> None:
        self.partner.get_connection("conn_1")
        self.assertEqual(self.capture.last.get_method(), "GET")
        self.assertEqual(self.capture.url.path, "/partner/connections/conn_1")

    def test_rotate_connection_key(self) -> None:
        self.partner.rotate_connection_key("conn_1")
        self.assertEqual(self.capture.last.get_method(), "POST")
        self.assertEqual(self.capture.url.path, "/partner/connections/conn_1/rotate-key")

    def test_id_vai_escapado_no_caminho(self) -> None:
        self.partner.get_connection("a/b")
        self.assertTrue(self.capture.last.full_url.endswith("/partner/connections/a%2Fb"))

    def test_todo_metodo_publico_executa(self) -> None:
        """Invoca cada método público; um TypeError de delegação quebra aqui."""
        nomes = sorted(
            n for n, o in vars(PartnerClient).items()
            if not n.startswith("_") and inspect.isfunction(o)
        )
        self.assertGreaterEqual(len(nomes), 7, nomes)
        for nome in nomes:
            with self.subTest(metodo=nome):
                sig = inspect.signature(getattr(PartnerClient, nome))
                args = [
                    {"email": "a@b.co"} if p.name == "customer" else "x"
                    for n, p in sig.parameters.items()
                    if n != "self" and p.default is p.empty
                ]
                antes = len(self.capture.requests)
                getattr(self.partner, nome)(*args)
                self.assertEqual(len(self.capture.requests), antes + 1)


class TestPartnerRevoke204(ConnectTestCase):
    payload = None  # 204: corpo vazio

    def test_revoke_connection(self) -> None:
        res = self.partner.revoke_connection("conn_1")
        self.assertIsNone(res)
        self.assertEqual(self.capture.last.get_method(), "DELETE")
        self.assertEqual(self.capture.url.path, "/partner/connections/conn_1")
        self.assertEqual(self.capture.header("Authorization"), f"Bearer {SECRET}")


class TestConnectSuspended(ConnectTestCase):
    """A key do cliente responde 402 ``connect_suspended`` enquanto o Pro não é pago."""

    payload = {"code": "connect_suspended", "message": "Pro unpaid", "locale": "en"}
    status = 402

    def test_vira_bzapper_error_com_code_estavel(self) -> None:
        client = Client("bz_live_customer", base_url="https://api.example")
        with self.assertRaises(BzapperError) as ctx:
            client.send_text("+5511999999999", "oi")
        self.assertEqual(ctx.exception.code, "connect_suspended")
        self.assertEqual(ctx.exception.status_code, 402)


class TestConnectedApps(ConnectTestCase):
    payload = {"data": [{"id": "conn_1", "partner_name": "bFocus", "status": "active"}]}

    def setUp(self) -> None:
        super().setUp()
        self.client = Client("bz_live_customer", base_url="https://api.example")

    def test_list_connected_apps(self) -> None:
        res = self.client.list_connected_apps()
        self.assertEqual(self.capture.last.get_method(), "GET")
        self.assertEqual(self.capture.url.path, "/me/connections")
        self.assertEqual(self.capture.header("Authorization"), "Bearer bz_live_customer")
        self.assertEqual(res["data"][0]["partner_name"], "bFocus")

    def test_revoke_connected_app(self) -> None:
        self.capture.payload = None
        res = self.client.revoke_connected_app("conn_1")
        self.assertIsNone(res)
        self.assertEqual(self.capture.last.get_method(), "DELETE")
        self.assertEqual(self.capture.url.path, "/me/connections/conn_1")


class TestPartnerWebhook(unittest.TestCase):
    """Webhook do parceiro: mesma assinatura HMAC, envelope + ``connection``."""

    WH_SECRET = "whsec_partner"
    ENVELOPE = {
        "event_id": "evt_1",
        "event_type": "connect.suspended",
        "timestamp": "2026-09-17T12:00:00Z",
        "payload": {"reason": "payment_failed"},
        "connection": {
            "id": "conn_1",
            "external_id": "cust-42",
            "account_id": "acc_1",
            "project_id": "proj_1",
            "status": "suspended",
        },
    }

    def _assinado(self) -> tuple:
        raw = json.dumps(self.ENVELOPE).encode()
        sig = "sha256=" + hmac.new(self.WH_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return raw, sig

    def test_tipos_connect_estao_no_catalogo(self) -> None:
        self.assertEqual(
            CONNECT_EVENT_TYPES,
            ("connect.completed", "connect.suspended", "connect.resumed", "connect.revoked"),
        )
        for t in CONNECT_EVENT_TYPES:
            self.assertIn(t, EVENT_TYPES)

    def test_verifica_e_parseia_connection(self) -> None:
        raw, sig = self._assinado()
        self.assertTrue(verify(self.WH_SECRET, raw, sig))
        event = construct_event(self.WH_SECRET, raw, sig)
        self.assertEqual(event.type, "connect.suspended")
        self.assertEqual(
            event.connection,
            ConnectionRef(
                id="conn_1", external_id="cust-42", account_id="acc_1",
                project_id="proj_1", status="suspended",
            ),
        )

    def test_assinatura_invalida(self) -> None:
        raw, _ = self._assinado()
        with self.assertRaises(SignatureError):
            construct_event(self.WH_SECRET, raw, "sha256=deadbeef")

    def test_roteia_para_o_handler(self) -> None:
        raw, sig = self._assinado()
        hooks = Webhooks(secret=self.WH_SECRET)
        vistos: List[str] = []
        hooks.on("connect.suspended", lambda e: vistos.append(e.connection.external_id))
        hooks.handle(raw_body=raw, signature=sig)
        self.assertEqual(vistos, ["cust-42"])

    def test_envelope_sem_connection_continua_valido(self) -> None:
        env = {"event_id": "evt_2", "event_type": "message.received", "payload": {}}
        raw = json.dumps(env).encode()
        sig = "sha256=" + hmac.new(self.WH_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        self.assertIsNone(construct_event(self.WH_SECRET, raw, sig).connection)


if __name__ == "__main__":
    unittest.main()
