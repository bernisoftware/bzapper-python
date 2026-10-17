"""bZapper API client.

Zero third-party dependencies: built entirely on the Python standard library
(``urllib``). Idiomatic, fully type-hinted.

Transport (padrão Berni Software r2): every logical call carries an
``X-Request-Id`` and — on writes — an ``Idempotency-Key``, both repeated on the
automatic retries (network error/timeout, 429, 502, 503, 504; ``max_retries``
defaults to 2). Errors are typed subclasses of :class:`~bzapper.BzapperError`.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import _http
from ._http import FileInput
from ._version import __version__
from .errors import BzapperError

__all__ = ["Client", "DEFAULT_BASE_URL", "USER_AGENT", "DEFAULT_MAX_RETRIES"]

JSONDict = Dict[str, Any]

#: URL base padrão da API (produção). Sobrescreva só em dev/self-host.
DEFAULT_BASE_URL = "https://api.bzapper.com.br"

#: Identificação enviada em X-Bzapper-Client (e User-Agent).
USER_AGENT = f"bzapper-python/{__version__}"

#: New attempts after the first (0 disables retries).
DEFAULT_MAX_RETRIES = _http.DEFAULT_MAX_RETRIES


class Client:
    """HTTP client for the bZapper WhatsApp gateway API.

    Args:
        api_key: Tenant API key (``bz_live_...``). Sent as a Bearer token. This is
            the only required argument.
        base_url: Optional API base URL. Defaults to production
            (``https://api.bzapper.com.br``); pass ``http://localhost:8080`` only
            for dev or self-host.
        locale: Optional BCP-47 locale (e.g. ``"pt-BR"``) sent as
            ``Accept-Language`` so error messages come back translated.
        timeout: Per-attempt timeout in seconds (default ``30``).
        max_retries: New attempts after the first one on network errors,
            timeouts, 429, 502, 503 and 504 (default ``2``; ``0`` disables).
        project_id: Optional project scope, sent as ``X-Project-Id`` (a project
            key already carries its own).

    Example:
        >>> from bzapper import Client
        >>> client = Client("bz_live_...")  # points at production
        >>> client.send_text("+5511999999999", "Hello from bZapper!")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        locale: Optional[str] = None,
        timeout: float = 30,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        project_id: Optional[str] = None,
    ) -> None:
        # Compatibilidade: a assinatura antiga era ``Client(base_url, api_key)``.
        # Agora a URL é opcional e a API key vem primeiro. Se o 1º argumento
        # parecer uma URL (http...), tratamos como (base_url, api_key) legado.
        if api_key is not None and api_key.startswith(("http://", "https://")):
            api_key, base_url = base_url, api_key
        if not api_key:
            raise ValueError("Client: api_key é obrigatório (ex.: 'bz_live_...').")
        if max_retries < 0:
            raise ValueError("Client: max_retries must be >= 0.")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.locale = locale
        self.timeout = timeout
        self.max_retries = max_retries
        self.project_id = project_id
        #: Wait between attempts. Replaceable (the test-suite never really sleeps).
        self._sleep: Callable[[float], Any] = time.sleep

    # -- internal HTTP plumbing ------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            # Identifica SDK e versão para a API. Serve para avisarmos você
            # quando uma versão que você roda tiver correção que exige update.
            "X-Bzapper-Client": USER_AGENT,
            "User-Agent": USER_AGENT,
        }
        if self.locale:
            headers["Accept-Language"] = self.locale
        if self.project_id:
            headers["X-Project-Id"] = self.project_id
        return headers

    @staticmethod
    def _seg(value: Any, name: str = "id") -> str:
        """One percent-encoded path segment; empty/``.``/``..`` → ``ValueError``."""
        return _http.path_segment(value, name)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        idempotency_key: Optional[str] = None,
        multipart: Optional[Tuple[bytes, str]] = None,
        accept: Optional[str] = None,
        text: bool = False,
    ) -> Any:
        """Perform ONE logical call (1 attempt + up to ``max_retries`` retries).

        ``body`` keys whose value is ``None`` are omitted (never sent). ``headers``
        are merged over the default ones (e.g. ``Idempotency-Key``). ``multipart``
        is a pre-encoded ``(bytes, content_type)`` pair. ``accept`` overrides the
        ``Accept`` header and ``text=True`` returns the 2xx body as a string
        without ever trying to parse JSON (``GET /contacts/export`` → CSV).

        Returns:
            The decoded JSON body, or ``None`` for an empty (e.g. 204) response;
            the raw text when ``text=True``.

        Raises:
            BzapperError: (or a subclass) on any non-2xx response, a network
                failure (:class:`~bzapper.NetworkError`) or a 2xx whose body is
                not JSON (``code == "INVALID_RESPONSE"``).
        """
        method = method.upper()
        url = self.base_url + path
        query = _http.build_query(params)
        if query:
            url = f"{url}?{query}"

        all_headers = self._headers()
        if accept:
            all_headers["Accept"] = accept
        # Mesmo id em todas as tentativas desta chamada: é como o suporte correlaciona.
        all_headers["X-Request-Id"] = uuid.uuid4().hex
        data: Optional[bytes] = None
        if multipart is not None:
            data, all_headers["Content-Type"] = multipart
        elif body is not None:
            payload = {k: v for k, v in body.items() if v is not None}
            data = _http.encode_json(payload)
            all_headers["Content-Type"] = "application/json"
        if headers:
            all_headers.update({k: v for k, v in headers.items() if v is not None})
        if idempotency_key:
            all_headers["Idempotency-Key"] = idempotency_key
        if method in _http.WRITE_METHODS and not any(
            k.lower() == "idempotency-key" for k in all_headers
        ):
            # Mesma chave em todas as tentativas: a API devolve a resposta original
            # (Idempotent-Replayed: true) em vez de executar de novo.
            all_headers["Idempotency-Key"] = str(uuid.uuid4())
        request_id = all_headers["X-Request-Id"]

        attempt = 0
        while True:
            try:
                status, resp_headers, raw = self._send(method, url, all_headers, data)
            except _http.NETWORK_FAILURES as exc:
                if attempt < self.max_retries:
                    self._sleep(_http.backoff(attempt))
                    attempt += 1
                    continue
                raise _http.network_error(self.base_url, exc, request_id) from exc

            if 200 <= status < 300:
                if text:
                    return _http.decode_text(raw)
                return _http.decode_success(status, resp_headers, raw, request_id)
            if status in _http.RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(_http.retry_delay(attempt, _http.header(resp_headers, "Retry-After")))
                attempt += 1
                continue
            raise _http.build_error(status, resp_headers, raw, request_id)

    def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        data: Optional[bytes],
    ) -> Tuple[int, Any, bytes]:
        """One HTTP attempt → ``(status, headers, raw_body)``; raises on transport failure."""
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return getattr(resp, "status", None) or 200, getattr(resp, "headers", None), raw
        except urllib.error.HTTPError as exc:  # non-2xx: a response, not a network failure
            try:
                raw = exc.read()
            except _http.NETWORK_FAILURES:
                raw = b""
            finally:
                exc.close()
            return exc.code, exc.headers, raw

    def _upload(
        self,
        path: str,
        file: FileInput,
        filename: Optional[str],
        content_type: Optional[str],
        fields: Optional[Mapping[str, Any]],
        idempotency_key: Optional[str],
    ) -> Any:
        """``multipart/form-data`` upload (the file goes in the ``file`` part)."""
        content, name, ctype = _http.read_file(file, filename, content_type)
        encoded = _http.encode_multipart(fields, "file", content, name, ctype)
        return self._request("POST", path, multipart=encoded, idempotency_key=idempotency_key)

    @staticmethod
    def _decode(raw: bytes) -> Any:
        payload, _, _ = _http.decode_json(raw)
        return payload

    @staticmethod
    def _raise(status_code: int, raw: bytes) -> "Any":
        raise _http.build_error(status_code, None, raw)

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
        scheduled_at: Optional[str],
        quoted_participant: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
    ) -> JSONDict:
        """Build the SendBase fields shared by every message endpoint."""
        return {
            "to": to,
            "instance_id": instance_id,
            "pool_id": pool_id,
            "quoted_message_id": quoted_message_id,
            "quoted_participant": quoted_participant,
            "client_reference": client_reference,
            "mentions": list(mentions) if mentions is not None else None,
            "sticky": sticky,
            "scheduled_at": scheduled_at,
            "groups": list(groups) if groups is not None else None,
            "tags": list(tags) if tags is not None else None,
            "force": force,
        }

    @staticmethod
    def _idempotency_headers(idempotency_key: Optional[str]) -> Optional[Dict[str, str]]:
        """``Idempotency-Key`` header for a send, or ``None`` when not given."""
        return {"Idempotency-Key": idempotency_key} if idempotency_key else None

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
    ) -> JSONDict:
        """Send a text message.

        Args:
            to: Destination phone in E.164 (``+5511...``) or a JID.
            body: Text content.

        Returns:
            The queued-message object (``message_id``, ``status`` and optional
            ``client_reference``). On the official rail it may also carry
            ``delivery`` (``{"via", "template", "note"}``) — present only when the
            24-hour window was closed and the project fallback template went out
            instead (see :meth:`set_fallback_template`).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["body"] = body
        return self._request(
            "POST", "/messages/text", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["code"] = code
        if body is not None:
            payload["body"] = body
        if expiry_minutes is not None:
            payload["expiry_minutes"] = expiry_minutes
        return self._request(
            "POST", "/messages/otp", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
            idempotency_key=idempotency_key,
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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
            idempotency_key=idempotency_key,
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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
            idempotency_key=idempotency_key,
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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
            idempotency_key=idempotency_key,
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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
            idempotency_key=idempotency_key,
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
        scheduled_at: Optional[str],
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
    ) -> JSONDict:
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["media"] = dict(media)
        return self._request(
            "POST", path, body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["latitude"] = latitude
        payload["longitude"] = longitude
        payload["name"] = name
        payload["address"] = address
        return self._request(
            "POST", "/messages/location", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

    def send_contact(
        self,
        to: str,
        *,
        contact_name: Optional[str] = None,
        contact_phone: Optional[str] = None,
        contact_vcard: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
    ) -> JSONDict:
        """Send a contact card: ready ``contact_vcard``, or ``contact_name`` + ``contact_phone``.

        ``contact_phone`` is the phone of the person being SHARED (E.164), never
        the destination.
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["contact_name"] = contact_name
        payload["contact_phone"] = contact_phone
        payload["contact_vcard"] = contact_vcard
        return self._request(
            "POST", "/messages/contact", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["name"] = name
        payload["options"] = list(options)
        payload["selectable_count"] = selectable_count
        return self._request(
            "POST", "/messages/poll", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["emoji"] = emoji
        return self._request(
            "POST", "/messages/reaction", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["body"] = body
        payload["footer"] = footer
        payload["buttons"] = [dict(b) for b in buttons]
        return self._request(
            "POST", "/messages/buttons", body=payload, headers=self._idempotency_headers(idempotency_key)
        )

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
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
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
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["body"] = body
        payload["footer"] = footer
        payload["button_text"] = button_text
        payload["sections"] = [dict(s) for s in sections]
        return self._request(
            "POST", "/messages/list", body=payload, headers=self._idempotency_headers(idempotency_key)
        )


    # -- advanced messaging (edit / revoke / forward / read receipts) ----------

    def edit_message(
        self, message_id: str, text: str, *, idempotency_key: Optional[str] = None
    ) -> Any:
        """Edit the text of a sent message. ``PATCH /messages/{id}``

        Args:
            message_id: bZapper ``message_id`` returned by the send.
            text: The new text.
        """
        return self._request(
            "PATCH",
            f"/messages/{self._seg(message_id, 'message_id')}",
            body={"text": text},
            idempotency_key=idempotency_key,
        )

    def revoke_message(
        self,
        message_id: str,
        *,
        for_everyone: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Revoke (delete) a sent message. ``DELETE /messages/{id}``

        Args:
            message_id: bZapper ``message_id`` returned by the send.
            for_everyone: Delete for everyone (not only for you).
        """
        return self._request(
            "DELETE",
            f"/messages/{self._seg(message_id, 'message_id')}",
            params={"for_everyone": for_everyone},
            idempotency_key=idempotency_key,
        )

    def forward_message(
        self,
        instance_id: str,
        to: str,
        from_chat: str,
        wa_message_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Forward a message to another chat (experimental). ``POST /messages/forward``

        Args:
            instance_id: Number that forwards.
            to: Destination phone (E.164) or JID.
            from_chat: JID of the chat where the original message is.
            wa_message_id: WhatsApp id of the original message.
        """
        body = {
            "instance_id": instance_id,
            "to": to,
            "from_chat": from_chat,
            "wa_message_id": wa_message_id,
        }
        return self._request(
            "POST", "/messages/forward", body=body, idempotency_key=idempotency_key
        )

    def mark_read(
        self,
        message_id: str,
        instance_id: str,
        chat: str,
        *,
        wa_message_ids: Optional[Sequence[str]] = None,
        sender: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Send read receipts (blue ticks). ``POST /messages/{id}/read``

        Args:
            message_id: WhatsApp id of the (last) message read.
            instance_id: Number that read the messages.
            chat: Chat JID.
            wa_message_ids: Extra WhatsApp message ids to mark in the same call.
            sender: Author JID (required by WhatsApp inside groups).
        """
        body = {
            "instance_id": instance_id,
            "chat": chat,
            "wa_message_ids": list(wa_message_ids) if wa_message_ids is not None else None,
            "sender": sender,
        }
        return self._request(
            "POST",
            f"/messages/{self._seg(message_id, 'message_id')}/read",
            body=body,
            idempotency_key=idempotency_key,
        )

    # -- instances -------------------------------------------------------------

    # -- scheduled sends -------------------------------------------------------

    def list_scheduled(self, *, limit: Optional[int] = None) -> JSONDict:
        """List pending/recent scheduled sends. ``GET /messages/scheduled``"""
        return self._request("GET", "/messages/scheduled", params={"limit": limit})

    def cancel_scheduled(
        self, scheduled_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Cancel a pending scheduled send. ``DELETE /messages/scheduled/{id}``"""
        self._request(
            "DELETE",
            f"/messages/scheduled/{self._seg(scheduled_id, 'scheduled_id')}",
            idempotency_key=idempotency_key,
        )

    # -- campaigns (Pro + campaigns add-on) ------------------------------------

    def create_campaign(
        self,
        variations: Sequence[JSONDict],
        *,
        name: Optional[str] = None,
        pool_id: Optional[str] = None,
        pacing_profile: Optional[str] = None,
        start_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a campaign with template variations. ``POST /campaigns``

        Requires the Pro plan and the Campaigns add-on. Each variation body accepts
        ``{variables}`` and spintax ``{a|b|c}``.

        On the OFFICIAL rail each variation is an approved template instead of a
        body — free text only reaches people who wrote in the last 24h::

            {"body": "", "template": {"name": "promo", "language": "pt_BR",
                                      "variables": ["{nome}", "OUTUBRO10"]}}

        The template must exist, be approved and receive as many ``variables`` as
        its body expects; each value accepts ``{column}`` from the recipient list.
        """
        body: JSONDict = {"variations": list(variations)}
        if name is not None:
            body["name"] = name
        if pool_id is not None:
            body["pool_id"] = pool_id
        if pacing_profile is not None:
            body["pacing_profile"] = pacing_profile
        if start_at is not None:
            body["start_at"] = start_at
        return self._request("POST", "/campaigns", body=body, idempotency_key=idempotency_key)

    def list_campaigns(self, *, limit: Optional[int] = None) -> JSONDict:
        """List the project's campaigns. ``GET /campaigns``"""
        return self._request("GET", "/campaigns", params={"limit": limit})

    def get_campaign(self, campaign_id: str) -> JSONDict:
        """Get a campaign with stats. ``GET /campaigns/{id}``"""
        return self._request("GET", f"/campaigns/{self._seg(campaign_id, 'campaign_id')}")

    def update_campaign(
        self,
        campaign_id: str,
        *,
        name: Optional[str] = None,
        pool_id: Optional[str] = None,
        pacing_profile: Optional[str] = None,
        start_at: Optional[str] = None,
        variations: Optional[Sequence[JSONDict]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Update a campaign. ``PATCH /campaigns/{id}``

        Only the provided fields are changed. Allowed while the campaign is still
        editable (draft/scheduled).
        """
        body: JSONDict = {}
        if name is not None:
            body["name"] = name
        if pool_id is not None:
            body["pool_id"] = pool_id
        if pacing_profile is not None:
            body["pacing_profile"] = pacing_profile
        if start_at is not None:
            body["start_at"] = start_at
        if variations is not None:
            body["variations"] = list(variations)
        return self._request(
            "PATCH",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}",
            body=body,
            idempotency_key=idempotency_key,
        )

    def estimate_campaign(
        self,
        *,
        recipients: Optional[int] = None,
        pacing: Optional[str] = None,
        pool_id: Optional[str] = None,
    ) -> JSONDict:
        """Live send estimate for a recipient count + pacing. ``GET /campaigns/estimate``

        No campaign needed. Returns ``{recipients, numbers_available,
        estimated_seconds, estimated_human}``.
        """
        return self._request(
            "GET",
            "/campaigns/estimate",
            params={"recipients": recipients, "pacing": pacing, "pool_id": pool_id},
        )

    def get_campaign_eligibility(self, *, pool_id: Optional[str] = None) -> JSONDict:
        """Per-number campaign eligibility (connection and age). ``GET /campaigns/eligibility``

        Args:
            pool_id: Optional — only the numbers of this pool.
        """
        return self._request("GET", "/campaigns/eligibility", params={"pool_id": pool_id})

    def upload_campaign_media(
        self,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Upload a campaign header image (multipart). ``POST /campaigns/media``

        Args:
            file: Raw bytes, a path on disk or a binary file object.
            filename: File name sent in the multipart part (default: the path's
                basename, or ``"file"``).
            content_type: MIME type (default: guessed from the file name).

        Returns:
            ``{"url"}`` — use it in the variation's media.
        """
        return self._upload(
            "/campaigns/media", file, filename, content_type, None, idempotency_key
        )

    def add_campaign_recipients(
        self,
        campaign_id: str,
        *,
        recipients: Optional[Sequence[JSONDict]] = None,
        contacts: Optional[JSONDict] = None,
        contact_ids: Optional[Sequence[str]] = None,
        contact_filter: Optional[JSONDict] = None,
        replace: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Add recipients. ``POST /campaigns/{id}/recipients``

        Combine any of: ``recipients`` (list of ``{"phone", "payload"}``),
        ``contacts`` (mapping phone -> payload), ``contact_ids`` (explicit contact
        ids) and/or ``contact_filter`` (a dict with any of ``search``, ``tags``,
        ``tags_all``, ``groups``, ``city``, ``state``, ``country``, ``has_email``).
        For contact_ids/contact_filter phones are resolved server-side and
        restricted to ACTIVE contacts. Pass ``replace=True`` to replace the whole
        recipient list instead of appending (draft/scheduled only).
        """
        body: JSONDict = {}
        if recipients is not None:
            body["recipients"] = list(recipients)
        if contacts is not None:
            body["contacts"] = contacts
        if contact_ids is not None:
            body["contact_ids"] = list(contact_ids)
        if contact_filter is not None:
            body["contact_filter"] = contact_filter
        if replace is not None:
            body["replace"] = replace
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/recipients",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_campaign_recipients(
        self, campaign_id: str, *, limit: Optional[int] = None
    ) -> JSONDict:
        """List recipients. ``GET /campaigns/{id}/recipients``

        Each item includes per-recipient delivery state: ``contact_name``
        (resolved from the contact base), ``status`` (pending/claimed/sent/
        failed/suppressed), ``delivery`` (''/sent/delivered/read from WhatsApp
        receipts), ``message_id`` and ``last_error``.
        """
        return self._request(
            "GET",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/recipients",
            params={"limit": limit},
        )

    def start_campaign(
        self, campaign_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Start (or schedule) the campaign. ``POST /campaigns/{id}/start``"""
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/start",
            idempotency_key=idempotency_key,
        )

    def pause_campaign(
        self, campaign_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Pause the campaign. ``POST /campaigns/{id}/pause``"""
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/pause",
            idempotency_key=idempotency_key,
        )

    def resume_campaign(
        self, campaign_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Resume the campaign. ``POST /campaigns/{id}/resume``"""
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/resume",
            idempotency_key=idempotency_key,
        )

    def cancel_campaign(
        self, campaign_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Cancel the campaign. ``POST /campaigns/{id}/cancel``"""
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/cancel",
            idempotency_key=idempotency_key,
        )

    def dry_run_campaign(
        self, campaign_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Simulate without sending (numbers, duration, warnings). ``POST /campaigns/{id}/dry-run``"""
        return self._request(
            "POST",
            f"/campaigns/{self._seg(campaign_id, 'campaign_id')}/dry-run",
            idempotency_key=idempotency_key,
        )

    # -- pools (number redundancy) ---------------------------------------------

    def list_pools(self) -> JSONDict:
        """List the tenant's number pools. ``GET /pools``"""
        return self._request("GET", "/pools")

    def create_pool(
        self,
        *,
        name: Optional[str] = None,
        strategy: Optional[str] = None,
        is_default: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a number pool. ``POST /pools``

        Args:
            name: Pool name.
            strategy: ``"round_robin"``, ``"least_used"`` or ``"health_weighted"``.
            is_default: Make it the default pool of the project.
        """
        body = {"name": name, "strategy": strategy, "is_default": is_default}
        return self._request("POST", "/pools", body=body, idempotency_key=idempotency_key)

    def get_pool(self, pool_id: str) -> JSONDict:
        """Get a pool with its members. ``GET /pools/{id}``"""
        return self._request("GET", f"/pools/{self._seg(pool_id, 'pool_id')}")

    def add_pool_number(
        self, pool_id: str, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Add a number (instance) to the pool. ``POST /pools/{id}/numbers``"""
        return self._request(
            "POST",
            f"/pools/{self._seg(pool_id, 'pool_id')}/numbers",
            body={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    # -- instances (numbers) ---------------------------------------------------

    def list_instances(
        self,
        *,
        project_id: Optional[str] = None,
        archived: Optional[Union[bool, str]] = None,
    ) -> JSONDict:
        """List the tenant's instances (numbers).

        Args:
            project_id: Optional project scope — a project id, or ``"all"`` for
                every number in the account. Omit to use the active project
                (the ``X-Project-Id`` header / the project bound to your key).
            archived: ``True`` (or ``"1"``) lists the ARCHIVED numbers instead of
                the active ones.
        """
        if archived is True:
            archived = "1"
        elif archived is False:
            archived = None
        return self._request(
            "GET", "/instances", params={"project_id": project_id, "archived": archived}
        )

    def create_instance(
        self,
        phone: str,
        *,
        nickname: Optional[str] = None,
        proxy_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create an instance (number).

        Args:
            phone: Phone in ``+DDI...`` format (e.g. ``+5511999999999``).
            nickname: Optional human label.
            proxy_url: Optional per-instance proxy URL (network configuration).
        """
        body = {"phone": phone, "nickname": nickname, "proxy_url": proxy_url}
        return self._request("POST", "/instances", body=body, idempotency_key=idempotency_key)

    def get_instance(self, instance_id: str) -> JSONDict:
        """Fetch a single instance by ID."""
        return self._request("GET", f"/instances/{self._seg(instance_id, 'instance_id')}")

    def delete_instance(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Remove an instance (ends the WhatsApp session). ``DELETE /instances/{id}``"""
        return self._request(
            "DELETE",
            f"/instances/{self._seg(instance_id, 'instance_id')}",
            idempotency_key=idempotency_key,
        )

    def connect_instance(
        self,
        instance_id: str,
        *,
        method: str = "qr",
        idempotency_key: Optional[str] = None,
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
            f"/instances/{self._seg(instance_id, 'instance_id')}/connect",
            params={"method": method},
            idempotency_key=idempotency_key,
        )

    def disconnect_instance(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Disconnect an instance (reconnectable)."""
        return self._request(
            "POST",
            f"/instances/{self._seg(instance_id, 'instance_id')}/disconnect",
            idempotency_key=idempotency_key,
        )

    def logout_instance(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Log the number out (a new QR is needed afterwards). ``POST /instances/{id}/logout``"""
        return self._request(
            "POST",
            f"/instances/{self._seg(instance_id, 'instance_id')}/logout",
            idempotency_key=idempotency_key,
        )

    def clear_instance_session(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Wipe the paired device credential, forcing a clean re-pairing.

        Use it when :meth:`connect_instance` will not produce a QR code, or when
        pairing is stuck in an inconsistent state: a plain logout only drops the
        reference and leaves the old device behind.

        Destructive and irreversible -- the number goes offline and must be
        paired again by scanning a QR code. Idempotent and safe to retry.
        """
        return self._request(
            "POST",
            f"/instances/{self._seg(instance_id, 'instance_id')}/clear-session",
            idempotency_key=idempotency_key,
        )

    def archive_instance(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Archive (deactivate) a number, keeping its history. ``POST /instances/{id}/archive``"""
        return self._request(
            "POST",
            f"/instances/{self._seg(instance_id, 'instance_id')}/archive",
            idempotency_key=idempotency_key,
        )

    def unarchive_instance(
        self, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Reactivate an archived number (it comes back disconnected). ``POST /instances/{id}/unarchive``"""
        return self._request(
            "POST",
            f"/instances/{self._seg(instance_id, 'instance_id')}/unarchive",
            idempotency_key=idempotency_key,
        )

    def set_instance_proxy(
        self, instance_id: str, proxy_url: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Set (or clear, with ``""``) the number's proxy. ``PATCH /instances/{id}/proxy``"""
        return self._request(
            "PATCH",
            f"/instances/{self._seg(instance_id, 'instance_id')}/proxy",
            body={"proxy_url": proxy_url},
            idempotency_key=idempotency_key,
        )

    def set_inbound_filters(
        self,
        instance_id: str,
        *,
        ignore_broadcast: Optional[bool] = None,
        ignore_status: Optional[bool] = None,
        ignore_groups: Optional[bool] = None,
        group_allowlist: Optional[Sequence[str]] = None,
        group_denylist: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Set the number's inbound filters. ``PATCH /instances/{id}/inbound-filters``

        Only the given fields change. ``group_allowlist``/``group_denylist`` are
        group JIDs.
        """
        body = {
            "ignore_broadcast": ignore_broadcast,
            "ignore_status": ignore_status,
            "ignore_groups": ignore_groups,
            "group_allowlist": list(group_allowlist) if group_allowlist is not None else None,
            "group_denylist": list(group_denylist) if group_denylist is not None else None,
        }
        return self._request(
            "PATCH",
            f"/instances/{self._seg(instance_id, 'instance_id')}/inbound-filters",
            body=body,
            idempotency_key=idempotency_key,
        )

    def set_privacy(
        self,
        instance_id: str,
        setting: str,
        value: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Set one WhatsApp privacy setting of the number. ``PATCH /instances/{id}/privacy``

        Args:
            setting: e.g. ``"last"``, ``"online"``, ``"profile"``, ``"status"``,
                ``"readreceipts"``, ``"groupadd"``.
            value: e.g. ``"all"``, ``"contacts"``, ``"none"``.
        """
        return self._request(
            "PATCH",
            f"/instances/{self._seg(instance_id, 'instance_id')}/privacy",
            body={"setting": setting, "value": value},
            idempotency_key=idempotency_key,
        )

    # -- official WhatsApp Business (Cloud API) account -----------------------

    def get_official_account(self) -> JSONDict:
        """The WhatsApp Business (Cloud API) account of the project. ``GET /official/account``"""
        return self._request("GET", "/official/account")

    def connect_official_account(
        self,
        waba_id: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
        *,
        display_number: Optional[str] = None,
        verified_name: Optional[str] = None,
        status: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Connect a WhatsApp Business account with manual credentials. ``POST /official/account``

        Only for projects created with ``api_mode="OFFICIAL"``. Calling it again
        replaces the credentials — that is the reconnection path when a token is
        revoked; it never creates a second account for the same project.

        Args:
            waba_id: The WhatsApp Business Account id.
            phone_number_id: Optional — discovered from the WABA when omitted. If
                the WABA holds more than one number the API answers 409
                ``multiple_phone_numbers`` with the list (in ``error.body``), so
                the choice stays with you instead of being guessed.
            access_token: The WABA token (required; stored encrypted at rest). A
                token from Meta's "API Setup" screen expires in 24h — for a
                lasting connection use a system user token.
            status: ``PENDENTE`` (default), ``AGUARDANDO_PAGAMENTO`` or ``ATIVA``.

        Raises:
            ValueError: ``access_token`` missing (before any request).
        """
        if not access_token:
            raise ValueError("connect_official_account: access_token é obrigatório.")
        body = {
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "access_token": access_token,
            "display_number": display_number,
            "verified_name": verified_name,
            "status": status,
        }
        return self._request(
            "POST", "/official/account", body=body, idempotency_key=idempotency_key
        )

    def disconnect_official_account(self, *, idempotency_key: Optional[str] = None) -> None:
        """Disconnect the project's WhatsApp Business account. ``DELETE /official/account``"""
        return self._request("DELETE", "/official/account", idempotency_key=idempotency_key)

    def get_official_signup_config(self) -> JSONDict:
        """What the browser needs to open Meta's Embedded Signup. ``GET /official/signup-config``

        Returns:
            ``{"enabled", "app_id", "config_id", "graph_version"}`` — public by
            nature (the customer's browser sees them; the app secret never leaves
            the server). ``enabled`` false means this installation has no
            Embedded Signup: offer the manual path (:meth:`connect_official_account`).
        """
        return self._request("GET", "/official/signup-config")

    def complete_official_signup(
        self,
        code: str,
        waba_id: str,
        *,
        phone_number_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Finish the connection with the code returned by Meta's popup. ``POST /official/embedded-signup``

        Exchanges the one-time ``code`` for a lasting token, subscribes the app
        to the WABA's webhooks and registers the number on the Cloud API. The
        code expires about 30 seconds after the popup closes — send it straight
        through.

        Args:
            code: One-time code from the Embedded Signup callback.
            waba_id: The WABA id the popup returned.
            phone_number_id: Optional — discovered from the WABA when omitted.

        Returns:
            ``{"account", "warning"}``. A non-fatal step that failed comes back in
            ``warning`` (``webhooks_not_subscribed`` / ``phone_not_registered``)
            instead of losing the whole connection.
        """
        body = {"code": code, "waba_id": waba_id, "phone_number_id": phone_number_id}
        return self._request(
            "POST", "/official/embedded-signup", body=body, idempotency_key=idempotency_key
        )

    def get_official_status(self) -> JSONDict:
        """What is ready and what is missing, checked live against Meta. ``GET /official/status``

        ``can_send`` is the only question that matters. ``live`` false means Meta
        could not be reached and the values are the last known ones — the
        difference between "this is how it is" and "we could not check".
        """
        return self._request("GET", "/official/status")

    # -- official numbers ------------------------------------------------------

    def upload_official_media(
        self,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        instance_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Upload a file to Meta once and reuse it as ``media.id``. ``POST /official/media``

        Goes through the default number (or ``instance_id``); the id is valid for
        30 days and only for sends from that same number — the same image for a
        whole campaign without uploading it per message.
        """
        content, name, ctype = _http.read_file(file, filename, content_type)
        encoded = _http.encode_multipart(None, "file", content, name, ctype)
        return self._request(
            "POST", "/official/media", params={"instance_id": instance_id},
            multipart=encoded, idempotency_key=idempotency_key,
        )

    def get_official_media(self, media_id: str, instance_id: Optional[str] = None) -> JSONDict:
        """Check that a media id is still valid (404 ``media_not_found`` once expired).

        ``GET /official/media/{media_id}``
        """
        return self._request(
            "GET", f"/official/media/{self._seg(media_id, 'media_id')}", params={"instance_id": instance_id}
        )

    def delete_official_media(self, media_id: str, instance_id: Optional[str] = None) -> None:
        """Delete a stored file from Meta before it expires. ``DELETE /official/media/{media_id}``"""
        self._request(
            "DELETE", f"/official/media/{self._seg(media_id, 'media_id')}", params={"instance_id": instance_id}
        )

    def list_meta_webhook_events(self, limit: Optional[int] = None, before: Optional[int] = None) -> JSONDict:
        """Raw events Meta sent for the project's accounts, newest first. ``GET /official/webhook-events``

        The "Meta → bZapper" side of webhooks, with how each event was processed
        (``pending``, ``running``, ``done`` or ``failed``). Pass the last ``id`` as
        ``before`` to page back.
        """
        return self._request("GET", "/official/webhook-events", params={"limit": limit, "before": before})

    def list_official_numbers(self) -> JSONDict:
        """List the numbers connected to the project. ``GET /official/numbers``

        Meta allows several numbers on one business account. Each has an
        ``instance_id`` — the same value you pass as ``instance_id`` in a send to
        choose which number it goes out from, exactly as on the unofficial rail.
        The one flagged ``is_default`` sends when a request does not pick one.

        Returns:
            ``{"numbers": [...]}``, default first.
        """
        return self._request("GET", "/official/numbers")

    def list_available_official_numbers(self) -> JSONDict:
        """Numbers of the business account not connected here yet. ``GET /official/numbers/available``

        What feeds "add a number": the numbers the WABA holds on Meta, minus the
        ones already connected — without opening Meta's dashboard.

        Returns:
            ``{"numbers": [...]}`` (``id`` is what :meth:`add_official_number` takes).
        """
        return self._request("GET", "/official/numbers/available")

    def add_official_number(
        self, phone_number_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Connect one more number from the same business account. ``POST /official/numbers``

        Reuses the authorization already stored (it belongs to the account, not
        the number). The number is checked against Meta before it is saved, so a
        wrong id fails here and not on the first send.

        Args:
            phone_number_id: Meta's id of the number (``id`` from
                :meth:`list_available_official_numbers`).
        """
        return self._request(
            "POST",
            "/official/numbers",
            body={"phone_number_id": phone_number_id},
            idempotency_key=idempotency_key,
        )

    def set_default_official_number(
        self, number_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Choose the number that sends when a request does not pick one. ``PUT /official/numbers/{id}/default``"""
        return self._request(
            "PUT",
            f"/official/numbers/{self._seg(number_id, 'number_id')}/default",
            idempotency_key=idempotency_key,
        )

    def disconnect_official_number(
        self, number_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Disconnect one number; the conversation history stays. ``DELETE /official/numbers/{id}``

        Removing the default promotes the oldest remaining number, so the
        project never ends up with numbers and no default.
        """
        return self._request(
            "DELETE",
            f"/official/numbers/{self._seg(number_id, 'number_id')}",
            idempotency_key=idempotency_key,
        )

    # -- official business profile ---------------------------------------------

    @staticmethod
    def _business_profile(
        about: Optional[str],
        description: Optional[str],
        address: Optional[str],
        email: Optional[str],
        vertical: Optional[str],
        websites: Optional[Sequence[str]],
    ) -> JSONDict:
        return {
            "about": about,
            "description": description,
            "address": address,
            "email": email,
            "vertical": vertical,
            "websites": list(websites) if websites is not None else None,
        }

    def get_official_profile(self) -> JSONDict:
        """Business profile of the project's default number. ``GET /official/profile``

        What the end customer sees when opening the chat: ``about``,
        ``description``, ``address``, ``email``, ``vertical``, up to two
        ``websites`` and ``profile_picture_url``. It lives on the NUMBER — with
        several numbers, use :meth:`get_official_number_profile`.
        """
        return self._request("GET", "/official/profile")

    def update_official_profile(
        self,
        *,
        about: Optional[str] = None,
        description: Optional[str] = None,
        address: Optional[str] = None,
        email: Optional[str] = None,
        vertical: Optional[str] = None,
        websites: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Update the business profile of the default number. ``PUT /official/profile``

        Send the WHOLE profile: an empty field is not "unchanged", it clears that
        field on Meta. Read it first with :meth:`get_official_profile` and send it
        back with your edits. Validated against Meta's limits before sending
        (``about`` 139 chars, ``description`` 512, ``address`` 256, at most 2
        ``websites``, a closed list of ``vertical``) — a refusal (422) names the
        field and by how much it overflowed.

        Returns:
            The profile as saved (normalized as sent to Meta).
        """
        body = self._business_profile(about, description, address, email, vertical, websites)
        return self._request("PUT", "/official/profile", body=body, idempotency_key=idempotency_key)

    def upload_official_profile_photo(
        self,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Optional[JSONDict]:
        """Replace the profile photo of the default number (multipart). ``POST /official/profile/photo``

        JPEG/PNG up to 5 MB (Meta recommends 640x640). WhatsApp Business has only
        the round profile photo — there is no cover image.

        Args:
            file: Raw bytes, a path on disk or a binary file object.
            filename: File name of the part (default: basename or ``"file"``).
            content_type: MIME type (default: guessed from the file name).

        Returns:
            The profile read back (new ``profile_picture_url``), or ``None`` when
            the photo was saved but the profile could not be read back yet (204).
        """
        return self._upload(
            "/official/profile/photo", file, filename, content_type, None, idempotency_key
        )

    def reregister_official_profile(self, *, idempotency_key: Optional[str] = None) -> None:
        """Re-register the default number on the Cloud API. ``POST /official/profile/reregister``

        The step people forget after Meta approves a new display name: until the
        number is re-registered it keeps showing the old name. Already registered
        counts as success.
        """
        return self._request(
            "POST", "/official/profile/reregister", idempotency_key=idempotency_key
        )

    def get_official_number_profile(self, number_id: str) -> JSONDict:
        """Business profile of one connected number. ``GET /official/numbers/{id}/profile``

        Args:
            number_id: Connected number id (``id`` from :meth:`list_official_numbers`).
        """
        return self._request(
            "GET", f"/official/numbers/{self._seg(number_id, 'number_id')}/profile"
        )

    def update_official_number_profile(
        self,
        number_id: str,
        *,
        about: Optional[str] = None,
        description: Optional[str] = None,
        address: Optional[str] = None,
        email: Optional[str] = None,
        vertical: Optional[str] = None,
        websites: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Update the business profile of one number. ``PUT /official/numbers/{id}/profile``

        Same rules as :meth:`update_official_profile`: send the WHOLE profile —
        an empty field clears it on Meta.
        """
        body = self._business_profile(about, description, address, email, vertical, websites)
        return self._request(
            "PUT",
            f"/official/numbers/{self._seg(number_id, 'number_id')}/profile",
            body=body,
            idempotency_key=idempotency_key,
        )

    def upload_official_number_profile_photo(
        self,
        number_id: str,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Optional[JSONDict]:
        """Replace the profile photo of one number (multipart). ``POST /official/numbers/{id}/profile/photo``

        Same as :meth:`upload_official_profile_photo` (JPEG/PNG up to 5 MB),
        for the number given.
        """
        return self._upload(
            f"/official/numbers/{self._seg(number_id, 'number_id')}/profile/photo",
            file,
            filename,
            content_type,
            None,
            idempotency_key,
        )

    def reregister_official_number_profile(
        self, number_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Re-register one number on the Cloud API. ``POST /official/numbers/{id}/profile/reregister``

        See :meth:`reregister_official_profile`.
        """
        return self._request(
            "POST",
            f"/official/numbers/{self._seg(number_id, 'number_id')}/profile/reregister",
            idempotency_key=idempotency_key,
        )

    # -- templates (official rail) ---------------------------------------------

    def send_template(
        self,
        to: str,
        template: str,
        *,
        language: Optional[str] = None,
        variables: Optional[Sequence[str]] = None,
        header_variables: Optional[Sequence[str]] = None,
        button_variables: Optional[Sequence[str]] = None,
        code: Optional[str] = None,
        instance_id: Optional[str] = None,
        pool_id: Optional[str] = None,
        quoted_message_id: Optional[str] = None,
        client_reference: Optional[str] = None,
        mentions: Optional[Sequence[str]] = None,
        sticky: Optional[bool] = None,
        scheduled_at: Optional[str] = None,
        quoted_participant: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        force: Optional[bool] = None,
    ) -> JSONDict:
        """Send an approved template by name. ``POST /messages/template``

        OFFICIAL RAIL ONLY (on the unofficial rail the API answers 409). This is
        the only way to start a conversation outside the 24-hour service window.
        You pass the template NAME; the id Meta assigned to it never appears here.

        Args:
            to: Destination phone in E.164 (``+5511...``) or a JID.
            template: The template name.
            language: e.g. ``pt_BR``. May be omitted when the name exists in a
                single language; in more than one the API answers 409
                ``template_ambiguous`` instead of guessing.
            variables: Body values, in order (``{{1}}``, ``{{2}}``…). The count
                must match the approved template exactly — a mismatch is the
                number one cause of Meta error 132000, and the API checks it
                (422) before spending the call.
            header_variables: Value for the header variable, when there is one.
            button_variables: Values for dynamic URL buttons, by button index.
            code: AUTHENTICATION templates only. Meta requires the code both in
                the body and in the copy-code button; the API places it in both.

        Returns:
            The queued-message object (same as :meth:`send_text`).
        """
        payload = self._send_base(
            to,
            instance_id=instance_id,
            pool_id=pool_id,
            quoted_message_id=quoted_message_id,
            client_reference=client_reference,
            mentions=mentions,
            sticky=sticky,
            scheduled_at=scheduled_at,
            quoted_participant=quoted_participant,
            groups=groups,
            tags=tags,
            force=force,
        )
        payload["template"] = template
        payload["language"] = language
        payload["variables"] = list(variables) if variables is not None else None
        payload["header_variables"] = (
            list(header_variables) if header_variables is not None else None
        )
        payload["button_variables"] = (
            list(button_variables) if button_variables is not None else None
        )
        payload["code"] = code
        return self._request(
            "POST",
            "/messages/template",
            body=payload,
            headers=self._idempotency_headers(idempotency_key),
        )

    def list_templates(self) -> JSONDict:
        """List the project templates, newest first. ``GET /templates``

        Each item carries ``status`` (``PENDING``/``APPROVED``/``REJECTED``/
        ``PAUSED``/``DISABLED``) and ``variables`` — how many values the body
        expects, and therefore how many you must pass when sending.

        Returns:
            ``{"templates": [...]}``.
        """
        return self._request("GET", "/templates")

    def create_template(
        self,
        name: str,
        language: str,
        category: str,
        body: Mapping[str, Any],
        *,
        header: Optional[Mapping[str, Any]] = None,
        footer: Optional[str] = None,
        buttons: Optional[Sequence[Mapping[str, Any]]] = None,
        code_expiry_minutes: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a template and submit it to Meta for review. ``POST /templates``

        You describe header, body, footer and buttons; the API builds the
        component format Meta expects. Validation happens BEFORE submission:
        Meta takes up to 24 hours to answer, rejects with a terse reason and caps
        how many templates you may submit. A variable at the start or end of the
        body, numbering that skips, two variables side by side, a missing example
        — each comes back as 422 with a message that says what to fix. The
        decision arrives later through the ``template.approved`` /
        ``template.rejected`` webhooks.

        Args:
            name: Lowercase letters, numbers and ``_``. This is the name you send by.
            language: e.g. ``pt_BR`` (``pt-BR`` is accepted too).
            category: ``MARKETING``, ``UTILITY`` or ``AUTHENTICATION``. It decides
                what the conversation COSTS and what Meta accepts in the content.
            body: ``{"text": ..., "example": [...]}``. Use ``{{1}}``, ``{{2}}``…
                for the values that change, and give one example per variable.
            header: ``{"type": "text"|"image"|"video"|"document", "text"?,
                "example"?, "media_url"?}`` (``media_url`` from
                :meth:`upload_template_media`).
            footer: Up to 60 characters, no variables.
            buttons: Up to 10, each ``{"type": "quick_reply"|"url"|"phone_number",
                "text": ..., "url"?, "phone"?, "example"?}``.
            code_expiry_minutes: ``AUTHENTICATION`` only (1–90). In that category
                Meta writes the text and adds the copy-code button.

        Returns:
            ``{"template": ..., "advice": [...]}``. ``advice`` lists things that did
            NOT block the submission but change the outcome — promotional wording
            in a ``UTILITY`` template, for instance, which Meta reclassifies on its
            own, and reclassification changes the price.
        """
        spec = {
            "name": name,
            "language": language,
            "category": category,
            "body": dict(body),
            "header": dict(header) if header is not None else None,
            "footer": footer,
            "buttons": [dict(b) for b in buttons] if buttons is not None else None,
            "code_expiry_minutes": code_expiry_minutes,
        }
        return self._request("POST", "/templates", body=spec, idempotency_key=idempotency_key)

    def get_template(self, template_id: str) -> JSONDict:
        """Get one template. ``GET /templates/{id}``"""
        return self._request("GET", f"/templates/{self._seg(template_id, 'template_id')}")

    def delete_template(self, template_id: str, *, idempotency_key: Optional[str] = None) -> None:
        """Delete a template here and at Meta. ``DELETE /templates/{id}``

        Meta deletes ALL languages of a name at once — so while another language
        of the same name is still registered here, Meta is left alone.
        """
        return self._request(
            "DELETE",
            f"/templates/{self._seg(template_id, 'template_id')}",
            idempotency_key=idempotency_key,
        )

    def sync_templates(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Import templates that exist at Meta and not here. ``POST /templates/sync``

        For the account that used the Cloud API before bZapper and created
        templates in WhatsApp Manager, and to repair a webhook that never
        arrived. Returns ``{"imported": n}``.
        """
        return self._request("POST", "/templates/sync", idempotency_key=idempotency_key)

    def upload_template_media(
        self,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Upload the header sample of a template (multipart). ``POST /templates/media``

        JPEG/PNG/MP4/PDF up to 10 MB. Meta reviews media headers from a sample
        file: pass the returned ``url`` as ``header["media_url"]`` in
        :meth:`create_template` — no need to host the file anywhere. It is only
        the sample Meta reviews, not the media that goes out on each send.

        Args:
            file: Raw bytes, a path on disk or a binary file object.
            filename: File name of the part (default: basename or ``"file"``).
            content_type: MIME type (default: guessed from the file name).

        Returns:
            ``{"url"}``.
        """
        return self._upload("/templates/media", file, filename, content_type, None, idempotency_key)

    def get_fallback_template(self) -> JSONDict:
        """The template sent when the 24-hour window is closed. ``GET /templates/fallback``

        ``{"template": None}`` when none is set.
        """
        return self._request("GET", "/templates/fallback")

    def set_fallback_template(
        self, template_id: Optional[str], *, idempotency_key: Optional[str] = None
    ) -> None:
        """Set the template sent when the 24-hour window is closed. ``PUT /templates/fallback``

        This is what makes the same :meth:`send_text` work inside and outside the
        window: when the contact has not written for over 24 hours, the platform
        sends this approved template instead and reports the detour in the
        ``delivery`` field of the send result. Only an APPROVED template is
        accepted (409 otherwise). ``None`` (or ``""``) clears it.
        """
        return self._request(
            "PUT",
            "/templates/fallback",
            body={"template_id": template_id or ""},
            idempotency_key=idempotency_key,
        )

    # -- API keys --------------------------------------------------------------

    def list_keys(self) -> JSONDict:
        """List the tenant's API keys (raw key not included)."""
        return self._request("GET", "/keys")

    def create_key(
        self, name: str, role: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Create a tenant API key.

        Args:
            name: Human label for the key.
            role: ``"admin"`` or ``"agent"``.

        Returns:
            ``{"api_key", "key"}`` — the raw ``api_key`` is shown only once.
        """
        return self._request(
            "POST", "/keys", body={"name": name, "role": role}, idempotency_key=idempotency_key
        )

    def revoke_key(self, key_id: str, *, idempotency_key: Optional[str] = None) -> None:
        """Revoke a tenant API key by ID."""
        return self._request(
            "DELETE", f"/keys/{self._seg(key_id, 'key_id')}", idempotency_key=idempotency_key
        )

    def rotate_key(
        self,
        key_id: str,
        *,
        revoke_in_seconds: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Rotate a tenant API key (admin only). ``POST /keys/{id}/rotate``

        Creates a NEW key inheriting the old one's name, role, scopes and project,
        and keeps the OLD one working for a grace period — so a running deploy does
        not break in the middle of the swap. Past the deadline the old key answers
        ``401 key_expired``. Partner keys (bZapper Connect) rotate through
        :meth:`PartnerClient.rotate_connection_key` instead.

        Args:
            key_id: ID of the key being rotated.
            revoke_in_seconds: Grace period for the OLD key in seconds (default
                ``86400``; ``0`` revokes it immediately; max ``2592000`` = 30 days).
            idempotency_key: Optional idempotency key (24 h) for this call.

        Returns:
            ``{"api_key", "key", "previous_key", "old_key_expires_at"}`` — the raw
            ``api_key`` is shown ONLY here, and ``old_key_expires_at`` is ``None``
            when the old key was revoked immediately. The rotated key carries
            ``expires_at`` (when it stops working) and ``rotated_to`` (the id of
            the key that replaced it).

        Raises:
            BzapperError: ``admin_required`` (403), ``not_found`` (404),
                ``key_already_revoked`` / ``key_already_expired`` (409).

        Example:
            >>> rotated = client.rotate_key(old_id, revoke_in_seconds=3600)
            >>> rotated["api_key"]  # store it now; it is never shown again
            'bz_live_...'
        """
        body = {"revoke_in_seconds": revoke_in_seconds} if revoke_in_seconds is not None else None
        return self._request(
            "POST",
            f"/keys/{self._seg(key_id, 'key_id')}/rotate",
            body=body,
            idempotency_key=idempotency_key,
        )

    # -- advisories -----------------------------------------------------------

    def list_advisories(self) -> JSONDict:
        """List pending integration advisories. ``GET /advisories``

        An advisory means a change on our side requires you to update YOUR code
        (an SDK to upgrade, a payload or endpoint that changed). It is never a
        changelog: you only receive advisories that affect your account, matched
        against the SDK version you run and the features you actually use.

        Each item has ``id``, ``title``, ``impact``, ``action``, ``link`` and
        ``published_at``. Use ``action`` — it says what to do.
        """
        return self._request("GET", "/advisories")

    def mark_advisory_read(
        self, advisory_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Dismiss an advisory once handled. ``POST /advisories/{id}/read``"""
        return self._request(
            "POST",
            f"/advisories/{self._seg(advisory_id, 'advisory_id')}/read",
            idempotency_key=idempotency_key,
        )

    # -- connected apps (bZapper Connect, customer side) -----------------------

    def list_connected_apps(self) -> JSONDict:
        """List partner apps connected to this account. ``GET /me/connections``

        Partner software using this account's WhatsApp through bZapper Connect.
        Returns ``{"data": [...]}``; each item has ``id``, ``external_id``,
        ``status``, ``partner_name``, ``partner_logo_url``, ``numbers`` and the
        ``activated_at``/``suspended_at``/``revoked_at`` timestamps.
        """
        return self._request("GET", "/me/connections")

    def revoke_connected_app(
        self, connection_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Disconnect a partner app (admin). ``DELETE /me/connections/{id}``

        The partner's key stops working immediately (it answers 401
        ``connect_revoked``). Does not change your plan.
        """
        path = f"/me/connections/{self._seg(connection_id, 'connection_id')}"
        return self._request("DELETE", path, idempotency_key=idempotency_key)

    def convert_connected_app_to_standalone(
        self, connection_id: str, *, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Convert a partner-created account to general use (admin).

        ``POST /me/connections/{id}/standalone``

        The connection STAYS: the partner keeps working and any partnership
        discount keeps applying — what changes is that the account may now issue
        its own API keys. Irreversible.
        """
        path = f"/me/connections/{self._seg(connection_id, 'connection_id')}/standalone"
        return self._request("POST", path, idempotency_key=idempotency_key)

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
        idempotency_key: Optional[str] = None,
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
            idempotency_key=idempotency_key,
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
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Update/pause a webhook. ``secret="regenerate"`` rotates it. ``PATCH /webhooks/{id}``"""
        return self._request(
            "PATCH",
            f"/webhooks/{self._seg(webhook_id, 'webhook_id')}",
            body={"url": url, "secret": secret, "event_types": event_types, "number_filter": number_filter, "active": active},
            idempotency_key=idempotency_key,
        )

    def delete_webhook(
        self, webhook_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Delete a webhook. ``DELETE /webhooks/{id}``"""
        return self._request(
            "DELETE",
            f"/webhooks/{self._seg(webhook_id, 'webhook_id')}",
            idempotency_key=idempotency_key,
        )

    def test_webhook(
        self,
        webhook_id: str,
        event_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Send a test event and return the endpoint's HTTP status. ``POST /webhooks/{id}/test``"""
        return self._request(
            "POST",
            f"/webhooks/{self._seg(webhook_id, 'webhook_id')}/test",
            body={"event_type": event_type},
            idempotency_key=idempotency_key,
        )

    def webhook_deliveries(self, webhook_id: str, *, limit: Optional[int] = None) -> JSONDict:
        """Recent delivery attempts for a webhook. ``GET /webhooks/{id}/deliveries``"""
        return self._request("GET", f"/webhooks/{self._seg(webhook_id, 'webhook_id')}/deliveries", params={"limit": limit})

    def redeliver_webhook_delivery(self, webhook_id: str, delivery_id: str) -> JSONDict:
        """Redeliver a FAILED delivery (same body and event_id, signed with the current secret).

        Only failures from the last 7 days. ``POST /webhooks/{id}/deliveries/{delivery_id}/redeliver``
        """
        return self._request(
            "POST",
            f"/webhooks/{self._seg(webhook_id, 'webhook_id')}/deliveries/{self._seg(delivery_id, 'delivery_id')}/redeliver",
        )

    def trigger_webhook_event(
        self, event_type: Optional[str] = None, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Emit a sample event to the project's stream and webhooks. ``POST /webhooks/trigger``

        Like ``stripe trigger``: handy to test your receiver end to end.
        """
        return self._request(
            "POST",
            "/webhooks/trigger",
            body={"event_type": event_type},
            idempotency_key=idempotency_key,
        )

    # -- usage -----------------------------------------------------------------

    def get_usage(
        self, *, from_: Optional[str] = None, to: Optional[str] = None
    ) -> JSONDict:
        """Get a usage summary for the tenant.

        Args:
            from_: Start of window, RFC3339 (e.g. ``2026-06-01T00:00:00Z``).
                A ``datetime`` is also accepted (converted to UTC ``Z``).
            to: End of window, RFC3339.
        """
        return self._request("GET", "/usage", params={"from": from_, "to": to})

    # -- presence (works in groups!) ------------------------------------------

    def presence_chat(
        self,
        instance_id: str,
        to: str,
        state: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Send a chat-presence update (typing indicator).

        Args:
            instance_id: Instance to act on (sent in the body).
            to: Destination phone (E.164) or JID — may be a **group** JID.
            state: ``"typing"``, ``"recording"`` or ``"paused"``.
        """
        body = {"instance_id": instance_id, "to": to, "state": state}
        return self._request("POST", "/presence/chat", body=body, idempotency_key=idempotency_key)

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
            f"/conversations/{self._seg(jid, 'jid')}/messages",
            params={"instance_id": instance_id, "before": before, "limit": limit},
        )

    # -- chats -----------------------------------------------------------------

    def archive_chat(
        self, jid: str, instance_id: str, on: bool, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Archive (``on=True``) or unarchive (``on=False``) a chat."""
        body = {"instance_id": instance_id, "on": on}
        return self._request(
            "POST", f"/chats/{self._seg(jid, 'jid')}/archive", body=body, idempotency_key=idempotency_key
        )

    def pin_chat(
        self, jid: str, instance_id: str, on: bool, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Pin (``on=True``) or unpin (``on=False``) a chat."""
        body = {"instance_id": instance_id, "on": on}
        return self._request(
            "POST", f"/chats/{self._seg(jid, 'jid')}/pin", body=body, idempotency_key=idempotency_key
        )

    def mark_chat(
        self, jid: str, instance_id: str, on: bool, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Mark a chat as read (``on=True``) or unread (``on=False``)."""
        body = {"instance_id": instance_id, "on": on}
        return self._request(
            "POST", f"/chats/{self._seg(jid, 'jid')}/read", body=body, idempotency_key=idempotency_key
        )

    def mute_chat(
        self, jid: str, instance_id: str, on: bool, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Mute (``on=True``) or unmute (``on=False``) a chat. ``POST /chats/{jid}/mute``"""
        body = {"instance_id": instance_id, "on": on}
        return self._request(
            "POST", f"/chats/{self._seg(jid, 'jid')}/mute", body=body, idempotency_key=idempotency_key
        )

    def apply_chat_label(
        self,
        jid: str,
        instance_id: str,
        label_id: str,
        *,
        apply: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Apply (default) or remove (``apply=False``) a label on a chat (experimental).

        ``POST /chats/{jid}/labels``
        """
        body = {"instance_id": instance_id, "label_id": label_id, "apply": apply}
        return self._request(
            "POST", f"/chats/{self._seg(jid, 'jid')}/labels", body=body, idempotency_key=idempotency_key
        )

    # -- labels (WhatsApp Business, experimental) ------------------------------

    def list_labels(self, instance_id: str) -> JSONDict:
        """List the number's labels (experimental). ``GET /labels``"""
        return self._request("GET", "/labels", params={"instance_id": instance_id})

    def create_label(
        self,
        instance_id: str,
        name: str,
        *,
        color: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a label (experimental). ``POST /labels``"""
        body = {"instance_id": instance_id, "name": name, "color": color}
        return self._request("POST", "/labels", body=body, idempotency_key=idempotency_key)

    def delete_label(
        self, label_id: str, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Delete a label (experimental). ``DELETE /labels/{id}``"""
        return self._request(
            "DELETE",
            f"/labels/{self._seg(label_id, 'label_id')}",
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    # -- block list and calls --------------------------------------------------

    def block_contact(
        self, jid: str, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Block a contact on the number. ``POST /contacts/{jid}/block``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(jid, 'jid')}/block",
            body={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def unblock_contact(
        self, jid: str, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Unblock a contact on the number. ``POST /contacts/{jid}/unblock``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(jid, 'jid')}/unblock",
            body={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def get_blocklist(self, instance_id: str) -> Any:
        """List the contacts blocked by the number. ``GET /blocklist``"""
        return self._request("GET", "/blocklist", params={"instance_id": instance_id})

    def reject_call(
        self,
        instance_id: str,
        call_from: str,
        call_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Reject an incoming call (ids come in the call webhook). ``POST /calls/reject``"""
        body = {"instance_id": instance_id, "call_from": call_from, "call_id": call_id}
        return self._request("POST", "/calls/reject", body=body, idempotency_key=idempotency_key)

    def offer_call(
        self,
        instance_id: str,
        to: str,
        *,
        video: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Start a call (experimental). ``POST /calls/offer``"""
        body = {"instance_id": instance_id, "to": to, "video": video}
        return self._request("POST", "/calls/offer", body=body, idempotency_key=idempotency_key)

    # -- groups ----------------------------------------------------------------

    def list_groups(self, instance_id: str) -> JSONDict:
        """List the groups the instance belongs to."""
        return self._request(
            "GET", "/groups", params={"instance_id": instance_id}
        )

    def create_group(
        self,
        instance_id: str,
        name: str,
        participants: Sequence[str],
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a group.

        Args:
            instance_id: Instance to act on (query parameter).
            name: Group subject/name.
            participants: Phones (E.164) or JIDs of the initial members.
        """
        body = {"name": name, "participants": list(participants)}
        return self._request(
            "POST", "/groups", body=body, params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def get_group(self, jid: str, instance_id: str) -> JSONDict:
        """Fetch a single group by JID."""
        return self._request(
            "GET", f"/groups/{self._seg(jid, 'jid')}", params={"instance_id": instance_id}
        )

    def update_group(
        self,
        jid: str,
        instance_id: str,
        *,
        name: Optional[str] = None,
        topic: Optional[str] = None,
        announce: Optional[bool] = None,
        locked: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Update the group name/topic/settings. ``PATCH /groups/{jid}``

        Args:
            name: New subject.
            topic: New description.
            announce: Only admins send messages.
            locked: Only admins edit the group info.
        """
        body = {"name": name, "topic": topic, "announce": announce, "locked": locked}
        return self._request(
            "PATCH",
            f"/groups/{self._seg(jid, 'jid')}",
            body=body,
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def preview_group_invite(
        self, instance_id: str, code: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Show the group behind an invite (name, topic, size) WITHOUT joining.

        Use it to confirm before putting the number in someone else's group.
        ``code`` is the invite code or link. POST /groups/join/preview.
        """
        return self._request(
            "POST",
            "/groups/join/preview",
            body={"code": code},
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def join_group(
        self, instance_id: str, code: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Join a group via its invite code."""
        return self._request(
            "POST",
            "/groups/join",
            body={"code": code},
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def update_group_participants(
        self,
        jid: str,
        instance_id: str,
        action: str,
        participants: Sequence[str],
        *,
        idempotency_key: Optional[str] = None,
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
            f"/groups/{self._seg(jid, 'jid')}/participants",
            body=body,
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def leave_group(
        self, jid: str, instance_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Leave a group."""
        return self._request(
            "POST", f"/groups/{self._seg(jid, 'jid')}/leave", params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    def group_invite(
        self, jid: str, instance_id: str, *, reset: Optional[bool] = None
    ) -> JSONDict:
        """Get a group's invite link/code (``reset=True`` revokes the old one)."""
        return self._request(
            "GET",
            f"/groups/{self._seg(jid, 'jid')}/invite",
            params={"instance_id": instance_id, "reset": reset},
        )

    def list_join_requests(self, jid: str, instance_id: str) -> Any:
        """Pending requests to join the group. ``GET /groups/{jid}/join-requests``"""
        return self._request(
            "GET",
            f"/groups/{self._seg(jid, 'jid')}/join-requests",
            params={"instance_id": instance_id},
        )

    def update_join_requests(
        self,
        jid: str,
        instance_id: str,
        participants: Sequence[str],
        approve: bool,
        *,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Approve (``approve=True``) or reject join requests. ``POST /groups/{jid}/join-requests``"""
        body = {"participants": list(participants), "approve": approve}
        return self._request(
            "POST",
            f"/groups/{self._seg(jid, 'jid')}/join-requests",
            body=body,
            params={"instance_id": instance_id},
            idempotency_key=idempotency_key,
        )

    # -- contacts --------------------------------------------------------------

    def contacts_check(
        self, instance_id: str, phones: Sequence[str], *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Check which phone numbers are registered on WhatsApp.

        Args:
            instance_id: Instance to act on (sent in the body).
            phones: Phones in E.164 to verify.
        """
        body = {"instance_id": instance_id, "phones": list(phones)}
        return self._request("POST", "/contacts/check", body=body, idempotency_key=idempotency_key)

    # -- profile ---------------------------------------------------------------

    def set_profile(
        self,
        instance_id: str,
        *,
        display_name: Optional[str] = None,
        status_message: Optional[str] = None,
        picture: Optional[str] = None,
        idempotency_key: Optional[str] = None,
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
            "PATCH", f"/instances/{self._seg(instance_id, 'instance_id')}/profile", body=body,
            idempotency_key=idempotency_key,
        )

    # -- contacts (captured from conversations — shared across the account) ----

    def list_contacts(
        self,
        *,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        tags: Optional[Sequence[str]] = None,
        tags_match: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        status: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country: Optional[str] = None,
        zip: Optional[str] = None,
        document: Optional[str] = None,
        has_email: Optional[bool] = None,
        last_activity_after: Optional[str] = None,
        last_activity_before: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> JSONDict:
        """List the account's shared contact base (auto-captured from chats).

        Args:
            search: Optional free-text filter (name/phone/email/document).
            project_id: Optional project filter — a project id or ``"current"``
                (the project the API key belongs to).
            instance_id: Optional filter by a number (instance) the contact
                interacted with. The contact↔number link is maintained
                automatically by the API (inbound/outbound correlation).
            limit: Optional max number of contacts.
            offset: Pagination offset.
            tags: Tag keys; ``tags_match`` = ``"any"`` (default) or ``"all"``.
            groups: Contact-group keys.
            status: ``active``, ``pending_validation``, ``opted_out``,
                ``blocked`` or ``unreachable``.
            city, state, country, zip, document: Address/document filters.
            has_email: Only contacts with (``True``) or without an email.
            last_activity_after, last_activity_before, created_after,
                created_before: RFC3339 timestamps (``datetime`` accepted).
            sort: ``last_activity`` (default), ``name`` or ``created``.

        Returns:
            ``{"data": [Contact...], "total", "limit", "offset"}``.
        """
        return self._request(
            "GET",
            "/contacts",
            params={
                "search": search,
                "project_id": project_id,
                "instance_id": instance_id,
                "limit": limit,
                "offset": offset,
                "tags": tags,
                "tags_match": tags_match,
                "groups": groups,
                "status": status,
                "city": city,
                "state": state,
                "country": country,
                "zip": zip,
                "document": document,
                "has_email": has_email,
                "last_activity_after": last_activity_after,
                "last_activity_before": last_activity_before,
                "created_after": created_after,
                "created_before": created_before,
                "sort": sort,
            },
        )

    def create_contact(
        self,
        phone: str,
        *,
        name: Optional[str] = None,
        email: Optional[str] = None,
        document: Optional[str] = None,
        document_type: Optional[str] = None,
        address: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a contact. ``POST /contacts``

        Args:
            phone: Phone in ``+DDIdigits``.
            address: ``{street, number, complement, district, city, state,
                country, zip}``.
        """
        body = {
            "phone": phone,
            "name": name,
            "email": email,
            "document": document,
            "document_type": document_type,
            "address": dict(address) if address is not None else None,
        }
        return self._request("POST", "/contacts", body=body, idempotency_key=idempotency_key)

    def get_contact(self, contact_id: str) -> JSONDict:
        """Get a contact. ``GET /contacts/{id}``"""
        return self._request("GET", f"/contacts/{self._seg(contact_id, 'contact_id')}")

    def update_contact(
        self,
        contact_id: str,
        *,
        name: Optional[str] = None,
        email: Optional[str] = None,
        document: Optional[str] = None,
        document_type: Optional[str] = None,
        address: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Update a contact (only the given fields). ``PATCH /contacts/{id}``"""
        body = {
            "name": name,
            "email": email,
            "document": document,
            "document_type": document_type,
            "address": dict(address) if address is not None else None,
        }
        return self._request(
            "PATCH",
            f"/contacts/{self._seg(contact_id, 'contact_id')}",
            body=body,
            idempotency_key=idempotency_key,
        )

    def delete_contact(
        self, contact_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Delete a contact. ``DELETE /contacts/{id}``"""
        return self._request(
            "DELETE",
            f"/contacts/{self._seg(contact_id, 'contact_id')}",
            idempotency_key=idempotency_key,
        )

    def get_contact_history(
        self, contact_id: str, *, limit: Optional[int] = None
    ) -> JSONDict:
        """Contact timeline (messages + events). ``GET /contacts/{id}/history``"""
        return self._request(
            "GET",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/history",
            params={"limit": limit},
        )

    def add_contact_note(
        self, contact_id: str, body: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Add an internal note to the contact. ``POST /contacts/{id}/notes``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/notes",
            body={"body": body},
            idempotency_key=idempotency_key,
        )

    def mutate_contact_tags(
        self,
        contact_id: str,
        *,
        add: Optional[Sequence[str]] = None,
        remove: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Add/remove tag keys on the contact. ``POST /contacts/{id}/tags``"""
        body = {
            "add": list(add) if add is not None else None,
            "remove": list(remove) if remove is not None else None,
        }
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/tags",
            body=body,
            idempotency_key=idempotency_key,
        )

    def mutate_contact_groups(
        self,
        contact_id: str,
        *,
        add: Optional[Sequence[str]] = None,
        remove: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Add/remove contact-group keys on the contact. ``POST /contacts/{id}/groups``"""
        body = {
            "add": list(add) if add is not None else None,
            "remove": list(remove) if remove is not None else None,
        }
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/groups",
            body=body,
            idempotency_key=idempotency_key,
        )

    def opt_out_contact(
        self, contact_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Opt the contact out (LGPD) — it stops receiving sends. ``POST /contacts/{id}/optout``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/optout",
            idempotency_key=idempotency_key,
        )

    def suppress_contact(
        self, contact_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Manually suppress the contact. ``POST /contacts/{id}/suppress``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/suppress",
            idempotency_key=idempotency_key,
        )

    def opt_in_contact(
        self, contact_id: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Opt the contact back in (removes the suppression). ``POST /contacts/{id}/optin``"""
        return self._request(
            "POST",
            f"/contacts/{self._seg(contact_id, 'contact_id')}/optin",
            idempotency_key=idempotency_key,
        )

    # -- contacts: bulk import and CSV export ----------------------------------

    def import_contacts(
        self,
        contacts: Sequence[Mapping[str, Any]],
        *,
        dry_run: Optional[bool] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Import (upsert) contacts in bulk by phone. ``POST /contacts/import``

        Up to **1000** rows per call. A new contact is created with
        ``source: import`` and ``status: pending_validation`` (it still needs
        opt-in before a campaign); an existing one has only the informed fields
        updated — a blank value never erases what is there. A suppressed, opted-out
        or blocked contact lands in ``skipped_rows`` and is never resurrected. Tags
        and groups are created on demand. A bad row goes to ``errors`` and does NOT
        fail the rest of the call.

        Args:
            contacts: Rows of ``{phone, name?, email?, document?, document_type?,
                address?, tags?, groups?}`` — ``phone`` in ``+DDIdigits`` is the only
                required field, ``address`` is ``{street, number, complement,
                district, city, state, zip, country}``. Keys whose value is ``None``
                are not sent.
            dry_run: ``True`` validates everything and writes nothing.
            idempotency_key: Optional idempotency key (24 h) for this call.

        Returns:
            ``{"dry_run", "total", "created", "updated", "skipped", "failed",
            "skipped_rows": [...], "errors": [...]}``; each issue is
            ``{"index", "phone", "reason", "detail"}`` where ``index`` is the
            position in the array you sent and ``reason`` one of
            ``phone_required``, ``invalid_phone``, ``invalid_email``,
            ``write_failed``, ``taxonomy_failed`` (errors) or ``duplicate_phone``,
            ``suppressed``, ``opted_out``, ``blocked``, ``unreachable``,
            ``deleted`` (skips).

        Raises:
            BzapperError: ``invalid_body`` / ``contacts_required`` (400) or
                ``import_too_large`` (422, more than 1000 rows).

        Example:
            >>> result = client.import_contacts(
            ...     [{"phone": "+5511999990000", "name": "Ana", "tags": ["lead"]}],
            ...     dry_run=True,
            ... )
            >>> result["created"], result["skipped_rows"]
            (1, [])
        """
        rows = [
            {key: value for key, value in dict(row).items() if value is not None}
            for row in contacts
        ]
        return self._request(
            "POST",
            "/contacts/import",
            body={"contacts": rows, "dry_run": dry_run},
            idempotency_key=idempotency_key,
        )

    def export_contacts(
        self,
        *,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        limit: Optional[int] = None,
        tags: Optional[Sequence[str]] = None,
        tags_match: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        status: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country: Optional[str] = None,
        zip: Optional[str] = None,
        document: Optional[str] = None,
        has_email: Optional[bool] = None,
        last_activity_after: Optional[str] = None,
        last_activity_before: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> str:
        """Export the contact base as **CSV text**. ``GET /contacts/export``

        Same filters as :meth:`list_contacts` (minus ``offset``). This endpoint is
        the one exception to "the API always answers JSON": it returns ``text/csv``,
        so this method returns the CSV **as a ``str``** — decoded UTF-8, BOM
        stripped, rows exactly as the API wrote them (quoting and commas intact).
        Nothing here tries to parse JSON.

        Columns: ``phone,name,email,status,source,tags,groups,created_at,
        last_activity_at`` — tags and groups ``;``-joined, timestamps RFC 3339 UTC.

        Args:
            limit: Cap of exported rows (max ``100000``). The API streams the
                result; for a very large base, export in slices by filter and
                write each chunk to disk as it arrives.
            search, project_id, instance_id, tags, tags_match, groups, status,
                city, state, country, zip, document, has_email,
                last_activity_after, last_activity_before, created_after,
                created_before, sort: See :meth:`list_contacts`.

        Returns:
            The CSV document, including its header line (``""`` if the API answers
            an empty body).

        Example:
            >>> csv_text = client.export_contacts(tags=["lead"], status="active")
            >>> with open("contacts.csv", "w", encoding="utf-8", newline="") as fh:
            ...     fh.write(csv_text)
            >>> import csv, io
            >>> next(csv.DictReader(io.StringIO(csv_text)))["phone"]
            '+5511999990000'
        """
        return self._request(
            "GET",
            "/contacts/export",
            params={
                "search": search,
                "project_id": project_id,
                "instance_id": instance_id,
                "limit": limit,
                "tags": tags,
                "tags_match": tags_match,
                "groups": groups,
                "status": status,
                "city": city,
                "state": state,
                "country": country,
                "zip": zip,
                "document": document,
                "has_email": has_email,
                "last_activity_after": last_activity_after,
                "last_activity_before": last_activity_before,
                "created_after": created_after,
                "created_before": created_before,
                "sort": sort,
            },
            accept="text/csv",
            text=True,
        )

    # -- tags and contact groups (dictionaries) --------------------------------

    def list_tags(self) -> JSONDict:
        """List the tag dictionary. ``GET /tags``"""
        return self._request("GET", "/tags")

    def create_tag(
        self,
        key: str,
        *,
        name: Optional[str] = None,
        color: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a tag. ``POST /tags``"""
        body = {"key": key, "name": name, "color": color}
        return self._request("POST", "/tags", body=body, idempotency_key=idempotency_key)

    def delete_tag(self, tag_id: str, *, idempotency_key: Optional[str] = None) -> None:
        """Delete a tag. ``DELETE /tags/{id}``"""
        return self._request(
            "DELETE", f"/tags/{self._seg(tag_id, 'tag_id')}", idempotency_key=idempotency_key
        )

    def list_contact_groups(self) -> JSONDict:
        """List the contact-group dictionary. ``GET /contact-groups``"""
        return self._request("GET", "/contact-groups")

    def create_contact_group(
        self,
        key: str,
        *,
        name: Optional[str] = None,
        color: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a contact group. ``POST /contact-groups``"""
        body = {"key": key, "name": name, "color": color}
        return self._request("POST", "/contact-groups", body=body, idempotency_key=idempotency_key)

    def delete_contact_group(
        self, group_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Delete a contact group. ``DELETE /contact-groups/{id}``"""
        return self._request(
            "DELETE",
            f"/contact-groups/{self._seg(group_id, 'group_id')}",
            idempotency_key=idempotency_key,
        )

    # -- suppression list ------------------------------------------------------

    def list_suppressions(self, *, limit: Optional[int] = None) -> JSONDict:
        """List the suppression list. ``GET /suppressions``"""
        return self._request("GET", "/suppressions", params={"limit": limit})

    def create_suppression(
        self,
        phone: str,
        *,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Add a number to the suppression list (it never receives sends). ``POST /suppressions``"""
        return self._request(
            "POST",
            "/suppressions",
            body={"phone": phone, "reason": reason},
            idempotency_key=idempotency_key,
        )

    def delete_suppression(
        self, phone: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Remove a number from the suppression list. ``DELETE /suppressions?phone=``"""
        return self._request(
            "DELETE", "/suppressions", params={"phone": phone}, idempotency_key=idempotency_key
        )

    # -- projects (numbers, inbox, keys and stats are isolated per project) ----

    def list_projects(self) -> JSONDict:
        """List the account's projects."""
        return self._request("GET", "/projects")

    def create_project(
        self,
        name: str,
        *,
        api_mode: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Create a project (admin).

        Args:
            name: Project name.
            api_mode: ``"UNOFFICIAL"`` (default) or ``"OFFICIAL"`` (WhatsApp
                Cloud API). Immutable after creation.
        """
        return self._request(
            "POST",
            "/projects",
            body={"name": name, "api_mode": api_mode},
            idempotency_key=idempotency_key,
        )

    def get_projects_health(self) -> JSONDict:
        """Number status per project (traffic light). ``GET /projects/health``"""
        return self._request("GET", "/projects/health")

    def update_project(
        self,
        project_id: str,
        name: str,
        *,
        logo_url: Optional[str] = None,
        color: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Update a project (admin). ``PATCH /projects/{id}``"""
        body = {"name": name, "logo_url": logo_url, "color": color}
        return self._request(
            "PATCH",
            f"/projects/{self._seg(project_id, 'project_id')}",
            body=body,
            idempotency_key=idempotency_key,
        )

    def delete_project(
        self, project_id: str, *, idempotency_key: Optional[str] = None
    ) -> None:
        """Delete a project (admin). ``DELETE /projects/{id}``"""
        return self._request(
            "DELETE",
            f"/projects/{self._seg(project_id, 'project_id')}",
            idempotency_key=idempotency_key,
        )

    def get_project_brand(self, project_id: str) -> JSONDict:
        """Brand identity of a specific project's numbers. ``GET /projects/{id}/brand``"""
        return self._request("GET", f"/projects/{self._seg(project_id, 'project_id')}/brand")

    def set_project_brand(
        self,
        project_id: str,
        profile: Mapping[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Save the brand identity of a specific project (admin). ``PUT /projects/{id}/brand``

        Args:
            profile: A BrandProfile dict (same fields as :meth:`set_brand`).
        """
        return self._request(
            "PUT",
            f"/projects/{self._seg(project_id, 'project_id')}/brand",
            body=dict(profile),
            idempotency_key=idempotency_key,
        )

    def upload_project_logo(
        self,
        project_id: str,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Upload the project logo (PNG/JPEG/WebP up to 5 MB, admin). ``POST /projects/{id}/logo``

        Args:
            file: Raw bytes, a path on disk or a binary file object.
        """
        return self._upload(
            f"/projects/{self._seg(project_id, 'project_id')}/logo",
            file,
            filename,
            content_type,
            None,
            idempotency_key,
        )

    # -- brand (numbers' identity — kit lives in the project) ------------------

    def get_brand(self) -> JSONDict:
        """Read the brand identity of the project's numbers."""
        return self._request("GET", "/brand")

    def set_brand(
        self, profile: Mapping[str, Any], *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Update the brand identity of the project's numbers.

        Args:
            profile: A BrandProfile dict (``about``, ``display_name``,
                ``logo_url``, ``website``, ``email``, ``phone``, ``address``,
                ``description``).
        """
        return self._request("PUT", "/brand", body=dict(profile), idempotency_key=idempotency_key)

    def apply_brand(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Apply the "about" text to every connected number of the project."""
        return self._request("POST", "/brand/apply", idempotency_key=idempotency_key)

    def upload_brand_logo(
        self,
        file: FileInput,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Upload the brand logo (multipart). ``POST /brand/logo``

        Args:
            file: Raw bytes, a path on disk or a binary file object.
            filename: File name of the part (default: basename or ``"file"``).
            content_type: MIME type (default: guessed from the file name).
        """
        return self._upload("/brand/logo", file, filename, content_type, None, idempotency_key)

    # -- me / account ----------------------------------------------------------

    def get_me(self) -> JSONDict:
        """Authenticated identity (account, project, key). ``GET /me``"""
        return self._request("GET", "/me")

    def update_profile(
        self,
        *,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        job_title: Optional[str] = None,
        locale: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Update the user profile (name/phone/job title/locale). ``PATCH /me``

        Not the WhatsApp profile of a number — that is :meth:`set_profile`.
        """
        body = {"name": name, "phone": phone, "job_title": job_title, "locale": locale}
        return self._request("PATCH", "/me", body=body, idempotency_key=idempotency_key)

    def update_account(self, name: str, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Rename the account (company name) — admin. ``PATCH /account``"""
        return self._request("PATCH", "/account", body={"name": name}, idempotency_key=idempotency_key)

    def get_health(self) -> JSONDict:
        """API health check. ``GET /healthz``"""
        return self._request("GET", "/healthz")

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
        idempotency_key: Optional[str] = None,
    ) -> JSONDict:
        """Invite a user to the account (admin).

        Args:
            email: User email.
            name: Optional display name.
            role: ``"admin"`` (everything) or ``"agent"`` (member — no billing).
        """
        body = {"email": email, "name": name, "role": role}
        return self._request("POST", "/users", body=body, idempotency_key=idempotency_key)

    def update_user_role(
        self, user_id: str, role: str, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Change a user's role (admin).

        Args:
            user_id: Account user id (path parameter).
            role: ``"admin"`` or ``"agent"``.
        """
        path = f"/users/{self._seg(user_id, 'user_id')}"
        return self._request("PATCH", path, body={"role": role}, idempotency_key=idempotency_key)

    def remove_user(self, user_id: str, *, idempotency_key: Optional[str] = None) -> None:
        """Remove a user from the account (admin).

        Args:
            user_id: Account user id (path parameter).
        """
        path = f"/users/{self._seg(user_id, 'user_id')}"
        return self._request("DELETE", path, idempotency_key=idempotency_key)

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

    # -- plan, add-ons and invoices ---------------------------------------------

    def get_my_entitlements(self) -> JSONDict:
        """Effective account limits (plan + add-ons + usage). ``GET /me/entitlements``"""
        return self._request("GET", "/me/entitlements")

    def get_my_subscription(self) -> Any:
        """Plan/subscription state (``None`` on Free). ``GET /me/subscription``"""
        return self._request("GET", "/me/subscription")

    def upgrade_plan(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Put the Pro plan in the cart (pending until paid). ``POST /me/plan/upgrade``"""
        return self._request("POST", "/me/plan/upgrade", idempotency_key=idempotency_key)

    def cancel_plan(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Cancel Pro at the end of the current cycle. ``POST /me/plan/cancel``"""
        return self._request("POST", "/me/plan/cancel", idempotency_key=idempotency_key)

    def uncancel_plan(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Undo a scheduled Pro cancellation. ``POST /me/plan/uncancel``"""
        return self._request("POST", "/me/plan/uncancel", idempotency_key=idempotency_key)

    def change_addon(
        self, kind: str, delta: int, *, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Add (``delta > 0``) or remove add-ons in the cart. ``POST /me/addons``

        Args:
            kind: ``number``, ``project``, ``storage_gb``, ``retention_block``,
                ``campaigns`` or ``schedule_year``.
            delta: Units to add (positive) or remove (negative).
        """
        return self._request(
            "POST", "/me/addons", body={"kind": kind, "delta": delta}, idempotency_key=idempotency_key
        )

    def get_addon_cart(self) -> JSONDict:
        """Cart state (pending Pro/add-ons + prorated total). ``GET /me/addons/cart``"""
        return self._request("GET", "/me/addons/cart")

    def clear_addon_cart(self, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Empty the cart. ``DELETE /me/addons/cart``"""
        return self._request("DELETE", "/me/addons/cart", idempotency_key=idempotency_key)

    def checkout_addon_cart(
        self, *, save_card: Optional[bool] = None, idempotency_key: Optional[str] = None
    ) -> JSONDict:
        """Pay the cart: creates the invoice and opens the in-app payment. ``POST /me/addons/cart/checkout``"""
        return self._request(
            "POST",
            "/me/addons/cart/checkout",
            body={"save_card": save_card},
            idempotency_key=idempotency_key,
        )

    def list_my_invoices(self) -> JSONDict:
        """The account's invoices (latest 24, newest first). ``GET /me/invoices``"""
        return self._request("GET", "/me/invoices")

    def pay_invoice(self, invoice_id: str, *, idempotency_key: Optional[str] = None) -> JSONDict:
        """Reopen the in-app payment of an open invoice. ``POST /me/invoices/{id}/pay``"""
        return self._request(
            "POST",
            f"/me/invoices/{self._seg(invoice_id, 'invoice_id')}/pay",
            idempotency_key=idempotency_key,
        )

    def get_billing_config(self) -> JSONDict:
        """Stripe publishable key for a front-end checkout. ``GET /billing/config``"""
        return self._request("GET", "/billing/config")

    def get_pricing(self) -> JSONDict:
        """Public rate card per currency (plans, add-ons, free allowances). ``GET /pricing``"""
        return self._request("GET", "/pricing")
