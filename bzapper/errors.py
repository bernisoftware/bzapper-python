"""Typed errors raised by the bZapper SDK."""

from __future__ import annotations

from typing import Optional


class BzapperError(Exception):
    """Error raised for any non-2xx response from the bZapper API.

    The API returns a stable, neutral ``code`` plus a human-readable,
    localized ``message``. Always branch on :attr:`code` (stable) and never
    parse :attr:`message` (translated, for humans only).

    Attributes:
        code: Stable, neutral error code (e.g. ``"instance_not_connected"``).
        message: Human-readable, localized message (do not parse).
        status_code: HTTP status code of the response.
        locale: Locale of the returned message, when present.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        locale: Optional[str] = None,
    ) -> None:
        super().__init__(f"[{status_code}] {code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.locale = locale

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"BzapperError(code={self.code!r}, message={self.message!r}, "
            f"status_code={self.status_code!r}, locale={self.locale!r})"
        )
