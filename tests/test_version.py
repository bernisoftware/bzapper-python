"""``__version__`` e o manifesto não podem divergir.

Mesma classe do bug de assinatura: dois lugares que precisam concordar e
pararam de concordar. O ``release-sdks.sh`` só bumpava o ``pyproject.toml``,
então o ``__version__`` congelou em 0.3.0 desde a v0.3.0 — e era a versão que
o usuário via ao reportar o bug.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import bzapper

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


if __name__ == "__main__":
    unittest.main()
