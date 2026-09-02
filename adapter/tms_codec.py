"""TMS legacy protocol codec — request encoding and response parsing.

Grounded in the HappyRobot Legacy TMS Protocol Reference (doc HR-LTMS-PR-001).
See docs/tms-protocol.md for the distilled spec and the source transcripts.

WIRE FORMAT
  * Transport TCP, ASCII, lines terminated with CRLF, 4096-byte max frame,
    one request per connection, 30s server idle timeout.
  * Request:  CMD:<cmd>|AUTH:<token>|<KEY>:<VALUE>|...   (CMD first, AUTH always;
    '|' and CR/LF illegal in values; unknown fields are accepted and discarded.)
  * Success:  zero or more record lines, then a terminator line 'END'.
  * Error:    ERR|CODE:<code>|MSG:<msg>
  * A DEBUG_ECHO success line leads with a bare 'ECHO' marker, then KEY:VALUE.
  * Record fields are space-padded on the right; we strip on the way in.
  * Field order is NOT guaranteed stable across server builds, so we parse by
    key, never by position.
  * RATE / MAX_BUY / WEIGHT / PIECES / MILES: zero-padded integers.
  * PICKUP_DT / DELIVERY_DT / TIMESTAMP: YYYYMMDDHHMMSS.

A success frame missing its END terminator is a FramingError — that is exactly
how the 'partial response' fault is caught on the wire (see docs/tms-protocol.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CRLF = "\r\n"
TERMINATOR = "END"

# Structured error codes seen in practice. NOT exhaustive (spec: do not assume).
TMS_ERROR_CODES = {
    "AUTH_FAILED", "UNKNOWN_CMD", "MISSING_FIELD", "UNKNOWN_LOAD",
    "ALREADY_BOOKED", "INVALID_RATE", "MALFORMED", "SERVER_ERROR",
}

# Zero-padded whole-dollar fields. MAX_BUY is the hidden ceiling: parsed here,
# but the service layer must keep it server-side and never surface it.
_MONEY_FIELDS = {"RATE", "MAX_BUY"}
# Other zero-padded integer fields.
_INT_FIELDS = {"WEIGHT", "PIECES", "MILES"}
# YYYYMMDDHHMMSS timestamp fields.
_DATETIME_FIELDS = {"PICKUP_DT", "DELIVERY_DT", "TIMESTAMP"}


class ProtocolError(Exception):
    """A request we refused to send, or a value we could not encode/parse."""


class FramingError(ProtocolError):
    """The response was not a well-formed frame (missing END, bad token, etc.).

    Retryable on a fresh connection — this is the partial / malformed fault.
    """


class TmsError(Exception):
    """The TMS returned a structured ERR response (a clean, non-retryable no)."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


def _reject_delimiters(key: str, value: Any) -> str:
    s = str(value)
    if "|" in s or "\r" in s or "\n" in s:
        raise ProtocolError(f"illegal delimiter char in value for {key!r}")
    return s


def encode_request(cmd: str, auth: str, **fields: Any) -> bytes:
    """Build one request line: CMD first, AUTH second, then fields, CRLF-terminated.

    None-valued fields are omitted. Keys are upper-cased.
    """
    if not cmd:
        raise ProtocolError("cmd is required")
    if not auth:
        raise ProtocolError("auth token is required")
    parts = [
        f"CMD:{_reject_delimiters('CMD', cmd)}",
        f"AUTH:{_reject_delimiters('AUTH', auth)}",
    ]
    for k, v in fields.items():
        if v is None:
            continue
        key = k.upper()
        parts.append(f"{key}:{_reject_delimiters(key, v)}")
    line = "|".join(parts) + CRLF
    if len(line.encode("ascii")) > 4096:
        raise ProtocolError("request exceeds 4096-byte frame limit")
    return line.encode("ascii")


def _parse_int(raw: str) -> int:
    s = raw.strip()
    if not s.isdigit():
        raise ProtocolError(f"expected zero-padded integer, got {raw!r}")
    return int(s)


def _parse_dt(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y%m%d%H%M%S")


def _coerce(key: str, value: str) -> Any:
    if key in _MONEY_FIELDS or key in _INT_FIELDS:
        return _parse_int(value)
    if key in _DATETIME_FIELDS:
        return _parse_dt(value)
    return value.strip()


def _tokenize(line: str) -> dict[str, Any]:
    """Parse one '|'-delimited record line into a dict.

    A leading bare marker (e.g. 'ECHO') is captured under '_MARKER'; any other
    token without a ':' is a framing violation and raises FramingError.
    """
    record: dict[str, Any] = {}
    for i, token in enumerate(line.split("|")):
        if not token:
            continue
        key, sep, value = token.partition(":")
        if not sep:
            if i == 0:
                record["_MARKER"] = key.strip()
                continue
            raise FramingError(f"malformed field token {token!r}")
        key = key.strip()
        record[key] = _coerce(key, value)
    return record


@dataclass
class ParsedResponse:
    records: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""


def parse_response(raw: str) -> ParsedResponse:
    """Parse a complete response frame.

    Raises TmsError on a structured ERR line, FramingError on an incomplete /
    malformed frame (including a success frame missing its END terminator).
    """
    lines = raw.replace(CRLF, "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise FramingError("empty response")

    if lines[0].startswith("ERR"):
        err = _tokenize(lines[0][len("ERR"):].lstrip("|"))
        raise TmsError(err.get("CODE", "SERVER_ERROR"), err.get("MSG", ""))

    if lines[-1].strip() != TERMINATOR:
        raise FramingError("missing END terminator — incomplete or truncated response")

    records = [_tokenize(ln) for ln in lines[:-1] if ln.strip()]
    return ParsedResponse(records=records, raw=raw)
