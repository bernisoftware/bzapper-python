"""``export_contacts`` — o único endpoint que NÃO responde JSON (BRIEF §6, "CSV").

Os casos gerados não cobrem ``exportContacts`` justamente porque a regra "2xx
não-JSON = INVALID_RESPONSE" não vale para ele. Então este arquivo cobre à mão o
que a conformidade não pode: o caminho + a query exatos, que a resposta
``text/csv`` NÃO passa pelo decodificador JSON (senão viraria
``INVALID_RESPONSE``) e que o texto volta íntegro — inclusive uma linha com
vírgula e aspas dentro do campo, que é onde um "parser esperto" estragaria o CSV.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qsl

from bzapper import BzapperError, Client

KEY = "bz_live_export"

# Linha 2: vírgula E aspas dentro do campo (CSV citado de verdade).
CSV_BODY = (
    "phone,name,email,status,source,tags,groups,created_at,last_activity_at\r\n"
    "+5511999990000,Ana Lúcia,ana@example.com,active,import,lead;vip,clientes,"
    "2026-09-01T12:00:00Z,2026-09-20T18:30:00Z\r\n"
    '+5511888880000,"Bar do Zé, Ltda","zé@example.com",active,widget,"lead,vip",'
    '"a ""b"" c",2026-09-02T09:00:00Z,\r\n'
)


class CsvServer:
    """Servidor local que responde ``text/csv`` como a API (anexo, BOM opcional)."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.body: bytes = CSV_BODY.encode("utf-8")
        self.status: int = 200
        self.content_type: str = "text/csv; charset=utf-8"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                path, _, qs = self.path.partition("?")
                outer.requests.append(
                    {
                        "raw_path": path,
                        "raw_query": qs,
                        "query": parse_qsl(qs, keep_blank_values=True),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                self.send_header(
                    "Content-Disposition", 'attachment; filename="contacts.csv"'
                )
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                if outer.body:
                    self.wfile.write(outer.body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "CsvServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class ExportContactsTest(unittest.TestCase):
    server: CsvServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = CsvServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.requests.clear()
        self.server.body = CSV_BODY.encode("utf-8")
        self.server.status = 200
        self.server.content_type = "text/csv; charset=utf-8"
        self.client = Client(KEY, base_url=self.server.base_url)

    def last(self) -> Dict[str, Any]:
        self.assertEqual(len(self.server.requests), 1, "exactly ONE request per call")
        return self.server.requests[0]

    # -- caminho, query e cabeçalhos -----------------------------------------

    def test_path_and_headers(self) -> None:
        self.client.export_contacts()
        req = self.last()
        self.assertEqual(req["raw_path"], "/contacts/export")
        self.assertEqual(req["raw_query"], "", "sem filtros = sem query")
        self.assertEqual(req["headers"]["authorization"], f"Bearer {KEY}")
        # Pede CSV — não JSON (é o único endpoint assim).
        self.assertEqual(req["headers"]["accept"], "text/csv")
        self.assertRegex(req["headers"]["x-bzapper-client"], r"^bzapper-python/")
        self.assertRegex(req["headers"]["x-request-id"], r"^[0-9a-f]{32}$")
        # GET não é escrita: nada de Idempotency-Key.
        self.assertNotIn("idempotency-key", req["headers"])

    def test_query_carries_the_same_filters_as_list_contacts(self) -> None:
        self.client.export_contacts(
            search="ana",
            tags=["lead", "vip"],
            tags_match="all",
            groups=["clientes"],
            project_id="current",
            instance_id="00000000-0000-4000-8000-000000000001",
            status="active",
            city="São Paulo",
            state="SP",
            country="BR",
            zip="01310-100",
            document="12345678901",
            has_email=True,
            last_activity_after="2026-09-01T00:00:00Z",
            last_activity_before="2026-09-30T00:00:00Z",
            created_after="2026-01-01T00:00:00Z",
            created_before="2026-12-31T00:00:00Z",
            sort="name",
            limit=5000,
        )
        req = self.last()
        self.assertEqual(
            dict(req["query"]),
            {
                "search": "ana",
                "tags": "lead,vip",  # form/explode=false: CSV
                "tags_match": "all",
                "groups": "clientes",
                "project_id": "current",
                "instance_id": "00000000-0000-4000-8000-000000000001",
                "status": "active",
                "city": "São Paulo",
                "state": "SP",
                "country": "BR",
                "zip": "01310-100",
                "document": "12345678901",
                "has_email": "true",  # booleano na fita: true/false
                "last_activity_after": "2026-09-01T00:00:00Z",
                "last_activity_before": "2026-09-30T00:00:00Z",
                "created_after": "2026-01-01T00:00:00Z",
                "created_before": "2026-12-31T00:00:00Z",
                "sort": "name",
                "limit": "5000",
            },
        )
        # offset não existe no export (a spec não tem): quem passar leva TypeError.
        with self.assertRaises(TypeError):
            self.client.export_contacts(offset=10)  # type: ignore[call-arg]

    # -- o texto volta íntegro (e ninguém tentou JSON) -----------------------

    def test_returns_the_csv_text_intact(self) -> None:
        out = self.client.export_contacts()
        self.assertIsInstance(out, str)
        self.assertEqual(out, CSV_BODY, "o CSV volta byte a byte (aspas e \\r\\n inclusive)")
        # Não é um dict/lista decodificado: é texto. E o texto NÃO é JSON válido —
        # se a SDK tivesse passado pelo caminho JSON, teria estourado
        # INVALID_RESPONSE em vez de devolver isto.
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_the_quoted_row_survives_a_real_csv_reader(self) -> None:
        rows = list(csv.DictReader(io.StringIO(self.client.export_contacts())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["phone"], "+5511999990000")
        self.assertEqual(rows[0]["tags"], "lead;vip")
        # A linha com vírgula e aspas dentro do campo:
        self.assertEqual(rows[1]["name"], "Bar do Zé, Ltda")
        self.assertEqual(rows[1]["tags"], "lead,vip")
        self.assertEqual(rows[1]["groups"], 'a "b" c')
        self.assertEqual(rows[1]["last_activity_at"], "")

    def test_bom_is_stripped_and_utf8_is_decoded(self) -> None:
        self.server.body = ("﻿" + CSV_BODY).encode("utf-8")
        out = self.client.export_contacts()
        self.assertTrue(out.startswith("phone,"), "BOM de planilha não vaza para o texto")
        self.assertIn("Ana Lúcia", out)

    def test_empty_body_is_an_empty_string(self) -> None:
        self.server.body = b""
        self.assertEqual(self.client.export_contacts(), "")

    def test_error_is_still_a_typed_error(self) -> None:
        self.server.status = 401
        self.server.content_type = "application/json"
        self.server.body = json.dumps(
            {"code": "unauthorized", "message": "chave inválida", "locale": "pt-BR"}
        ).encode("utf-8")
        with self.assertRaises(BzapperError) as ctx:
            self.client.export_contacts()
        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertEqual(ctx.exception.status, 401)

    def test_other_calls_still_ask_for_json(self) -> None:
        """A troca de Accept é só deste método (nada de vazar para o resto)."""
        self.server.content_type = "application/json"
        self.server.body = b'{"data": []}'
        self.assertEqual(self.client.list_contacts(), {"data": []})
        self.assertEqual(self.last()["headers"]["accept"], "application/json")


if __name__ == "__main__":
    unittest.main()
