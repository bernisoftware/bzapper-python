"""Webhook receiver for bZapper.

Receives the raw request body, **verifies the HMAC-SHA256 signature**, parses the
envelope into a typed :class:`WebhookEvent` and routes it to handlers registered
per event type. Zero third-party dependencies (stdlib only).

The API signs every delivery with ``X-Bzapper-Signature: sha256=<hex>`` where the
hex is ``HMAC_SHA256(secret, raw_body)``. It also sends ``X-Bzapper-Event-Id`` and
``X-Bzapper-Event-Type``.

Quickstart::

    from bzapper.webhooks import Webhooks

    hooks = Webhooks(secret="whsec_...")  # the webhook's secret (from create_webhook)

    @hooks.on("message.received")
    def _(event):
        print(event.sender.name, event.payload.get("body"))

    # In your HTTP endpoint (Flask/FastAPI/Django — framework-agnostic):
    #   verifies the signature, parses, and dispatches. Raises SignatureError if bad.
    hooks.handle(raw_body=request.get_data(), signature=request.headers["X-Bzapper-Signature"])

Idempotency: each event carries a stable ``event.id`` — store processed ids
(Redis/DB) and skip duplicates; the API may retry deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

__all__ = [
    "Webhooks",
    "WebhookEvent",
    "Group",
    "Sender",
    "SignatureError",
    "verify",
    "construct_event",
    "SIGNATURE_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "EVENT_TYPES",
]

SIGNATURE_HEADER = "X-Bzapper-Signature"
EVENT_ID_HEADER = "X-Bzapper-Event-Id"
EVENT_TYPE_HEADER = "X-Bzapper-Event-Type"

#: All event types the API can deliver (for reference/autocomplete).
EVENT_TYPES = (
    "message.received", "message.sent", "message.delivered", "message.read", "message.failed",
    "instance.connected", "instance.disconnected", "instance.banned", "instance.logged_out",
    "instance.warming", "instance.status",
    "group.joined", "group.participant_added", "group.participant_removed",
    "group.participant_promoted", "group.participant_demoted",
    "group.subject_changed", "group.description_changed",
)

Body = Union[str, bytes, bytearray]


class SignatureError(Exception):
    """Raised when a webhook signature is missing or does not match."""


@dataclass
class Group:
    """WhatsApp group context, when the event happened in a group."""

    jid: Optional[str] = None
    name: Optional[str] = None


@dataclass
class Sender:
    """Who sent/triggered the event (for message/group events)."""

    jid: Optional[str] = None
    lid: Optional[str] = None
    name: Optional[str] = None


@dataclass
class WebhookEvent:
    """A parsed, typed webhook event (the delivered envelope)."""

    id: str
    type: str
    timestamp: Optional[str] = None
    instance_id: Optional[str] = None
    client_reference: Optional[str] = None
    group: Optional[Group] = None
    sender: Optional[Sender] = None
    mentions: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WebhookEvent":
        g = d.get("group")
        s = d.get("sender")
        return cls(
            id=d.get("event_id", ""),
            type=d.get("event_type", ""),
            timestamp=d.get("timestamp"),
            instance_id=d.get("instance_id"),
            client_reference=d.get("client_reference"),
            group=Group(jid=g.get("jid"), name=g.get("name")) if isinstance(g, dict) else None,
            sender=Sender(jid=s.get("jid"), lid=s.get("lid"), name=s.get("name")) if isinstance(s, dict) else None,
            mentions=list(d.get("mentions") or []),
            payload=dict(d.get("payload") or {}),
            raw=d,
        )


def _as_bytes(body: Body) -> bytes:
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def verify(secret: str, body: Body, signature: Optional[str]) -> bool:
    """Return True iff ``signature`` matches the HMAC of the **raw** body.

    Timing-safe. Pass the exact bytes received — never the re-serialized JSON.
    """
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), _as_bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def construct_event(secret: str, body: Body, signature: Optional[str]) -> WebhookEvent:
    """Verify the signature and parse the body into a :class:`WebhookEvent`.

    Raises:
        SignatureError: if the signature is missing or invalid.
    """
    if not verify(secret, body, signature):
        raise SignatureError("invalid webhook signature")
    text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
    return WebhookEvent.from_dict(json.loads(text))


Handler = Callable[[WebhookEvent], None]


class Webhooks:
    """Verifies, parses and routes incoming webhook deliveries to handlers.

    Args:
        secret: The webhook's signing secret (returned once by ``create_webhook``).
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("Webhooks: `secret` is required.")
        self.secret = secret
        self._handlers: Dict[str, List[Handler]] = {}
        self._any: List[Handler] = []

    def on(self, event_type: str, handler: Optional[Handler] = None):
        """Register a handler for an event type. Usable as a decorator.

        ::

            @hooks.on("message.received")
            def _(event): ...

            hooks.on("instance.banned", my_handler)
        """
        def register(h: Handler) -> Handler:
            self._handlers.setdefault(event_type, []).append(h)
            return h

        return register(handler) if handler is not None else register

    def on_any(self, handler: Optional[Handler] = None):
        """Register a handler that runs for **every** event. Usable as a decorator."""
        def register(h: Handler) -> Handler:
            self._any.append(h)
            return h

        return register(handler) if handler is not None else register

    def construct_event(self, body: Body, signature: Optional[str]) -> WebhookEvent:
        """Verify + parse a delivery into a typed event (no dispatch)."""
        return construct_event(self.secret, body, signature)

    def handle(self, raw_body: Body, signature: Optional[str]) -> WebhookEvent:
        """Verify, parse and dispatch a delivery to the matching handlers.

        Returns the parsed event (use ``event.id`` for idempotency). Raises
        :class:`SignatureError` if the signature is invalid — do NOT process.
        """
        event = self.construct_event(raw_body, signature)
        for h in self._handlers.get(event.type, []):
            h(event)
        for h in self._any:
            h(event)
        return event
