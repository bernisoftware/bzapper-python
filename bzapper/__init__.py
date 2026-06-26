"""bZapper — official Python SDK for the bZapper WhatsApp gateway API.

Quickstart:
    >>> from bzapper import Client
    >>> client = Client("http://localhost:8080", "bz_live_...")
    >>> client.send_text("+5511999999999", "Hello from bZapper!")
"""

from .client import Client
from .errors import BzapperError
from .webhooks import (
    Webhooks,
    WebhookEvent,
    SignatureError,
    verify as verify_webhook,
    construct_event as construct_webhook_event,
)

__all__ = [
    "Client",
    "BzapperError",
    "Webhooks",
    "WebhookEvent",
    "SignatureError",
    "verify_webhook",
    "construct_webhook_event",
]
__version__ = "0.3.0"
