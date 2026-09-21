"""``idempotency_key`` vira o cabeçalho ``Idempotency-Key`` em TODO envio.

Nunca vai no corpo; sem ele, o cabeçalho não é enviado (nada muda para quem não
usa). Também cobre ``preview_group_invite`` e o ``phone`` do remetente no webhook.
"""

from __future__ import annotations

import io
import json
import unittest
from typing import Any, List, Optional
from unittest import mock

from bzapper.client import Client
from bzapper.webhooks import WebhookEvent

from test_delegation import _chamada, _metodos_de_envio


class _Transport:
    def __init__(self, payload: Any = None) -> None:
        self.requests: List[Any] = []
        self.payload = payload or {"message_id": "m_1", "status": "queued"}

    def __call__(self, req: Any, timeout: Optional[float] = None) -> Any:
        self.requests.append(req)
        resp = io.BytesIO(json.dumps(self.payload).encode())
        resp.__enter__ = lambda: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        return resp


class IdempotencyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client("bz_test_key", base_url="https://api.example")
        self.transport = _Transport()
        patcher = mock.patch("bzapper.client.urllib.request.urlopen", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_todo_envio_manda_o_cabecalho(self) -> None:
        for nome in _metodos_de_envio():
            with self.subTest(metodo=nome):
                args, kwargs = _chamada(nome)
                getattr(self.client, nome)(*args, idempotency_key=f"k-{nome}", **kwargs)
                req = self.transport.requests[-1]
                self.assertEqual(req.get_header("Idempotency-key"), f"k-{nome}")
                corpo = json.loads(req.data.decode())
                self.assertNotIn("idempotency_key", corpo)

    def test_sem_chave_sem_cabecalho(self) -> None:
        self.client.send_text("+5511999999999", "oi")
        self.assertIsNone(self.transport.requests[-1].get_header("Idempotency-key"))

    def test_preview_group_invite(self) -> None:
        self.transport.payload = {"jid": "1@g.us", "name": "G", "size": 3}
        out = self.client.preview_group_invite("inst_1", "ABC")
        req = self.transport.requests[-1]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.example/groups/join/preview?instance_id=inst_1")
        self.assertEqual(json.loads(req.data.decode()), {"code": "ABC"})
        self.assertEqual(out["size"], 3)


class SenderPhoneTest(unittest.TestCase):
    def test_sender_phone(self) -> None:
        ev = WebhookEvent.from_dict(
            {
                "event_id": "e1",
                "event_type": "message.received",
                "sender": {"jid": "9@lid", "lid": "9@lid", "phone": "+5511999999999"},
                "payload": {"quoted_participant": "+5511888888888"},
            }
        )
        self.assertEqual(ev.sender.phone, "+5511999999999")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
