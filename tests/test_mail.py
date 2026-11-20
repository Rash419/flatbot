import imaplib
import re
import socket
import unittest
from email.message import EmailMessage
from unittest import mock

from app import mail
from app.mail import Fetched, MailError, check_login, fetch_new, html_to_text, parse_message


def plain_message(sender, subject, body):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def html_message(sender, subject, html, text=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    if text is None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for app.mail, with a log of every command."""

    def __init__(self, messages=None, uidvalidity=7, login_error=None, select_ok=True):
        self.messages = dict(messages or {})  # uid -> raw bytes
        self.uidvalidity = uidvalidity
        self.login_error = login_error
        self.select_ok = select_ok
        self.log = []
        self.logged_out = False

    def login(self, user, password):
        self.log.append(("login", user))
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"Logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        self.log.append(("select", mailbox, readonly))
        return ("OK", [str(len(self.messages)).encode()]) if self.select_ok else ("NO", [b"no"])

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def status(self, mailbox, names):
        return "OK", [f'"INBOX" (UIDVALIDITY {self.uidvalidity})'.encode()]

    def uid(self, command, *args):
        self.log.append(("uid", command) + args)
        if command == "SEARCH":
            criteria = args[-1]
            uids = sorted(self.messages)
            if criteria == "UID *":
                found = uids[-1:]
            else:
                lo = int(re.match(r"UID (\d+):\*", criteria).group(1))
                # Like real servers: n:* always includes the highest UID, even below n.
                found = sorted({u for u in uids if u >= lo} | set(uids[-1:]))
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            uid_set, what = args
            self.assert_peek(what)
            out = []
            for i, uid in enumerate(int(u) for u in uid_set.split(",")):
                raw = self.messages[uid]
                if "HEADER.FIELDS" in what:
                    head = raw.split(b"\n\n", 1)[0]
                    raw = b"\r\n".join(l for l in head.split(b"\n")
                                       if l.lower().startswith((b"from:", b"subject:"))) + b"\r\n\r\n"
                out.append((f"{i + 1} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        raise AssertionError(f"unexpected UID {command}")

    @staticmethod
    def assert_peek(what):
        assert "BODY.PEEK[" in what and "BODY[" not in what.replace("BODY.PEEK[", ""), what

    def logout(self):
        self.logged_out = True

    # Anything that could change the mailbox must never be called.
    def store(self, *a):
        raise AssertionError("store called")

    def expunge(self, *a):
        raise AssertionError("expunge called")

    def copy(self, *a):
        raise AssertionError("copy called")


SENDERS = ("swiggy", "blinkit")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.imap = FakeIMAP({
            10: plain_message("Rahul <rahul@gmail.com>", "Hi", "dinner?"),
            11: plain_message("Swiggy <noreply@SWIGGY.in>", "Your order", "Total Rs 450"),
            12: plain_message("Blinkit <no-reply@blinkit.com>", "Order delivered", "Total Rs 409"),
            13: plain_message("Bank <alerts@hdfcbank.net>", "Statement", "..."),
        })
        patcher = mock.patch.object(mail, "connect", return_value=self.imap)
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, **kw):
        args = dict(uidvalidity=7, last_uid=9, senders=SENDERS)
        args.update(kw)
        return fetch_new("imap.example.com", 993, "me@example.com", "app-pass", **args)

    def test_first_run_returns_no_history(self):
        self.assertEqual(self.fetch(uidvalidity=None, last_uid=None), Fetched(7, 13, ()))
        self.assertFalse(any(e[:2] == ("uid", "FETCH") for e in self.imap.log))

    def test_changed_uidvalidity_resets(self):
        self.assertEqual(self.fetch(uidvalidity=6, last_uid=11), Fetched(7, 13, ()))

    def test_empty_mailbox_first_run(self):
        self.imap.messages.clear()
        self.assertEqual(self.fetch(uidvalidity=None, last_uid=None), Fetched(7, 0, ()))

    def test_new_mail_filtered_by_sender(self):
        got = self.fetch()
        self.assertEqual(got.uidvalidity, 7)
        self.assertEqual(got.last_uid, 13)
        self.assertEqual([m.uid for m in got.mails], [11, 12])
        self.assertEqual(got.mails[0].subject, "Your order")
        self.assertIn("Total Rs 450", got.mails[0].body)
        # Only the matching messages were downloaded in full.
        full = [e for e in self.imap.log if e[:2] == ("uid", "FETCH") and "BODY.PEEK[]" in e[3]]
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0][2], "11,12")

    def test_read_only(self):
        self.fetch()
        self.assertIn(("select", "INBOX", True), self.imap.log)
        self.assertTrue(self.imap.logged_out)

    def test_nothing_new_does_not_go_backwards(self):
        # "14:*" returns UID 13 (the highest); it must not be processed again.
        self.assertEqual(self.fetch(last_uid=13), Fetched(7, 13, ()))

    def test_limit_takes_oldest_first(self):
        got = self.fetch(limit=2)
        self.assertEqual(got.last_uid, 11)
        self.assertEqual([m.uid for m in got.mails], [11])
        got = self.fetch(last_uid=got.last_uid, limit=2)
        self.assertEqual(got.last_uid, 13)
        self.assertEqual([m.uid for m in got.mails], [12])

    def test_no_matching_senders(self):
        got = self.fetch(senders=("amazon",))
        self.assertEqual(got, Fetched(7, 13, ()))

    def test_uid_after_literal(self):
        # Some servers send "1 (BODY[...] {n}" + literal + " UID 11)".
        real_uid = self.imap.uid

        def uid(command, *args):
            typ, data = real_uid(command, *args)
            if command == "FETCH":
                moved = []
                for part in data:
                    if isinstance(part, tuple):
                        number = re.search(rb"UID (\d+)", part[0]).group(1)
                        moved += [(re.sub(rb"UID \d+ ", b"", part[0]), part[1]), b" UID " + number + b")"]
                data = moved
            return typ, data

        self.imap.uid = uid
        self.assertEqual([m.uid for m in self.fetch().mails], [11, 12])

    def test_connection_lost_mid_fetch(self):
        self.imap.uid = mock.Mock(side_effect=imaplib.IMAP4.abort("socket error"))
        with self.assertRaises(MailError):
            self.fetch()


class LoginErrorTest(unittest.TestCase):
    def login(self, conn=None, side_effect=None):
        with mock.patch.object(mail, "connect", return_value=conn, side_effect=side_effect):
            check_login("imap.gmail.com", 993, "me@gmail.com", "hunter2-app-pass")

    def assert_error(self, *words, **kw):
        with self.assertRaises(MailError) as ctx:
            self.login(**kw)
        message = str(ctx.exception)
        self.assertNotIn("hunter2", message)
        for word in words:
            self.assertIn(word, message)
        return message

    def test_ok(self):
        conn = FakeIMAP()
        self.login(conn)
        self.assertIn(("select", "INBOX", True), conn.log)
        self.assertTrue(conn.logged_out)

    def test_wrong_password(self):
        self.assert_error("me@gmail.com", "app password",
                          conn=FakeIMAP(login_error="[AUTHENTICATIONFAILED] Invalid credentials (Failure)"))

    def test_imap_disabled(self):
        self.assert_error("IMAP", conn=FakeIMAP(login_error="[ALERT] Your account is not enabled for IMAP use."))

    def test_app_password_required(self):
        self.assert_error("app password",
                          conn=FakeIMAP(login_error="[ALERT] Application-specific password required"))

    def test_unknown_host(self):
        self.assert_error("imap.gmail.com", side_effect=socket.gaierror(-2, "Name or service not known"))

    def test_unreachable(self):
        self.assert_error("reach", side_effect=TimeoutError("timed out"))

    def test_inbox_not_selectable(self):
        self.assert_error("inbox", conn=FakeIMAP(select_ok=False))


class ParseTest(unittest.TestCase):
    def test_plain(self):
        m = parse_message(plain_message("Swiggy <noreply@swiggy.in>", "Order #12", "Biryani  Rs 250\n\nTotal Rs 250"))
        self.assertEqual(m.sender, "Swiggy <noreply@swiggy.in>")
        self.assertEqual(m.subject, "Order #12")
        self.assertEqual(m.body, "Biryani Rs 250\nTotal Rs 250")

    def test_html_only(self):
        raw = html_message("a@zomato.com", "Receipt",
                           "<html><body><table><tr><td>Paneer</td><td>&#8377;200</td></tr>"
                           "<tr><td>Total</td><td>&#8377;230</td></tr></table></body></html>")
        self.assertEqual(parse_message(raw).body, "Paneer ₹200\nTotal ₹230")

    def test_multipart_prefers_plain(self):
        raw = html_message("a@zomato.com", "Receipt", "<p>HTML version</p>", text="Plain version")
        self.assertEqual(parse_message(raw).body, "Plain version")

    def test_encoded_headers(self):
        raw = (b"From: =?UTF-8?B?U3dpZ2d5IOKcqA==?= <noreply@swiggy.in>\r\n"
               b"Subject: =?UTF-8?Q?Your_order_=E2=82=B9450?=\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n")
        m = parse_message(raw)
        self.assertEqual(m.sender, "Swiggy ✨ <noreply@swiggy.in>")
        self.assertEqual(m.subject, "Your order ₹450")
        self.assertEqual(m.body, "hello")

    def test_unknown_charset(self):
        raw = b"From: a@b.c\r\nSubject: x\r\nContent-Type: text/plain; charset=x-nonsense\r\n\r\nRs 100\r\n"
        self.assertEqual(parse_message(raw).body, "Rs 100")


class HtmlToTextTest(unittest.TestCase):
    def test_blocks_scripts_entities(self):
        html = """<html><head><title>Ignore me</title><style>p {color: red}</style></head>
        <body><script>var x = "<p>no</p>";</script>
        <h1>Order   summary</h1><p>Milk &amp; bread<br>Eggs&nbsp;x&nbsp;12</p>
        <div>Total:
             Rs 409</div>
        <table><tr><td>Item</td><td>Price</td></tr><tr><th>Bread</th><td>45</td></tr></table>
        <ul><li>one</li><li>two</li></ul></body></html>"""
        self.assertEqual(html_to_text(html),
                         "Order summary\nMilk & bread\nEggs x 12\nTotal: Rs 409\nItem Price\nBread 45\none\ntwo")

    def test_broken_html(self):
        self.assertEqual(html_to_text("<p>Total <b>Rs 5"), "Total Rs 5")


if __name__ == "__main__":
    unittest.main()
