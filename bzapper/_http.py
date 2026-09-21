"""HTTP plumbing shared by :class:`~bzapper.Client` and :class:`~bzapper.PartnerClient`.

Padrão Berni Software (BRIEF §3–5): path-segment encoding, query encoding, retry
timing, error translation and multipart bodies. Standard library only; nothing
here touches the network on import.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import random
import urllib.error
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import IO, Any, Dict, Mapping, Optional, Tuple, Union

from .errors import BzapperError, NetworkError, error_for_status

DEFAULT_TIMEOUT = 30.0
#: New attempts after the first one (network error/timeout, 429, 502, 503, 504).
DEFAULT_MAX_RETRIES = 2

RETRY_STATUSES = frozenset({429, 502, 503, 504})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_RETRY_AFTER = 60.0
MAX_BACKOFF = 8.0

# Transport failures that become NetworkError (and are retried). ``socket.timeout`` and
# ``ConnectionError`` are OSError; ``RemoteDisconnected``/``IncompleteRead`` are HTTPException.
NETWORK_FAILURES = (urllib.error.URLError, OSError, http.client.HTTPException)

#: A file for the multipart uploads: raw bytes, a path on disk or a binary file object.
FileInput = Union[bytes, bytearray, str, "os.PathLike[str]", IO[bytes]]


# -- encoding ----------------------------------------------------------------


def path_segment(value: Any, name: str) -> str:
    """Percent-encode ONE path segment (``abc 1`` → ``abc%201``, ``/`` → ``%2F``).

    Raises:
        ValueError: when the value is missing, empty, ``"."`` or ``".."`` — before any
            request is made (the HTTP stack would resolve ``..`` and call another route).
    """
    if value is None:
        raise ValueError(f"{name} is required.")
    text = str(value)
    if text == "":
        raise ValueError(f"{name} must not be empty.")
    if text in (".", ".."):
        raise ValueError(f"{name} must not be {text!r}.")
    # '@' and ':' are valid path characters: JIDs (5511...@s.whatsapp.net) stay readable.
    return urllib.parse.quote(text, safe="@:")


def iso_utc(value: datetime) -> str:
    """``datetime`` → ISO 8601 in UTC with ``Z``. Naive = already UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None).isoformat() + "Z"


def query_value(value: Any) -> str:
    """One query value on the wire: booleans ``true``/``false``, dates ISO-8601 UTC,
    lists as CSV (``style: form, explode: false`` in the spec)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat() + "T00:00:00Z"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(query_value(v) for v in value)
    return str(value)


def build_query(params: Optional[Mapping[str, Any]]) -> str:
    """``{k: v}`` → ``k=v&...`` without the parameters that were not given (``None``)."""
    if not params:
        return ""
    pairs = [(k, query_value(v)) for k, v in params.items() if v is not None]
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote) if pairs else ""


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def encode_json(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, default=json_default).encode("utf-8")


def read_file(
    file: FileInput,
    filename: Optional[str],
    content_type: Optional[str],
) -> Tuple[bytes, str, str]:
    """Normalize a :data:`FileInput` into ``(content, filename, content_type)``."""
    if isinstance(file, (bytes, bytearray)):
        content = bytes(file)
        name = filename or "file"
    elif isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        with open(path, "rb") as fh:
            content = fh.read()
        name = filename or os.path.basename(path)
    elif hasattr(file, "read"):
        content = file.read()
        if isinstance(content, str):
            raise TypeError("open the file in binary mode ('rb').")
        name = filename or os.path.basename(str(getattr(file, "name", "") or "")) or "file"
    else:
        raise TypeError("file must be bytes, a path or a binary file object.")
    ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return content, name, ctype


def encode_multipart(
    fields: Optional[Mapping[str, Any]],
    file_field: str,
    content: bytes,
    filename: str,
    content_type: str,
) -> Tuple[bytes, str]:
    """``multipart/form-data`` body → ``(bytes, Content-Type header)``."""
    boundary = "bzapper-" + uuid.uuid4().hex
    out = bytearray()
    for key, value in (fields or {}).items():
        if value is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{_quote_param(key)}"\r\n\r\n'.encode()
        out += query_value(value).encode("utf-8") + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="{_quote_param(file_field)}"; '
        f'filename="{_quote_param(filename)}"\r\n'
    ).encode("utf-8")
    out += f"Content-Type: {content_type}\r\n\r\n".encode()
    out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _quote_param(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


# -- retries -----------------------------------------------------------------


def parse_retry_after(raw: Optional[str]) -> Optional[float]:
    """``Retry-After`` in seconds (number or HTTP date); ``None`` when absent/invalid."""
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds != seconds:  # NaN
        return None
    seconds = max(0.0, seconds)
    return int(seconds) if float(seconds).is_integer() else seconds


def backoff(attempt: int) -> float:
    """``min(8, 0.5 × 2^attempt)`` seconds + up to 25% jitter."""
    base = min(MAX_BACKOFF, 0.5 * (2 ** attempt))
    return base + random.uniform(0, base * 0.25)


def retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    seconds = parse_retry_after(retry_after)
    if seconds is not None:
        return min(float(seconds), MAX_RETRY_AFTER)
    return backoff(attempt)


# -- responses and errors ------------------------------------------------------


def header(headers: Any, name: str) -> Optional[str]:
    """Case-insensitive header lookup (``HTTPMessage`` or a plain dict)."""
    if not headers:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is not None:
        return value
    if isinstance(headers, Mapping):
        lower = name.lower()
        for key, val in headers.items():
            if str(key).lower() == lower:
                return val
    return None


def decode_json(raw: bytes) -> Tuple[Any, str, bool]:
    """``(payload, text, is_json)``; empty body → ``(None, "", False)``."""
    text = raw.decode("utf-8", "replace") if raw else ""
    if not text.strip():
        return None, text, False
    try:
        return json.loads(text), text, True
    except ValueError:
        return None, text, False


def build_error(
    status: int,
    headers: Any,
    raw: bytes,
    sent_request_id: Optional[str] = None,
) -> BzapperError:
    """Non-2xx response → the typed error (BRIEF §4)."""
    payload, text, _ = decode_json(raw)
    code: Optional[str] = None
    message: Optional[str] = None
    locale: Optional[str] = None
    if isinstance(payload, dict):
        for key in ("code", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                code = value
                break
        msg = payload.get("message")
        if isinstance(msg, str) and msg:
            message = msg
        loc = payload.get("locale")
        if isinstance(loc, str) and loc:
            locale = loc
    if not code:
        code = f"HTTP_{status}"
    if not message:
        message = code
    request_id = header(headers, "X-Request-Id") or sent_request_id or None
    retry_after = parse_retry_after(header(headers, "Retry-After")) if status == 429 else None
    required_scope = (header(headers, "X-Required-Scope") or None) if status == 403 else None
    cls = error_for_status(status)
    return cls(
        code,
        message,
        status,
        locale,
        request_id=request_id,
        retry_after=retry_after,
        required_scope=required_scope,
        body=payload if payload is not None else (text or None),
    )


def decode_success(status: int, headers: Any, raw: bytes, sent_request_id: str) -> Any:
    """2xx → decoded JSON (``None`` for an empty body). Non-JSON → ``INVALID_RESPONSE``."""
    payload, text, is_json = decode_json(raw)
    if is_json:
        return payload
    if not text.strip():
        return None
    shown = text.strip()[:200]
    raise BzapperError(
        "INVALID_RESPONSE",
        f"the API answered HTTP {status} with a body that is not JSON: {shown!r}",
        status,
        request_id=header(headers, "X-Request-Id") or sent_request_id,
        body=text,
    )


def network_error(base_url: str, exc: BaseException, request_id: str) -> NetworkError:
    reason = getattr(exc, "reason", None) or exc
    return NetworkError(
        "NETWORK_ERROR",
        f"could not reach {base_url} ({type(exc).__name__}: {reason})",
        0,
        request_id=request_id,
    )
