"""bZapper — official Python SDK for the bZapper WhatsApp gateway API.

Quickstart:
    >>> from bzapper import Client
    >>> client = Client("http://localhost:8080", "bz_live_...")
    >>> client.send_text("+5511999999999", "Hello from bZapper!")
"""

from .client import Client
from .errors import BzapperError

__all__ = ["Client", "BzapperError"]
__version__ = "0.2.0"
