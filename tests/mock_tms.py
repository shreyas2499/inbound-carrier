"""A scriptable in-process TMS over a real TCP socket, for testing the client.

The handler you pass receives each decoded request line and returns a
(bytes_to_send, hold_seconds) tuple, which lets a test reproduce every fault:

    normal              (full_response_bytes, 0)
    partial response    (truncated_bytes,     0)   # prefix, then close, no END
    malformed framing   (bad_bytes,           0)
    delayed termination (full_response_bytes, 2.0) # send, then hold the socket
    timeout             (b"",                 2.0) # send nothing, just hold

Each connection is handled once (the protocol is one-request-per-connection).
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

Handler = Callable[[str], "tuple[bytes, float]"]


class MockTms:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.host, self.port = self._sock.getsockname()
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "tuple[str, int]":
        self._thread.start()
        return self.host, self.port

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            send, hold = self.handler(data.decode("ascii", "replace"))
            if send:
                conn.sendall(send)
            if hold:
                time.sleep(hold)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
