"""Todo envio aceita ``scheduled_at`` e o entrega no corpo da requisição.

Regressão do bug de 0.4.0, 0.5.0 e 0.6.0: os métodos de mídia repassavam
``scheduled_at`` para ``_send_media()``, cuja assinatura não tinha o parâmetro
— TypeError em TODO envio de mídia, nas três releases. (A 0.3.0 era sã: o
parâmetro ainda não existia; a 0.6.1 corrigiu.) Os testes de mídia só
exercitavam o caminho sem agendamento, então as duas assinaturas puderam
divergir sem ninguém perceber.

Os testes interceptam ``urlopen``, e não ``_request``, para conferir o corpo
que de fato vai para a rede (``_request`` remove chaves ``None``).

Aqui fica só o COMPORTAMENTO do ``scheduled_at``. A guarda estrutural contra a
classe do defeito — divergência entre a assinatura de um helper privado e o que
os públicos lhe passam — vive em ``test_delegation.py``, que cobre os 83 pontos
de delegação e não só o ``_send_media``.
"""

from __future__ import annotations

import io
import json
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from bzapper.client import Client

WHEN = "2026-09-01T12:00:00Z"


class _Capture:
    """Substitui ``urlopen`` e guarda a requisição feita."""

    def __init__(self) -> None:
        self.requests: List[Any] = []

    def __call__(self, req: Any, timeout: Optional[float] = None) -> Any:
        self.requests.append(req)
        resp = io.BytesIO(json.dumps({"message_id": "m_1", "status": "scheduled"}).encode())
        resp.__enter__ = lambda: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        return resp

    @property
    def body(self) -> Dict[str, Any]:
        return json.loads(self.requests[-1].data.decode())

    @property
    def path(self) -> str:
        return self.requests[-1].full_url


class SendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client("bz_test_key", base_url="https://api.example")
        self.capture = _Capture()
        patcher = mock.patch("bzapper.client.urllib.request.urlopen", self.capture)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMediaScheduledAt(SendTestCase):
    """Um teste por método de mídia, com ``scheduled_at`` preenchido."""

    MEDIA = {"url": "https://example.com/f.bin", "caption": "oi"}

    def _assert_scheduled(self, method: str, path: str) -> None:
        result = getattr(self.client, method)(
            "+5511999999999", self.MEDIA, scheduled_at=WHEN
        )
        self.assertTrue(self.capture.path.endswith(path), self.capture.path)
        body = self.capture.body
        self.assertEqual(body["scheduled_at"], WHEN, f"{method} não enviou scheduled_at")
        self.assertEqual(body["to"], "+5511999999999")
        self.assertEqual(body["media"], self.MEDIA)
        self.assertEqual(result["status"], "scheduled")

    def test_send_image_scheduled(self) -> None:
        self._assert_scheduled("send_image", "/messages/image")

    def test_send_video_scheduled(self) -> None:
        self._assert_scheduled("send_video", "/messages/video")

    def test_send_document_scheduled(self) -> None:
        self._assert_scheduled("send_document", "/messages/document")

    def test_send_audio_scheduled(self) -> None:
        self._assert_scheduled("send_audio", "/messages/audio")

    def test_send_sticker_scheduled(self) -> None:
        self._assert_scheduled("send_sticker", "/messages/sticker")

    def test_media_sem_agendamento_nao_manda_a_chave(self) -> None:
        self.client.send_image("+5511999999999", self.MEDIA)
        self.assertNotIn("scheduled_at", self.capture.body)


class TestMediaSendBasePassthrough(SendTestCase):
    """Todo campo do SendBase chega ao corpo pelo caminho de mídia."""

    def test_todos_os_campos_do_sendbase(self) -> None:
        self.client.send_document(
            "+5511999999999",
            {"url": "https://example.com/boleto.pdf"},
            instance_id="inst_1",
            pool_id="pool_1",
            quoted_message_id="wamid.1",
            client_reference="ref-1",
            mentions=["5511888888888@s.whatsapp.net"],
            sticky=False,
            scheduled_at=WHEN,
        )
        self.assertEqual(
            self.capture.body,
            {
                "to": "+5511999999999",
                "instance_id": "inst_1",
                "pool_id": "pool_1",
                "quoted_message_id": "wamid.1",
                "client_reference": "ref-1",
                "mentions": ["5511888888888@s.whatsapp.net"],
                "sticky": False,
                "scheduled_at": WHEN,
                "media": {"url": "https://example.com/boleto.pdf"},
            },
        )


if __name__ == "__main__":
    unittest.main()
