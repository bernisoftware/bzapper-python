"""bZapper API client.

Zero third-party dependencies: built entirely on the Python standard library
(``urllib``). Idiomatic, fully type-hinted.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import BzapperError

__all__ = ["Client"]

JSONDict = Dict[str, Any]


class Client:
    """HTTP client for the bZapper WhatsApp gateway API.

    Args:
        base_url: API base URL, e.g. ``http://localhost:8080`` in dev or
            ``https://api.bzapper.com.br`` in production.
        api_key: Tenant API key (``bz_live_...``). Sent as a Bearer token.
        locale: Optional BCP-47 locale (e.g. ``"pt-BR"``) sent as
            ``Accept-Language`` so error messages come back translated.
        timeout: Per-request timeout in seconds (default ``30``).

    Example:
        >>> from bzapper import Client
        >>> client = Client("http://localhost:8080", "bz_live_...")
        >>> client.send_text("+5511999999999", "Hello from bZapper!")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        locale: Optional[str] = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.locale = locale
        self.timeout = timeout

    # -- internal HTTP plumbing ------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.locale:
            headers["Accept-Language"] = self.locale
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Perform an HTTP request and return the decoded JSON body.

        Raises:
            BzapperError: On any non-2xx response.
        """
        url = self.base_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        data: Optional[bytes] = None
        if body is not None:
            payload = {k: v for k, v in body.items() if v is not None}
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return self._decode(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            self._raise(exc.code, raw)
        except urllib.error.URLError as exc:  # network/DNS/timeout
            raise BzapperError("network_error", str(exc.reason), 0) from exc

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _raise(status_code: int, raw: bytes) -> "Any":
        payload = Client._decode(raw)
        if isinstance(payload, dict):
            code = str(payload.get("code", "unknown_error"))
            message = str(payload.get("message", "Unknown error"))
            locale = payload.get("locale")
        else:
            code = "unknown_error"
            message = (raw.decode("utf-8", "replace") if raw else "Unknown error")
            locale = None
        raise BzapperError(code, message, status_code, locale)

    @staticmethod
    def _send_base(
        to: str,
        *,
        instance_id: Optional[str],
        pool_id: Optional[str],
        quoted_message_id: Optional[str],
        client_reference: Optional[str],
        mentions: Optional[Sequence[str]],
        sticky: Optional[bool],
    ) -> JSONDict:
        """Build the SendBase fields shared by every message endpoint."""
        return {
            "to": to,
            "instance_id": instance_id,
            "pool_id": pool_id,
            "quoted_message_id": quoted_message_id,
            "client_reference": client_reference,
            "mentions": list(mentions) if mentions is not None else None,
            "sticky": sticky,
        }

    # -- messages --------------------------------------------------------------

    def send_text(
        self,
        to: str,
        body: str,
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a text message.

        Args:
            to: Destination phone in E.164 (``+5511...``) or a JID.
            body: Text content.

        Returns:
            The queued-message object (``message_id``, ``status`` and optional
            ``client_reference``).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["body"] = body
        return self._request("POST", "/messages/text", body=payload)

    def send_otp(
        self,
        to: str,
        code: str,
        *,
        body: Optional[str] = None,
        expiry_minutes: Optional[int] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a verification code (OTP) as two messages.

        Sends the context text and the code on its own bubble, so the recipient
        can copy the code on any device. Counts as a single send. When ``body``
        is omitted, the API generates the text in the account language, with
        variations to reduce blocking. The code is never stored or shown.

        Args:
            to: Destination phone in E.164 (``+5511...``) or a JID.
            code: The verification code.
            body: Optional context text. Empty → generated by the API.
            expiry_minutes: Optional — mentions the expiry in the generated text.
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["code"] = code
        if body is not None:
            payload["body"] = body
        if expiry_minutes is not None:
            payload["expiry_minutes"] = expiry_minutes
        return self._request("POST", "/messages/otp", body=payload)

    def send_image(
        self,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send an image. ``media`` is a MediaInput dict (use ``url`` OR ``base64``)."""
        return self._send_media(
            "/messages/image",
            to,
            media,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )

    def send_video(
        self,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a video. ``media`` is a MediaInput dict (use ``url`` OR ``base64``)."""
        return self._send_media(
            "/messages/video",
            to,
            media,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )

    def send_document(
        self,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a document. ``media`` is a MediaInput dict (use ``url`` OR ``base64``)."""
        return self._send_media(
            "/messages/document",
            to,
            media,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )

    def send_audio(
        self,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send audio. Set ``media["ptt"] = True`` for a voice note."""
        return self._send_media(
            "/messages/audio",
            to,
            media,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )

    def send_sticker(
        self,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a sticker. ``media`` is a MediaInput dict (use ``url`` OR ``base64``)."""
        return self._send_media(
            "/messages/sticker",
            to,
            media,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )

    def _send_media(
        self,
        path: str,
        to: str,
        media: Mapping[str, Any],
        *,
        instance_id: Optional[str],
        pool_id: Optional[str],
        quoted_message_id: Optional[str],
        client_reference: Optional[str],
        mentions: Optional[Sequence[str]],
        sticky: Optional[bool],
    ) -> JSONDict:
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["media"] = dict(media)
        return self._request("POST", path, body=payload)

    def send_location(
        self,
        to: str,
        latitude: float,
        longitude: float,
        *,
        name: Optional[str] = None,
        address: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a location (latitude/longitude, optional name/address)."""
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["latitude"] = latitude
        payload["longitude"] = longitude
        payload["name"] = name
        payload["address"] = address
        return self._request("POST", "/messages/location", body=payload)

    def send_contact(
        self,
        to: str,
        *,
        contact_name: Optional[str] = None,
        contact_vcard: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a contact card (name and/or raw vCard)."""
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["contact_name"] = contact_name
        payload["contact_vcard"] = contact_vcard
        return self._request("POST", "/messages/contact", body=payload)

    def send_poll(
        self,
        to: str,
        name: str,
        options: Sequence[str],
        *,
        selectable_count: int = 1,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send a poll.

        Args:
            name: Poll question.
            options: Poll options.
            selectable_count: Max number of selectable options (default 1).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["name"] = name
        payload["options"] = list(options)
        payload["selectable_count"] = selectable_count
        return self._request("POST", "/messages/poll", body=payload)

    def send_reaction(
        self,
        to: str,
        quoted_message_id: str,
        emoji: str,
        *,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """React to a message.

        Args:
            quoted_message_id: ``wa_message_id`` of the target message (required).
            emoji: Reaction emoji (empty string removes the reaction).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["emoji"] = emoji
        return self._request("POST", "/messages/reaction", body=payload)

    def send_buttons(
        self,
        to: str,
        body: str,
        buttons: Sequence[Mapping[str, Any]],
        *,
        footer: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send interactive buttons.

        Args:
            body: Message body.
            buttons: List of ``{"id"?: str, "title": str}`` dicts.
            footer: Optional footer text.

        Note:
            Buttons are unreliable on WhatsApp (worse in groups); the API
            always also sends an equivalent numbered text menu as a fallback.
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["body"] = body
        payload["footer"] = footer
        payload["buttons"] = [dict(b) for b in buttons]
        return self._request("POST", "/messages/buttons", body=payload)

    def send_list(
        self,
        to: str,
        body: str,
        sections: Sequence[Mapping[str, Any]],
        *,
        footer: Optional[str] = None,
        button_text: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
    ) -> JSONDict:
        """Send an interactive list.

        Args:
            body: Message body.
            sections: List of ``{"title"?: str, "rows": [{"id"?, "title",
                "description"?}]}`` dicts.
            footer: Optional footer text.
            button_text: Optional label for the list-open button.

        Note:
            Lists fall back to a numbered text menu on WhatsApp (see buttons).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
        )
        payload["body"] = body
        payload["footer"] = footer
        payload["button_text"] = button_text
        payload["sections"] = [dict(s) for s in sections]
        return self._request("POST", "/messages/list", body=payload)

    # -- instances -------------------------------------------------------------

    def list_instances(self) -> JSONDict:
        """List the tenant's instances (numbers)."""
        return self._request("GET", "/instances")

    def create_instance(
        self,
        phone: str,
        *,
        nickname: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> JSONDict:
        """Create an instance (number).

        Args:
            phone: Phone in ``+DDI...`` format (e.g. ``+5511999999999``).
            nickname: Optional human label.
            proxy_url: Optional per-instance proxy URL (anti-ban / IP isolation).
        """
        body = {"phone": phone, "nickname": nickname, "proxy_url": proxy_url}
        return self._request("POST", "/instances", body=body)

    def get_instance(self, instance_id: str) -> JSONDict:
        """Fetch a single instance by ID."""
        return self._request("GET", f"/instances/{instance_id}")

    def connect_instance(
        self, instance_id: str, *, method: str = "qr"
    ) -> JSONDict:
        """Connect an instance via QR or pairing code.

        Args:
            method: ``"qr"`` (default) returns a QR; ``"code"`` returns an
                8-character pairing code.

        Returns:
            ``{"status", "qr_code"?, "pair_code"?}``.
        """
        return self._request(
            "POST",
            f"/instances/{instance_id}/connect",
            params={"method": method},
        )

    def disconnect_instance(self, instance_id: str) -> None:
        """Disconnect an instance (reconnectable)."""
        return self._request("POST", f"/instances/{instance_id}/disconnect")

    # -- API keys --------------------------------------------------------------

    def list_keys(self) -> JSONDict:
        """List the tenant's API keys (raw key not included)."""
        return self._request("GET", "/keys")

    def create_key(self, name: str, role: str) -> JSONDict:
        """Create a tenant API key.

        Args:
            name: Human label for the key.
            role: ``"admin"`` or ``"agent"``.

        Returns:
            ``{"api_key", "key"}`` — the raw ``api_key`` is shown only once.
        """
        return self._request("POST", "/keys", body={"name": name, "role": role})

    def revoke_key(self, key_id: str) -> None:
        """Revoke a tenant API key by ID."""
        return self._request("DELETE", f"/keys/{key_id}")

    # -- webhooks (management; to RECEIVE+process events use bzapper.webhooks) --

    def list_webhooks(self) -> JSONDict:
        """List the project's webhooks. ``GET /webhooks``"""
        return self._request("GET", "/webhooks")

    def create_webhook(
        self,
        url: str,
        *,
        secret: Optional[str] = None,
        event_types: Optional[Sequence[str]] = None,
        number_filter: Optional[str] = None,
    ) -> JSONDict:
        """Create a webhook. ``POST /webhooks``

        Args:
            url: HTTPS endpoint that will receive the deliveries.
            secret: Omit to let the API generate a strong one (returned ONCE in
                ``secret``). Use it with :class:`bzapper.webhooks.Webhooks`.
            event_types: Subscribed events; empty/None = all. Each event can
                belong to a single webhook (409 on conflict).
            number_filter: ``instance_id`` to restrict to one number.
        """
        return self._request(
            "POST",
            "/webhooks",
            body={"url": url, "secret": secret, "event_types": event_types, "number_filter": number_filter},
        )

    def update_webhook(
        self,
        webhook_id: str,
        *,
        url: Optional[str] = None,
        secret: Optional[str] = None,
        event_types: Optional[Sequence[str]] = None,
        number_filter: Optional[str] = None,
        active: Optional[bool] = None,
    ) -> JSONDict:
        """Update/pause a webhook. ``secret="regenerate"`` rotates it. ``PATCH /webhooks/{id}``"""
        return self._request(
            "PATCH",
            f"/webhooks/{webhook_id}",
            body={"url": url, "secret": secret, "event_types": event_types, "number_filter": number_filter, "active": active},
        )

    def delete_webhook(self, webhook_id: str) -> None:
        """Delete a webhook. ``DELETE /webhooks/{id}``"""
        return self._request("DELETE", f"/webhooks/{webhook_id}")

    def test_webhook(self, webhook_id: str, event_type: Optional[str] = None) -> JSONDict:
        """Send a test event and return the endpoint's HTTP status. ``POST /webhooks/{id}/test``"""
        return self._request("POST", f"/webhooks/{webhook_id}/test", body={"event_type": event_type})

    def webhook_deliveries(self, webhook_id: str, *, limit: Optional[int] = None) -> JSONDict:
        """Recent delivery attempts for a webhook. ``GET /webhooks/{id}/deliveries``"""
        return self._request("GET", f"/webhooks/{webhook_id}/deliveries", params={"limit": limit})

    # -- usage -----------------------------------------------------------------

    def get_usage(
        self, *, from_: Optional[str] = None, to: Optional[str] = None
    ) -> JSONDict:
        """Get a usage summary for the tenant.

        Args:
            from_: Start of window, RFC3339 (e.g. ``2026-06-01T00:00:00Z``).
            to: End of window, RFC3339.
        """
        return self._request("GET", "/usage", params={"from": from_, "to": to})

    # -- presence (works in groups!) ------------------------------------------

    def presence_chat(
        self, instance_id: str, to: str, state: str
    ) -> JSONDict:
        """Send a chat-presence update (typing indicator).

        Args:
            instance_id: Instance to act on (sent in the body).
            to: Destination phone (E.164) or JID — may be a **group** JID.
            state: ``"typing"``, ``"recording"`` or ``"paused"``.
        """
        body = {"instance_id": instance_id, "to": to, "state": state}
        return self._request("POST", "/presence/chat", body=body)

    # -- conversations ---------------------------------------------------------

    def list_conversations(self, instance_id: str) -> JSONDict:
        """List conversations (chats) for an instance."""
        return self._request(
            "GET", "/conversations", params={"instance_id": instance_id}
        )

    def conversation_history(
        self,
        jid: str,
        instance_id: str,
        *,
        before: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> JSONDict:
        """Fetch message history for a conversation.

        Args:
            jid: Conversation JID (path parameter).
            instance_id: Instance to act on (query parameter).
            before: Only messages before this RFC3339 timestamp.
            limit: Max number of messages (server caps at 200).
        """
        return self._request(
            "GET",
            f"/conversations/{jid}/messages",
            params={"instance_id": instance_id, "before": before, "limit": limit},
        )

    # -- chats -----------------------------------------------------------------

    def archive_chat(self, jid: str, instance_id: str, on: bool) -> JSONDict:
        """Archive (``on=True``) or unarchive (``on=False``) a chat."""
        body = {"instance_id": instance_id, "on": on}
        return self._request("POST", f"/chats/{jid}/archive", body=body)

    def pin_chat(self, jid: str, instance_id: str, on: bool) -> JSONDict:
        """Pin (``on=True``) or unpin (``on=False``) a chat."""
        body = {"instance_id": instance_id, "on": on}
        return self._request("POST", f"/chats/{jid}/pin", body=body)

    def mark_chat(self, jid: str, instance_id: str, on: bool) -> JSONDict:
        """Mark a chat as read (``on=True``) or unread (``on=False``)."""
        body = {"instance_id": instance_id, "on": on}
        return self._request("POST", f"/chats/{jid}/read", body=body)

    # -- groups ----------------------------------------------------------------

    def list_groups(self, instance_id: str) -> JSONDict:
        """List the groups the instance belongs to."""
        return self._request(
            "GET", "/groups", params={"instance_id": instance_id}
        )

    def create_group(
        self, instance_id: str, name: str, participants: Sequence[str]
    ) -> JSONDict:
        """Create a group.

        Args:
            instance_id: Instance to act on (query parameter).
            name: Group subject/name.
            participants: Phones (E.164) or JIDs of the initial members.
        """
        body = {"name": name, "participants": list(participants)}
        return self._request(
            "POST", "/groups", body=body, params={"instance_id": instance_id}
        )

    def get_group(self, jid: str, instance_id: str) -> JSONDict:
        """Fetch a single group by JID."""
        return self._request(
            "GET", f"/groups/{jid}", params={"instance_id": instance_id}
        )

    def join_group(self, instance_id: str, code: str) -> JSONDict:
        """Join a group via its invite code."""
        return self._request(
            "POST",
            "/groups/join",
            body={"code": code},
            params={"instance_id": instance_id},
        )

    def update_group_participants(
        self,
        jid: str,
        instance_id: str,
        action: str,
        participants: Sequence[str],
    ) -> JSONDict:
        """Add, remove, promote or demote group participants.

        Args:
            jid: Group JID (path parameter).
            instance_id: Instance to act on (query parameter).
            action: ``"add"``, ``"remove"``, ``"promote"`` or ``"demote"``.
            participants: Phones (E.164) or JIDs to apply the action to.
        """
        body = {"action": action, "participants": list(participants)}
        return self._request(
            "POST",
            f"/groups/{jid}/participants",
            body=body,
            params={"instance_id": instance_id},
        )

    def leave_group(self, jid: str, instance_id: str) -> JSONDict:
        """Leave a group."""
        return self._request(
            "POST", f"/groups/{jid}/leave", params={"instance_id": instance_id}
        )

    def group_invite(self, jid: str, instance_id: str) -> JSONDict:
        """Get a group's invite link/code."""
        return self._request(
            "GET", f"/groups/{jid}/invite", params={"instance_id": instance_id}
        )

    # -- contacts --------------------------------------------------------------

    def contacts_check(
        self, instance_id: str, phones: Sequence[str]
    ) -> JSONDict:
        """Check which phone numbers are registered on WhatsApp.

        Args:
            instance_id: Instance to act on (sent in the body).
            phones: Phones in E.164 to verify.
        """
        body = {"instance_id": instance_id, "phones": list(phones)}
        return self._request("POST", "/contacts/check", body=body)

    # -- profile ---------------------------------------------------------------

    def set_profile(
        self,
        instance_id: str,
        *,
        display_name: Optional[str] = None,
        status_message: Optional[str] = None,
        picture: Optional[str] = None,
    ) -> JSONDict:
        """Update the instance's WhatsApp profile.

        Args:
            instance_id: Instance to update (path parameter).
            display_name: New display name.
            status_message: New "about"/status text.
            picture: New profile picture (URL or base64, per the API).
        """
        body = {
            "display_name": display_name,
            "status_message": status_message,
            "picture": picture,
        }
        return self._request(
            "PATCH", f"/instances/{instance_id}/profile", body=body
        )

    # -- contacts (captured from conversations — shared across the account) ----

    def list_contacts(
        self,
        *,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> JSONDict:
        """List the account's shared contact base (auto-captured from chats).

        Args:
            search: Optional free-text filter (name/phone).
            project_id: Optional project filter — a project id or ``"current"``
                (the project the API key belongs to).
            limit: Optional max number of contacts.
        """
        return self._request(
            "GET",
            "/contacts",
            params={"search": search, "project_id": project_id, "limit": limit},
        )

    # -- projects (numbers, inbox, keys and stats are isolated per project) ----

    def list_projects(self) -> JSONDict:
        """List the account's projects."""
        return self._request("GET", "/projects")

    def create_project(self, name: str) -> JSONDict:
        """Create a project (admin).

        Args:
            name: Project name.
        """
        return self._request("POST", "/projects", body={"name": name})

    # -- brand (numbers' identity — kit lives in the project) ------------------

    def get_brand(self) -> JSONDict:
        """Read the brand identity of the project's numbers."""
        return self._request("GET", "/brand")

    def set_brand(self, profile: Mapping[str, Any]) -> JSONDict:
        """Update the brand identity of the project's numbers.

        Args:
            profile: A BrandProfile dict (``about``, ``display_name``,
                ``logo_url``, ``website``, ``email``, ``phone``, ``address``,
                ``description``).
        """
        return self._request("PUT", "/brand", body=dict(profile))

    def apply_brand(self) -> JSONDict:
        """Apply the "about" text to every connected number of the project."""
        return self._request("POST", "/brand/apply")

    # -- account: users and usage (admin) -------------------------------------

    def list_users(self) -> JSONDict:
        """List the account's users."""
        return self._request("GET", "/users")

    def invite_user(
        self,
        email: str,
        *,
        name: Optional[str] = None,
        role: Optional[str] = None,
    ) -> JSONDict:
        """Invite a user to the account (admin).

        Args:
            email: User email.
            name: Optional display name.
            role: ``"admin"`` (everything) or ``"agent"`` (member — no billing).
        """
        body = {"email": email, "name": name, "role": role}
        return self._request("POST", "/users", body=body)

    def update_user_role(self, user_id: str, role: str) -> JSONDict:
        """Change a user's role (admin).

        Args:
            user_id: Account user id (path parameter).
            role: ``"admin"`` or ``"agent"``.
        """
        path = f"/users/{urllib.parse.quote(user_id, safe='')}"
        return self._request("PATCH", path, body={"role": role})

    def remove_user(self, user_id: str) -> None:
        """Remove a user from the account (admin).

        Args:
            user_id: Account user id (path parameter).
        """
        path = f"/users/{urllib.parse.quote(user_id, safe='')}"
        return self._request("DELETE", path)

    def get_account_usage(
        self, *, from_: Optional[str] = None, to: Optional[str] = None
    ) -> JSONDict:
        """Aggregated account usage plus a per-project breakdown (admin).

        Args:
            from_: Start of window, RFC3339 (e.g. ``2026-06-01T00:00:00Z``).
            to: End of window, RFC3339.
        """
        return self._request(
            "GET", "/account/usage", params={"from": from_, "to": to}
        )
