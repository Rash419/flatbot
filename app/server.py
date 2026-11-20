"""The only inbound endpoint: phones POST bank SMS to /sms/<token>.

Privacy: request bodies, SMS text and tokens are never logged. Request logs show
the method, a redacted path and the status code only.

Replies: {"status": "queued"} (200) when the SMS was accepted for reading,
"ignored" (200) for messages with no amount or OTPs, "duplicate" (200) for repeats,
"unknown" (404) for a bad token.
"""

from __future__ import annotations

import json
import logging
import socketserver
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .telegram import TelegramError

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
TEXT_KEYS = ("text", "message", "body", "msg", "content", "sms")


def _first_text(mapping: dict) -> str:
    for key in TEXT_KEYS:
        value = mapping.get(key)
        if isinstance(value, list):  # parse_qs gives lists
            value = next((v for v in value if isinstance(v, str) and v.strip()), None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def extract_text(body: bytes, content_type: str = "") -> str:
    """Pull the SMS text out of whatever a forwarder app sent. Returns "" if there is none."""
    raw = body.decode("utf-8", "replace").strip()
    ctype = content_type.split(";")[0].strip().lower()

    if raw.startswith("{") or ctype == "application/json" or ctype.endswith("+json"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return _first_text(data).strip()
        if isinstance(data, str):
            return data.strip()
        if data is not None:
            return ""  # a JSON number, list, null: nothing usable
        # Not valid JSON after all: fall through and treat it as plain text.

    if ctype == "application/x-www-form-urlencoded":
        return _first_text(parse_qs(raw, keep_blank_values=False)).strip()

    return raw


def _redact(path: str) -> str:
    path = path.split("?", 1)[0]
    if path.startswith("/sms/"):
        return "/sms/<redacted>"
    if path == "/health":
        return path
    return "<other>"


class _Handler(BaseHTTPRequestHandler):
    server_version = "expense-bot"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # a stalled client must not hold a thread forever

    bot = None  # set by make_server on a subclass

    # Logging: the default handler logs the full request line (token included). Replace it.
    def log_message(self, format, *args):  # noqa: A002 - signature from the base class
        pass

    def log_request(self, code="-", size="-"):
        path = getattr(self, "path", "")
        # The container health check hits /health every 30 s; keep it out of the INFO log.
        level = logging.DEBUG if path == "/health" and code == 200 else logging.INFO
        log.log(level, "%s %s %s", getattr(self, "command", "-"), _redact(path), int(code) if code != "-" else code)

    def _reply(self, code: int, status: str, close: bool = False) -> None:
        payload = json.dumps({"status": status}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _discard_body(self) -> None:
        """Read (and drop) a small body so a keep-alive connection stays in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)
        elif length:
            self.close_connection = True

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._reply(200, "ok")
        else:
            self._discard_body()
            self._reply(404, "not found", close=self.close_connection)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        token = path[len("/sms/"):] if path.startswith("/sms/") else ""
        if not token or "/" in token:
            self._discard_body()
            self._reply(404, "not found", close=self.close_connection)
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._reply(411, "length required", close=True)
            return
        try:
            length = int(length_header)
            if length < 0:
                raise ValueError
        except ValueError:
            self._reply(400, "bad content-length", close=True)
            return
        if length > MAX_BODY:
            self._reply(413, "too large", close=True)
            return

        body = self.rfile.read(length) if length else b""
        text = extract_text(body, self.headers.get("Content-Type", ""))
        if not text:
            self._reply(400, "no text")
            return

        try:
            status = self.bot.ingest_sms(token, text)
        except TelegramError as e:
            # ingest_sms only queues now, so this should not happen; kept so a failure that
            # does reach Telegram tells the phone app to retry (the bot deduplicates).
            log.warning("SMS webhook: Telegram error: %s", e)
            self._reply(502, "telegram unavailable")
            return
        except Exception as e:
            # Log the stack but not the exception message, which could quote the SMS.
            log.error("SMS webhook failed with %s:\n%s", type(e).__name__,
                      "".join(traceback.format_tb(e.__traceback__)).rstrip())
            self._reply(500, "internal error")
            return

        self._reply(404 if status == "unknown" else 200, status)

    def _not_found(self):
        self._discard_body()
        self._reply(404, "not found", close=self.close_connection)

    do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_found


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that can stall
        # startup for seconds on a box without working DNS. The name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(bot, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server. Use port=0 for an ephemeral port."""
    handler = type("Handler", (_Handler,), {"bot": bot})
    return _Server((host, port), handler)
