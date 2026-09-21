"""bZapper Connect — partner client.

For a PARTNER software that lets its customers subscribe to bZapper Pro and
connect WhatsApp without leaving the partner's product. Authenticates with the
partner secret (``bz_partner_...``), which must live on your **backend** only —
never ship it to a browser.

Flow::

    backend  create_connect_session()  -> session_token
    front    BzapperConnect.open({ session })  -> emits one-time `code`
    backend  exchange_code(code)  -> customer's api_key (bz_live_...)
    backend  Client(api_key).send_text(...)

Once connected, the customer's key answers **402 ``connect_suspended``** while the
customer's Pro is unpaid (it resumes by itself once paid) and **401
``connect_revoked``** after the connection ends. Lifecycle changes arrive on the
partner webhook as ``connect.completed``, ``connect.suspended``,
``connect.resumed`` and ``connect.revoked`` (see :mod:`bzapper.webhooks`).

Zero third-party dependencies: reuses the HTTP/error plumbing of
:class:`bzapper.client.Client`.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, Mapping, Optional

from .client import Client

__all__ = ["PartnerClient", "CONNECTION_STATUSES"]

JSONDict = Dict[str, Any]

#: Todos os status de uma conexão (para referência/autocomplete).
CONNECTION_STATUSES = (
    "pending_account", "pending_payment", "pending_number",
    "active", "suspended", "revoked",
)


class PartnerClient:
    """HTTP client for the bZapper Connect partner API (``/partner/*``).

    Args:
        partner_secret: Partner secret (``bz_partner_...``). Sent as a Bearer
            token. Backend only.
        base_url: Optional API base URL. Defaults to production
            (``https://api.bzapper.com.br``).
        locale: Optional BCP-47 locale sent as ``Accept-Language``.
        timeout: Per-request timeout in seconds (default ``30``).

    Raises:
        BzapperError: On any non-2xx response (branch on ``err.code``).

    Example:
        >>> from bzapper import PartnerClient
        >>> partner = PartnerClient("bz_partner_...")
        >>> s = partner.create_connect_session(
        ...     "customer-42", {"name": "Ana Souza", "email": "ana@boxy.com"}
        ... )
        >>> s["session_token"]
    """

    def __init__(
        self,
        partner_secret: str,
        base_url: Optional[str] = None,
        locale: Optional[str] = None,
        timeout: float = 30,
    ) -> None:
        if not partner_secret:
            raise ValueError(
                "PartnerClient: partner_secret é obrigatório (ex.: 'bz_partner_...')."
            )
        # Composição, não herança: o parceiro não pode herdar os métodos do
        # tenant (send_*, keys…). Reaproveita só o encanamento HTTP/erros do
        # Client — mesmos headers de identificação, mesmo BzapperError.
        self._http = Client(partner_secret, base_url=base_url, locale=locale, timeout=timeout)
        self.base_url = self._http.base_url
        self.locale = locale
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        return self._http._request(method, path, body=body, params=params)

    @staticmethod
    def _id(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    # -- partner ---------------------------------------------------------------

    def me(self) -> JSONDict:
        """Partner identity (who the secret belongs to). ``GET /partner/me``

        Returns ``{id, slug, name, logo_url, allowed_origins, webhook_url,
        key_scopes}``.
        """
        return self._request("GET", "/partner/me")

    # -- connect sessions ------------------------------------------------------

    def create_connect_session(
        self,
        external_id: str,
        customer: Mapping[str, Any],
        locale: Optional[str] = None,
    ) -> JSONDict:
        """Open a Connect session for one of your customers. ``POST /partner/connect-sessions``

        Creates (or reuses) the customer's connection and returns a short-lived
        ``session_token`` (30 min) that opens the embedded component. The
        customer data is trusted (you already authenticated this person): the
        account is created without password, captcha or email confirmation —
        unless the email already has a bZapper account, in which case the
        component asks for a code sent to that email.

        Args:
            external_id: Your id for this customer. Same id = same connection.
            customer: ConnectCustomer dict — ``email`` (required), ``name`` or
                ``company`` (one of them required), ``phone`` (E.164, pre-fills
                the number), ``country`` (ISO-3166 alpha-2, sets the currency)
                and ``locale``.
            locale: Optional component locale (e.g. ``"pt-BR"``).

        Returns:
            ``{session_token, expires_at, connection}``.
        """
        return self._request(
            "POST",
            "/partner/connect-sessions",
            body={"external_id": external_id, "customer": dict(customer), "locale": locale},
        )

    def exchange_code(self, code: str) -> JSONDict:
        """Exchange the completion code for the customer's API key. ``POST /partner/connect/exchange``

        The component emits ``bzapper:complete`` with a one-time ``code`` (valid
        10 min) once the customer paid Pro and connected WhatsApp.

        Returns:
            The connection plus ``api_key`` (``bz_live_...``) — shown only once,
            store it now (use :meth:`rotate_connection_key` if lost).
        """
        return self._request("POST", "/partner/connect/exchange", body={"code": code})

    # -- connections -----------------------------------------------------------

    def list_connections(
        self,
        external_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> JSONDict:
        """List your connections. ``GET /partner/connections``

        Args:
            external_id: Optional filter by your customer id.
            status: Optional filter — ``pending_account``, ``pending_payment``,
                ``pending_number``, ``active``, ``suspended`` or ``revoked``.

        Returns:
            ``{"data": [PartnerConnection, ...]}``.
        """
        return self._request(
            "GET",
            "/partner/connections",
            params={"external_id": external_id, "status": status},
        )

    def get_connection(self, connection_id: str) -> JSONDict:
        """Get one connection (status, account, numbers). ``GET /partner/connections/{id}``"""
        return self._request("GET", f"/partner/connections/{self._id(connection_id)}")

    def rotate_connection_key(self, connection_id: str) -> JSONDict:
        """Issue a new API key for a completed connection. ``POST /partner/connections/{id}/rotate-key``

        The previous key stops working. Returns the connection plus the new
        ``api_key`` (shown once). Raises ``BzapperError`` with code
        ``connection_not_active`` (409) if the connection is not completed yet
        or was revoked.
        """
        return self._request(
            "POST", f"/partner/connections/{self._id(connection_id)}/rotate-key"
        )

    def revoke_connection(self, connection_id: str) -> None:
        """End a connection. ``DELETE /partner/connections/{id}``

        Revokes the key (it then answers 401 ``connect_revoked``) and sends a
        ``connect.revoked`` webhook. Does NOT cancel the customer's plan.
        """
        self._request("DELETE", f"/partner/connections/{self._id(connection_id)}")
