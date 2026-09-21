"""``__version__`` e o manifesto não podem divergir.

Mesma classe do bug de assinatura: dois lugares que precisam concordar e
pararam de concordar. O ``release-sdks.sh`` só bumpava o ``pyproject.toml``,
então o ``__version__`` congelou em 0.3.0 desde a v0.3.0 — e era a versão que
o usuário via ao reportar o bug.

Agora a versão também vai no header ``X-Bzapper-Client`` de toda requisição: se
ela congelar de novo, a API passa a achar que a conta roda uma versão velha (ou
antiga demais) e o aviso de atualização vai para o alvo errado.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import bzapper
from bzapper.client import USER_AGENT, Client

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


class TestVersionAlignment(unittest.TestCase):
    def test_version_bate_com_o_pyproject(self) -> None:
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', PYPROJECT.read_text())
        self.assertIsNotNone(m, "não achei version no pyproject.toml")
        assert m is not None
        self.assertEqual(
            bzapper.__version__,
            m.group(1),
            "bzapper.__version__ divergiu do pyproject.toml — o bump da release "
            "precisa alterar os dois (ver scripts/release-sdks.sh)",
        )


class TestIdentificacaoDoCliente(unittest.TestCase):
    """O header de identificação vai em TODA requisição, com a versão certa.

    É por ele que a API sabe qual SDK/versão a conta roda e, quando uma
    correção exige mexer no código da integração, avisa só quem é afetado.
    Se ele sumir ou congelar, o aviso vai para o alvo errado — em silêncio.
    """

    def test_formato(self) -> None:
        self.assertEqual(USER_AGENT, f"bzapper-python/{bzapper.__version__}")

    def test_vai_nos_headers(self) -> None:
        headers = Client("bz_test_key")._headers()
        self.assertEqual(headers["X-Bzapper-Client"], USER_AGENT)
        self.assertEqual(headers["User-Agent"], USER_AGENT)


if __name__ == "__main__":
    unittest.main()
