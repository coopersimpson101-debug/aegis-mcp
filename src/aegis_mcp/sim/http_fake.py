"""Tiny framework for fake HTTP devices.

A :class:`FakeApp` is a plain object with a ``handle(FakeRequest) -> (status, body)``
method. It can be mounted in tests as an ``httpx.MockTransport`` (no sockets) or served
for real with :meth:`FakeApp.serve` so you can point the MCP servers - or a browser - at it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


@dataclass
class FakeRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    raw_query: str
    headers: dict[str, str]
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body or b"{}")

    def q(self, key: str, default: str | None = None) -> str | None:
        return self.query.get(key, [default])[0]

    def form(self) -> dict[str, Any]:
        """Parse multipart/form-data or urlencoded bodies. Files come back as (filename, bytes)."""
        ctype = self.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + self.body
            )
            out: dict[str, Any] = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                out[name] = (fname, payload) if fname else payload.decode("utf-8", "replace")
            return out
        if ctype.startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(self.body.decode()).items()}
        return {}


Response = tuple[int, Any]  # body: dict/list -> JSON, bytes -> raw, str -> text


@dataclass
class FakeApp:
    api_key: str | None = None
    requests: list[FakeRequest] = field(default_factory=list)

    def handle(self, req: FakeRequest) -> Response:  # pragma: no cover - overridden
        return 404, {"error": "not found"}

    def authorized(self, req: FakeRequest) -> bool:
        return self.api_key is None or req.headers.get("x-api-key") == self.api_key

    def dispatch(self, req: FakeRequest) -> tuple[int, bytes, str]:
        self.requests.append(req)
        status, body = self.handle(req)
        if isinstance(body, bytes):
            return status, body, "image/jpeg" if body[:2] == b"\xff\xd8" else "application/octet-stream"
        if isinstance(body, str):
            return status, body.encode(), "text/plain"
        return status, json.dumps(body).encode(), "application/json"

    # -- in-process transport for tests ------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            u = urlsplit(str(request.url))
            req = FakeRequest(request.method, u.path, parse_qs(u.query, keep_blank_values=True), u.query,
                              {k.lower(): v for k, v in request.headers.items()}, request.read())
            status, body, ctype = self.dispatch(req)
            return httpx.Response(status, content=body, headers={"content-type": ctype})
        return httpx.MockTransport(handler)

    # -- real socket server ----------------------------------------------------------
    def serve(self, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self) -> None:
                u = urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                req = FakeRequest(self.command, u.path, parse_qs(u.query, keep_blank_values=True), u.query,
                                  {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(n) if n else b"")
                status, body, ctype = app.dispatch(req)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _do

            def log_message(self, *a: Any) -> None:
                pass

        srv = ThreadingHTTPServer((host, port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t


TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c"
    "20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f000001050101010101010000"
    "0000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191"
    "a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a"
    "737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8"
    "d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)
"""Small JPEG-framed (SOI...EOI) byte string used as the fake webcam frame."""
