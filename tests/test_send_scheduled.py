"""Todo envio aceita ``scheduled_at`` e o entrega no corpo da requisição.

Regressão do bug 0.3.0: os métodos de mídia repassavam ``scheduled_at`` para
``_send_media()``, cuja assinatura não tinha o parâmetro — TypeError em TODO
envio de mídia. Os testes de mídia só exercitavam o caminho sem agendamento,
então as duas assinaturas puderam divergir sem ninguém perceber.

Os testes interceptam ``urlopen``, e não ``_request``, para conferir o corpo
que de fato vai para a rede (``_request`` remove chaves ``None``).
"""

from __future__ import annotations

import inspect
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


class TestSignatureAlignment(unittest.TestCase):
    """As assinaturas não podem divergir de novo — o defeito era esse.

    Trava a classe inteira do bug, não só o ``scheduled_at``: todo método
    público de envio aceita o SendBase completo, e o ``_send_media`` aceita
    tudo que os públicos repassam para ele.
    """

    SENDBASE = set(inspect.signature(Client._send_base).parameters) - {"to"}

    def _keyword_only(self, fn: Any) -> set:
        return {
            name
            for name, p in inspect.signature(fn).parameters.items()
            if p.kind is p.KEYWORD_ONLY
        }

    def test_send_media_aceita_o_sendbase_inteiro(self) -> None:
        faltando = self.SENDBASE - self._keyword_only(Client._send_media)
        self.assertEqual(faltando, set(), f"_send_media não aceita: {sorted(faltando)}")

    def test_metodos_publicos_aceitam_o_sendbase_inteiro(self) -> None:
        # send_reaction recebe quoted_message_id como posicional obrigatório.
        excecoes = {"send_reaction": {"quoted_message_id"}}
        for name, fn in sorted(vars(Client).items()):
            if not name.startswith("send_"):
                continue
            with self.subTest(method=name):
                aceitos = self._keyword_only(fn) | set(
                    inspect.signature(fn).parameters
                )
                faltando = self.SENDBASE - aceitos - excecoes.get(name, set())
                self.assertEqual(faltando, set(), f"{name} não aceita: {sorted(faltando)}")

    def test_publicos_de_midia_repassam_tudo_que_recebem(self) -> None:
        """O que o público aceita, o _send_media tem que aceitar também."""
        aceito_pelo_helper = self._keyword_only(Client._send_media)
        for name in (
            "send_image",
            "send_video",
            "send_document",
            "send_audio",
            "send_sticker",
        ):
            with self.subTest(method=name):
                publico = self._keyword_only(getattr(Client, name))
                faltando = publico - aceito_pelo_helper
                self.assertEqual(
                    faltando,
                    set(),
                    f"{name} aceita {sorted(faltando)}, que _send_media não recebe",
                )


if __name__ == "__main__":
    unittest.main()
