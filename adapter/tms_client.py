"""TMS socket client — the transport + resilience layer.

Opens one TCP connection per request (the protocol does not support reuse),
sends an encoded request via tms_codec, reads the framed response, and turns the
four injected fault categories into typed, bounded behaviour:

  * timeout             no bytes before the client deadline   -> TmsUnavailable
  * partial response    connection closes with no END         -> FramingError
  * malformed framing   bad token / overlong frame            -> FramingError
  * delayed termination full response then socket held open    -> handled by
    returning as soon as END (or an ERR line) is seen, so we never wait on the
    close.

Reads and searches retry on a FRESH connection up to max_retries. LOAD_BOOK
never blind-retries: a booking is monotonic per token and a timed-out book may
have committed server-side, so an ambiguous book raises BookAmbiguousError for
the caller to resolve with a LOAD_GET status check.
"""
from __future__ import annotations

import socket
import time
from typing import Any

from adapter.tms_codec import (
    CRLF, TERMINATOR, encode_request, parse_response,
    ParsedResponse, TmsError, FramingError,
)

_MAX_FRAME = 4096


class TmsUnavailable(Exception):
    """Transient transport failure (timeout, refused, reset) — retryable."""


class BookAmbiguousError(Exception):
    """A LOAD_BOOK could not be confirmed; it may or may not have committed."""

    def __init__(self, load_id: str, cause: Exception | None = None) -> None:
        self.load_id = load_id
        self.cause = cause
        super().__init__(f"booking ambiguous for {load_id}: {cause}")


def _response_complete(text: str) -> bool:
    """True once the buffer holds a terminated response: an END line, or an ERR line."""
    if text.startswith("ERR") and CRLF in text:
        return True
    return any(line == TERMINATOR for line in text.split(CRLF))


class TmsClient:
    def __init__(self, host: str, port: int, token: str, *,
                 timeout: float = 8.0, max_retries: int = 2, backoff_base: float = 0.2) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = timeout          # must stay well under the TMS 30s idle timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    # --- public commands ---------------------------------------------------

    def debug_echo(self, msg: str) -> ParsedResponse:
        return self._request("DEBUG_ECHO", MSG=msg)

    def load_query(self, **filters: Any) -> list[dict]:
        return self._request("LOAD_QUERY", **filters).records

    def load_get(self, load_id: str) -> dict | None:
        recs = self._request("LOAD_GET", LOAD_ID=load_id).records
        return recs[0] if recs else None

    def load_book(self, load_id: str, mc_num: str, agreed_rate: int) -> dict:
        try:
            recs = self._request(
                "LOAD_BOOK", retryable=False,
                LOAD_ID=load_id, MC_NUM=mc_num, AGREED_RATE=agreed_rate,
            ).records
        except (TmsUnavailable, FramingError) as e:
            # Cannot tell whether the book committed — never blind-retry.
            raise BookAmbiguousError(load_id, e) from e
        return recs[0] if recs else {}

    # --- transport ---------------------------------------------------------

    def _request(self, cmd: str, *, retryable: bool = True, **fields: Any) -> ParsedResponse:
        attempts = self.max_retries + 1 if retryable else 1
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self._single_request(cmd, **fields)
            except TmsError:
                raise  # a clean structured 'no' — not a fault, never retry
            except (TmsUnavailable, FramingError) as e:
                last = e
                if i + 1 < attempts and self.backoff_base:
                    time.sleep(self.backoff_base * (2 ** i))
        assert last is not None
        raise last

    def _single_request(self, cmd: str, **fields: Any) -> ParsedResponse:
        payload = encode_request(cmd, self.token, **fields)
        buffer = b""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(payload)
                while True:
                    try:
                        chunk = sock.recv(_MAX_FRAME)
                    except socket.timeout as e:
                        raise TmsUnavailable(f"{cmd}: no response within {self.timeout}s") from e
                    if not chunk:
                        break  # server closed the connection (possible partial fault)
                    buffer += chunk
                    if len(buffer) > _MAX_FRAME * 4:
                        raise FramingError("response exceeds sane frame bound")
                    if _response_complete(buffer.decode("ascii", "replace")):
                        break
        except OSError as e:
            # connect refused / reset / timeout on connect
            raise TmsUnavailable(f"{cmd}: transport error: {e}") from e
        return parse_response(buffer.decode("ascii", "replace"))
