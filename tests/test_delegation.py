"""Guarda contra a CLASSE de defeito que quebrou 0.4.0, 0.5.0 e 0.6.0.

Nessas três releases, ``_send_media()`` não declarava ``scheduled_at``, mas os
cinco métodos públicos de mídia o repassavam. Todo envio de mídia morria em
``TypeError`` ANTES de qualquer requisição HTTP — 100% de falha, três releases
seguidas. Passou porque nenhum teste jamais CHAMOU ``send_document`` (nem os
outros quatro): a suíte só exercitava texto.

A causa não é o ``scheduled_at``. É o formato do SDK Python: cada método público
redeclara os campos do SendBase e os repassa um a um para um helper privado.
Os outros 5 SDKs passam um objeto/struct de opções inteiro e são imunes por
construção. Aqui há 83 pontos de delegação interna — ``_request`` (65
chamadores), ``_send_base`` (9) e ``_send_media`` (5) — e QUALQUER um deles pode
divergir da assinatura do privado sem que nada acuse.

Este arquivo fecha as duas portas:

1. :class:`TestTodoEnvioPublicoExecuta` INVOCA todo método público ``send_*``
   contra um transporte falso, com todos os campos do SendBase preenchidos.
   Os métodos são descobertos por introspecção e os argumentos obrigatórios
   são sintetizados a partir das anotações — um método novo entra na matriz
   sozinho, sem ninguém precisar lembrar de registrá-lo.

2. :class:`TestDelegacaoInterna` lê o AST do próprio ``client.py`` e confere,
   em TODA chamada ``self._privado(...)``, que cada keyword existe na
   assinatura do privado. Cobre os 83 pontos, inclusive os que nenhum teste
   executa, e vale para helpers que ainda nem foram escritos.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from bzapper.client import Client

# SendBase completo: o que todo envio aceita além do destino. Derivado da
# assinatura real — se um campo for acrescentado lá, ele passa a ser exigido
# aqui automaticamente.
SENDBASE: Dict[str, Any] = {
    "instance_id": "inst_1",
    "pool_id": "pool_1",
    "quoted_message_id": "wamid.1",
    "quoted_participant": "+5511777777777",
    "client_reference": "ref-1",
    "mentions": ["5511888888888@s.whatsapp.net"],
    "sticky": False,
    "scheduled_at": "2026-09-01T12:00:00Z",
}


class _Transport:
    """Substitui ``urlopen`` e guarda a requisição que teria ido para a rede."""

    def __init__(self) -> None:
        self.requests: List[Any] = []

    def __call__(self, req: Any, timeout: Optional[float] = None) -> Any:
        self.requests.append(req)
        resp = io.BytesIO(json.dumps({"message_id": "m_1", "status": "queued"}).encode())
        resp.__enter__ = lambda: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        return resp

    @property
    def body(self) -> Dict[str, Any]:
        return json.loads(self.requests[-1].data.decode())


def _valor_para(anotacao: Any) -> Any:
    """Sintetiza um argumento plausível a partir da anotação.

    ``client.py`` usa ``from __future__ import annotations``, então as anotações
    chegam como string. O SDK não valida nada — só monta o corpo —, então
    qualquer valor do tipo certo serve.
    """
    texto = anotacao if isinstance(anotacao, str) else getattr(anotacao, "__name__", str(anotacao))
    if texto.startswith("Sequence[Mapping") or texto.startswith("Sequence[Dict"):
        return [{"id": "x", "title": "x", "rows": [{"id": "r", "title": "r"}]}]
    if texto.startswith("Sequence") or texto.startswith("List"):
        return ["a", "b"]
    if texto.startswith("Mapping") or texto.startswith("Dict"):
        return {"url": "https://example.com/arquivo.bin"}
    if texto == "float":
        return 1.5
    if texto == "int":
        return 1
    if texto == "bool":
        return True
    return "x"


def _metodos_de_envio() -> List[str]:
    """Todo método público ``send_*`` da classe, descoberto por introspecção."""
    return sorted(
        nome
        for nome, obj in vars(Client).items()
        if nome.startswith("send_") and inspect.isfunction(obj)
    )


def _chamada(nome: str) -> Tuple[List[Any], Dict[str, Any]]:
    """Monta (args, kwargs) para invocar ``nome`` com o SendBase inteiro.

    Os obrigatórios saem das anotações; os campos do SendBase só entram como
    keyword nos métodos que os declaram como keyword-only — ``send_reaction``
    recebe ``quoted_message_id`` posicionalmente e receberia duas vezes.
    """
    sig = inspect.signature(getattr(Client, nome))
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}
    for param_nome, p in sig.parameters.items():
        if param_nome == "self":
            continue
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            if p.default is p.empty:
                args.append(_valor_para(p.annotation))
        elif p.kind is p.KEYWORD_ONLY and param_nome in SENDBASE:
            kwargs[param_nome] = SENDBASE[param_nome]
    return args, kwargs


class TestTodoEnvioPublicoExecuta(unittest.TestCase):
    """Todo ``send_*`` completa a chamada e põe o SendBase no corpo.

    É o teste que faltava: um único percurso por método público teria barrado
    as três releases quebradas. Não afirma nada sobre a semântica de cada tipo
    — só que a delegação interna fecha e que nada se perde no caminho.
    """

    def setUp(self) -> None:
        self.client = Client("bz_test_key", base_url="https://api.example")
        self.transport = _Transport()
        patcher = mock.patch("bzapper.client.urllib.request.urlopen", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matriz_nao_esta_vazia(self) -> None:
        """Se a descoberta parar de achar métodos, a matriz vira um no-op."""
        metodos = _metodos_de_envio()
        self.assertGreaterEqual(len(metodos), 13, f"só achei {metodos}")
        self.assertIn("send_document", metodos)

    def test_todo_metodo_publico_de_envio(self) -> None:
        for nome in _metodos_de_envio():
            with self.subTest(metodo=nome):
                args, kwargs = _chamada(nome)
                # Um TypeError aqui é exatamente o bug de 0.4.0/0.5.0/0.6.0.
                getattr(self.client, nome)(*args, **kwargs)

                self.assertTrue(self.transport.requests, f"{nome} não fez requisição")
                corpo = self.transport.body
                for campo, valor in kwargs.items():
                    self.assertIn(campo, corpo, f"{nome} engoliu '{campo}'")
                    self.assertEqual(
                        corpo[campo], valor, f"{nome} adulterou '{campo}'"
                    )

    def test_todo_metodo_publico_aceita_o_sendbase_inteiro(self) -> None:
        """Nenhum envio pode ficar sem um campo do SendBase (exceto posicionais)."""
        for nome in _metodos_de_envio():
            with self.subTest(metodo=nome):
                sig = inspect.signature(getattr(Client, nome))
                aceitos = set(sig.parameters)
                faltando = set(SENDBASE) - aceitos
                self.assertEqual(
                    faltando, set(), f"{nome} não aceita: {sorted(faltando)}"
                )

    def test_sendbase_do_teste_bate_com_o_do_codigo(self) -> None:
        """Se um campo entrar no ``_send_base``, ele passa a ser exigido acima."""
        real = set(inspect.signature(Client._send_base).parameters) - {"to"}
        self.assertEqual(
            real,
            set(SENDBASE),
            "SENDBASE deste teste divergiu do _send_base — atualize a matriz",
        )


class TestDelegacaoInterna(unittest.TestCase):
    """Toda chamada ``self._privado(kw=...)`` bate com a assinatura do privado.

    Camada estática: alcança os pontos de delegação que nenhum teste executa,
    e não exige que alguém se lembre de escrever um teste para o helper novo.
    """

    @classmethod
    def setUpClass(cls) -> None:
        fonte = inspect.getsource(inspect.getmodule(Client))
        arvore = ast.parse(fonte)
        classe = next(
            n
            for n in arvore.body
            if isinstance(n, ast.ClassDef) and n.name == "Client"
        )
        cls.metodos = {
            n.name: n
            for n in classe.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @staticmethod
    def _assinatura(fn: ast.AST) -> Tuple[set, bool, int, bool]:
        a = fn.args  # type: ignore[attr-defined]
        nomes = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
        posicionais = [p.arg for p in a.posonlyargs + a.args]
        return nomes, a.kwarg is not None, len(posicionais), a.vararg is not None

    def _delegacoes(self):
        """Todo ``self._x(...)``/``Client._x(...)`` para um método da classe."""
        for chamador, fn in self.metodos.items():
            for no in ast.walk(fn):
                if not isinstance(no, ast.Call):
                    continue
                alvo = no.func
                if not isinstance(alvo, ast.Attribute):
                    continue
                if not (
                    isinstance(alvo.value, ast.Name)
                    and alvo.value.id in ("self", "Client")
                ):
                    continue
                if alvo.attr not in self.metodos:
                    continue
                yield chamador, alvo.attr, no

    def test_encontrou_os_pontos_de_delegacao(self) -> None:
        """Se o walk parar de achar chamadas, o teste abaixo vira um no-op."""
        pontos = list(self._delegacoes())
        self.assertGreater(len(pontos), 50, f"só achei {len(pontos)} delegações")
        alvos = {alvo for _, alvo, _ in pontos}
        self.assertTrue({"_send_base", "_send_media", "_request"} <= alvos, alvos)

    def test_toda_keyword_existe_na_assinatura_do_privado(self) -> None:
        for chamador, alvo, no in self._delegacoes():
            aceitos, tem_kwargs, _, _ = self._assinatura(self.metodos[alvo])
            for kw in no.keywords:
                if kw.arg is None or tem_kwargs:  # **kwargs aceita qualquer coisa
                    continue
                with self.subTest(chamador=chamador, alvo=alvo, kw=kw.arg):
                    self.assertIn(
                        kw.arg,
                        aceitos,
                        f"linha {no.lineno}: {chamador}() passa '{kw.arg}' para "
                        f"{alvo}(), que não recebe esse parâmetro",
                    )

    def test_nenhuma_chamada_estoura_os_posicionais(self) -> None:
        for chamador, alvo, no in self._delegacoes():
            _, _, limite, tem_varargs = self._assinatura(self.metodos[alvo])
            if tem_varargs:
                continue
            fn = self.metodos[alvo]
            # `self` não é passado explicitamente em self.x(...); nos
            # @staticmethod chamados via Client.x(...) também não existe.
            decoradores = {
                d.id for d in fn.decorator_list if isinstance(d, ast.Name)
            }
            disponivel = limite if "staticmethod" in decoradores else limite - 1
            with self.subTest(chamador=chamador, alvo=alvo):
                self.assertLessEqual(
                    len(no.args),
                    disponivel,
                    f"linha {no.lineno}: {chamador}() passa {len(no.args)} "
                    f"posicionais para {alvo}(), que aceita {disponivel}",
                )


if __name__ == "__main__":
    unittest.main()
