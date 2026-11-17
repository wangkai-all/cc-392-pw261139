"""Shared plumbing for the three independent services.

Only the Python standard library is used so the project runs from a clean
checkout without installing packages.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple

MAX_BODY_BYTES = 32 * 1024 * 1024


# ---------------------------------------------------------------------------
# Errors and responses
# ---------------------------------------------------------------------------
class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


class Response:
    def __init__(
        self,
        payload: Any = None,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        raw: Optional[bytes] = None,
        content_type: str = "application/json; charset=utf-8",
    ):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.raw = raw
        self.content_type = content_type


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=False).encode("utf-8")


def _header_safe(value: str) -> str:
    """HTTP headers are latin-1 on the wire; keep them ASCII and unambiguous."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return urllib.parse.quote(value, safe="/:;=._- ")


# ---------------------------------------------------------------------------
# HTTP service base
# ---------------------------------------------------------------------------
class Service:
    """Small regex router shared by all services."""

    name = "service"

    def __init__(self) -> None:
        self._routes: List[Tuple[str, Pattern[str], Callable[..., Any]]] = []

    def route(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method.upper(), re.compile(pattern), handler))

    def dispatch(self, method: str, path: str, query: Dict[str, List[str]], body: Any) -> Response:
        if method == "OPTIONS":
            return Response(payload={}, status=204)
        try:
            for http_method, regex, handler in self._routes:
                if http_method != method:
                    continue
                match = regex.fullmatch(path)
                if match:
                    return handler(match, query, body)
            raise AppError(404, "not_found", f"No route for {method} {path}")
        except AppError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise AppError(500, "internal_error", f"{type(exc).__name__}: {exc}") from exc

    def make_handler(self) -> type:
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"offset-lab-{service.name}/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:  # quiet tests
                if os.environ.get("OFFSET_LAB_LOG", "") == "1":
                    super().log_message(fmt, *args)

            def _send(self, response: Response) -> None:
                if response.raw is not None:
                    body = response.raw
                else:
                    body = json_bytes(response.payload if response.payload is not None else {})
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Expose-Headers",
                                 "X-Source-Revision, X-Source-Sha256, X-Source-Bytes, X-Source-Chars")
                for key, value in response.headers.items():
                    self.send_header(key, _header_safe(value))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _handle(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parsed.path)
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                body: Any = None
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY_BYTES:
                        raise AppError(413, "body_too_large", "Request body exceeds limit")
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        try:
                            body = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise AppError(400, "invalid_json", f"Invalid JSON body: {exc}")
                    response = service.dispatch(method, path, query, body)
                    self._send(response)
                except AppError as exc:
                    self._send(Response(payload=exc.to_dict(), status=exc.status))
                except Exception as exc:  # pragma: no cover
                    self._send(
                        Response(
                            payload={
                                "error": {
                                    "code": "internal_error",
                                    "message": f"{type(exc).__name__}: {exc}",
                                }
                            },
                            status=500,
                        )
                    )

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_OPTIONS(self) -> None:
                self._handle("OPTIONS")

        return Handler


def serve(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), service.make_handler())
    server.daemon_threads = True
    return server


def q1(query: Dict[str, List[str]], key: str, default: Optional[str] = None) -> Optional[str]:
    values = query.get(key)
    return values[0] if values else default


def int_param(value: Any, name: str, default: Optional[int] = None) -> int:
    if value is None or value == "":
        if default is not None:
            return default
        raise AppError(422, "invalid_parameter", f"{name} is required")
    if isinstance(value, bool):
        raise AppError(422, "invalid_parameter", f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise AppError(422, "invalid_parameter", f"{name} must be an integer")
    return result


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Cross-service HTTP client
# ---------------------------------------------------------------------------
class RemoteError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _remote_request(url: str, method: str = "GET", payload: Any = None, timeout: float = 15.0):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json_bytes(payload)
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = None
        message = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(message)
            code = parsed.get("error", {}).get("code")
            message = parsed.get("error", {}).get("message", message)
        except Exception:
            pass
        raise RemoteError(message, status=exc.code, code=code) from exc
    except urllib.error.URLError as exc:
        raise RemoteError(f"upstream unavailable: {exc.reason}") from exc


def remote_json(url: str, method: str = "GET", payload: Any = None, timeout: float = 15.0) -> Any:
    status, _headers, raw = _remote_request(url, method=method, payload=payload, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RemoteError(f"upstream returned invalid JSON ({status})") from exc


def remote_bytes(url: str, timeout: float = 30.0) -> Tuple[bytes, Dict[str, str]]:
    _status, headers, raw = _remote_request(url, timeout=timeout)
    return raw, headers


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def utf8_prefix(text: str) -> List[int]:
    """byte_offsets[i] = UTF-8 byte offset of code point i (i may equal len)."""
    offsets = [0] * (len(text) + 1)
    total = 0
    for index, char in enumerate(text):
        code = ord(char)
        if code < 0x80:
            total += 1
        elif code < 0x800:
            total += 2
        elif code < 0x10000:
            total += 3
        else:
            total += 4
        offsets[index + 1] = total
    return offsets


def decode_utf8(raw: bytes, what: str = "content") -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError(
            422,
            "invalid_utf8",
            f"{what} is not valid UTF-8 (byte {exc.start}); retain source bytes and fix the input",
            {"byte_offset": exc.start, "reason": exc.reason},
        ) from exc


def line_ending_stats(text: str) -> Dict[str, int]:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return {"crlf": crlf, "lf": lf, "cr": cr}
