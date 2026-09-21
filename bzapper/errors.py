"""Typed errors raised by the bZapper SDK.

Every non-2xx response becomes a :class:`BzapperError` — or one of its subclasses,
picked by HTTP status. A connection failure/timeout becomes :class:`NetworkError`
(``status_code == 0``). **Branch on** :attr:`BzapperError.code` (stable), never on the
translated :attr:`~BzapperError.message`.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "BzapperError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "RateLimitError",
    "ServerError",
    "NetworkError",
    "error_for_status",
]


class BzapperError(Exception):
    """Error raised for any non-2xx response from the bZapper API.

    The API returns a stable, neutral ``code`` plus a human-readable,
    localized ``message``. Always branch on :attr:`code` (stable) and never
    parse :attr:`message` (translated, for humans only).

    Attributes:
        code: Stable, neutral error code (e.g. ``"instance_not_connected"``).
            ``body.code``, else ``body.error``, else ``HTTP_<status>`` (non-JSON
            body); ``NETWORK_ERROR`` on connection failures and
            ``INVALID_RESPONSE`` when a 2xx arrives with a non-JSON body.
        message: Human-readable, localized message (do not parse).
        status_code: HTTP status code of the response (``0`` on network errors).
        locale: Locale of the returned message, when present.
        request_id: The response ``X-Request-Id`` header, else the ``X-Request-Id``
            the SDK sent. Always filled — quote it to support.
        retry_after: Seconds from the ``Retry-After`` header (429 only).
        required_scope: Scope the key lacks (``X-Required-Scope`` header, 403 only).
        body: The decoded response body (dict), or the raw text when not JSON.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        locale: Optional[str] = None,
        *,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
        required_scope: Optional[str] = None,
        body: Any = None,
    ) -> None:
        super().__init__(f"[{status_code}] {code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.locale = locale
        self.request_id = request_id
        self.retry_after = retry_after
        self.required_scope = required_scope
        self.body = body

    @property
    def status(self) -> int:
        """Alias of :attr:`status_code` (the name used across the Berni SDKs)."""
        return self.status_code

    def __str__(self) -> str:
        base = f"[{self.status_code}] {self.code}: {self.message}"
        if self.required_scope:
            base += f" (required scope: {self.required_scope})"
        if self.retry_after is not None:
            base += f" (retry after {self.retry_after}s)"
        if self.request_id:
            base += f" [request_id={self.request_id}]"
        return base

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, "
            f"status_code={self.status_code!r}, locale={self.locale!r}, "
            f"request_id={self.request_id!r})"
        )


class AuthenticationError(BzapperError):
    """401 — missing, invalid or revoked key (also ``connect_revoked``)."""


class PermissionDeniedError(BzapperError):
    """403 — the key cannot do this (see ``required_scope``)."""


class NotFoundError(BzapperError):
    """404 — the resource does not exist (or is not in this project)."""


class ConflictError(BzapperError):
    """409 — the current state prevents the operation."""


class ValidationError(BzapperError):
    """400/422 — invalid body or parameter."""


class RateLimitError(BzapperError):
    """429 — rate limited; ``retry_after`` says how long to wait."""


class ServerError(BzapperError):
    """5xx — error on the API side. Quote the ``request_id`` to support."""


class NetworkError(BzapperError):
    """Connection failure or timeout (``status_code == 0``, ``code == "NETWORK_ERROR"``)."""


_BY_STATUS = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_for_status(status: int) -> type:
    """Error class for an HTTP status (5xx → :class:`ServerError`, others → base)."""
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    if 500 <= status <= 599:
        return ServerError
    return BzapperError
