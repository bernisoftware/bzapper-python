"""Versão do SDK — módulo próprio, sem importar nada.

Mora aqui, e não no ``__init__.py``, porque o ``client`` precisa dela para o
cabeçalho de identificação e o ``__init__`` importa o ``client``: importar de
volta fecharia um ciclo. O ``release-sdks.sh`` bumpa este arquivo, e o
``tests/test_version.py`` trava a igualdade com o ``pyproject.toml``.
"""

__version__ = "0.8.1"
