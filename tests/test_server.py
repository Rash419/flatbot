import contextlib
import http.client
import io
import json
import logging
import threading
import unittest
from urllib.parse import urlencode

from app.config import ConfigError, load_config
from app.server import MAX_BODY, make_server
from app.telegram import TelegramError

TOKEN = "s3cr3t-token-abc123"
SMS = "Sent Rs.450.00 From HDFC Bank A/C *1234 To BLINKIT On 20/11/26 Ref 123456789012"


class StubBot:
    def __init__(self):
        self.calls = []
        self.result = "queued"
        self.error = None

    def ingest_sms(self, token, text):
        self.calls.append((token, text))
        if self.error:
            raise self.error
        return "unknown" if token != TOKEN else self.result


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = StubBot()
        cls.server = make_server(cls.bot, 0, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.bot.calls.clear()
        self.bot.result = "queued"
        self.bot.error = None
        self.capture = _Capture()
        self.capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.stderr = io.StringIO()
        self._redirect = contextlib.redirect_stderr(self.stderr)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)
        root = logging.getLogger()
        root.removeHandler(self.capture)
        root.setLevel(self._old_level)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, json.loads(data) if data else None
        finally:
            conn.close()

    def post_json(self, obj, path=f"/sms/{TOKEN}", ctype="application/json"):
        return self.request("POST", path, json.dumps(obj).encode(), {"Content-Type": ctype})

    # --- routes ---

    def test_health(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"status": "ok"}))

    def test_json_body(self):
        self.assertEqual(self.post_json({"from": "HDFCBK", "text": SMS}), (200, {"status": "queued"}))
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_alternate_json_key(self):
        self.assertEqual(self.post_json({"sender": "HDFCBK", "message": SMS}), (200, {"status": "queued"}))
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_json_key_order_and_non_strings(self):
        self.post_json({"text": 5, "body": "", "msg": SMS, "sms": "other"})
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_form_body(self):
        body = urlencode({"from": "HDFCBK", "body": SMS}).encode()
        status = self.request("POST", f"/sms/{TOKEN}", body,
                              {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, (200, {"status": "queued"}))
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_plain_text_body(self):
        status = self.request("POST", f"/sms/{TOKEN}", SMS.encode(), {"Content-Type": "text/plain"})
        self.assertEqual(status, (200, {"status": "queued"}))
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_json_with_wrong_content_type(self):
        self.assertEqual(self.post_json({"text": SMS}, ctype="text/plain"), (200, {"status": "queued"}))
        self.assertEqual(self.bot.calls, [(TOKEN, SMS)])

    def test_other_statuses_are_200(self):
        for result in ("ignored", "duplicate"):
            self.bot.result = result
            self.assertEqual(self.post_json({"text": SMS}), (200, {"status": result}))

    def test_unknown_token(self):
        self.assertEqual(self.post_json({"text": SMS}, path="/sms/nope"), (404, {"status": "unknown"}))

    def test_empty_text(self):
        self.assertEqual(self.post_json({"text": "   "})[0], 400)
        self.assertEqual(self.post_json({"from": "HDFCBK"})[0], 400)
        self.assertEqual(self.request("POST", f"/sms/{TOKEN}", b"", {"Content-Length": "0"})[0], 400)
        self.assertEqual(self.bot.calls, [])

    def test_oversized_body(self):
        status, _ = self.request("POST", f"/sms/{TOKEN}", b"x" * (MAX_BODY + 1))
        self.assertEqual(status, 413)
        self.assertEqual(self.bot.calls, [])

    def test_oversized_body_is_not_read(self):
        # Claim a huge body but send none: the server must answer without waiting for it.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", f"/sms/{TOKEN}")
            conn.putheader("Content-Length", str(50 * 1024 * 1024))
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 413)
        finally:
            conn.close()

    def test_missing_content_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", f"/sms/{TOKEN}")
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 411)
        finally:
            conn.close()

    def test_telegram_error_is_502(self):
        self.bot.error = TelegramError("sendMessage: could not reach Telegram")
        self.assertEqual(self.post_json({"text": SMS})[0], 502)

    def test_unexpected_error_is_500(self):
        self.bot.error = ValueError(f"boom while handling {SMS}")
        self.assertEqual(self.post_json({"text": SMS}), (500, {"status": "internal error"}))
        logged = "\n".join(self.capture.lines)
        self.assertIn("ValueError", logged)
        self.assertIn("ingest_sms", logged)  # the stack is logged
        self.assertNotIn("boom", logged)  # the message (which could quote the SMS) is not

    def test_wrong_paths(self):
        self.assertEqual(self.request("GET", "/")[0], 404)
        self.assertEqual(self.request("GET", f"/sms/{TOKEN}")[0], 404)
        self.assertEqual(self.request("POST", "/sms/")[0], 404)
        self.assertEqual(self.request("POST", f"/sms/{TOKEN}/x", b"hi")[0], 404)
        self.assertEqual(self.request("POST", "/other", b"hi")[0], 404)
        self.assertEqual(self.bot.calls, [])

    def test_logs_never_contain_token_or_text(self):
        self.post_json({"text": SMS})
        self.post_json({"text": SMS}, path="/sms/another-token-xyz")
        self.request("POST", f"/sms/{TOKEN}?k={TOKEN}", SMS.encode())
        self.request("POST", f"/sms/{TOKEN}", b"")
        self.bot.error = TelegramError("sendMessage: Bad Request: chat not found")
        self.post_json({"text": SMS})
        self.bot.error = RuntimeError(f"failed on {SMS} for {TOKEN}")
        self.post_json({"text": SMS})
        self.request("GET", f"/nope/{TOKEN}")

        logged = "\n".join(self.capture.lines) + self.stderr.getvalue()
        self.assertIn("/sms/<redacted>", logged)  # requests are logged, redacted
        for secret in (TOKEN, "another-token-xyz", "BLINKIT", "450.00", "123456789012"):
            self.assertNotIn(secret, logged)


class ConfigTest(unittest.TestCase):
    def test_missing_token(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({})
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_lists_every_problem(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({"PORT": "http", "TIMEZONE": "Mars/Base", "CODEX_EFFORT": "max",
                         "AI_TIMEOUT": "soon", "MAIL_POLL_SECONDS": "-5"})
        message = str(ctx.exception)
        for name in ("TELEGRAM_BOT_TOKEN", "PORT", "TIMEZONE", "CODEX_EFFORT", "AI_TIMEOUT",
                     "MAIL_POLL_SECONDS"):
            self.assertIn(name, message)

    def test_defaults(self):
        cfg = load_config({"TELEGRAM_BOT_TOKEN": "123:abc"})
        self.assertEqual(cfg.telegram_token, "123:abc")
        self.assertEqual(cfg.public_url, "")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.db_path, "data/bot.db")
        self.assertEqual(cfg.currency, "INR")
        self.assertEqual(cfg.timezone, "Asia/Kolkata")
        self.assertEqual(cfg.codex_model, "")
        self.assertEqual(cfg.codex_effort, "low")
        self.assertEqual(cfg.ai_timeout, 120)
        self.assertEqual(cfg.mail_poll_seconds, 120)
        self.assertIn("swiggy", cfg.mail_senders)
        self.assertIn("magicpin", cfg.mail_senders)
        self.assertEqual(len(cfg.mail_senders), 16)
        self.assertEqual(cfg.telegram_api_base, "https://api.telegram.org")
        with self.assertRaises(Exception):
            cfg.port = 1  # frozen

    def test_overrides_and_url_normalisation(self):
        cfg = load_config({
            "TELEGRAM_BOT_TOKEN": " 123:abc ",
            "PUBLIC_URL": "https://home.example.net/bot/// ",
            "PORT": "9000",
            "DB_PATH": "/data/bot.db",
            "CODEX_MODEL": "gpt-5.5",
            "CODEX_EFFORT": "Medium",
            "AI_TIMEOUT": "90",
            "MAIL_SENDERS": " Swiggy, ,zomato,swiggy ",
            "MAIL_POLL_SECONDS": "10",
            "TELEGRAM_API_BASE": "http://localhost:8081/",
            "CURRENCY": "eur",
        })
        self.assertEqual(cfg.telegram_token, "123:abc")
        self.assertEqual(cfg.public_url, "https://home.example.net/bot")
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.db_path, "/data/bot.db")
        self.assertEqual(cfg.codex_model, "gpt-5.5")
        self.assertEqual(cfg.codex_effort, "medium")
        self.assertEqual(cfg.ai_timeout, 90)
        self.assertEqual(cfg.mail_senders, ("swiggy", "zomato"))
        self.assertIn("hdfcbank", cfg.bank_senders)  # BANK_SENDERS unset: the built-in list
        self.assertEqual(load_config({"TELEGRAM_BOT_TOKEN": "1:a", "BANK_SENDERS": "ICICIBank, alerts@sbi.co.in"})
                         .bank_senders, ("icicibank", "alerts@sbi.co.in"))
        self.assertEqual(cfg.mail_poll_seconds, 30)  # raised to the minimum
        self.assertEqual(cfg.telegram_api_base, "http://localhost:8081")
        self.assertEqual(cfg.currency, "EUR")

    def test_bad_public_url(self):
        with self.assertRaises(ConfigError):
            load_config({"TELEGRAM_BOT_TOKEN": "1:a", "PUBLIC_URL": "home.example.net"})


if __name__ == "__main__":
    unittest.main()
