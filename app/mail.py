"""Read order emails over IMAP. Strictly read-only.

The inbox is opened with EXAMINE (`readonly=True`) and messages are fetched with
BODY.PEEK, so nothing is marked as read, moved or flagged. Only messages whose
From address matches one of the configured senders are downloaded in full.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from html.parser import HTMLParser

TIMEOUT = 30  # seconds per network operation


class MailError(Exception):
    """A mailbox problem. The message is written for the user and never contains the password."""


@dataclass(frozen=True)
class Mail:
    uid: int
    sender: str
    subject: str
    body: str  # plain text


@dataclass(frozen=True)
class Fetched:
    uidvalidity: int
    last_uid: int
    mails: tuple[Mail, ...]


def connect(host: str, port: int):
    """Open the IMAP connection. Tests replace this with a fake."""
    return imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)


# --- connection and errors ----------------------------------------------------------------

def _login_error(e: Exception, user: str) -> MailError:
    text = str(e).lower()
    if "imap" in text and ("disabled" in text or "not enabled" in text or "enable" in text):
        return MailError("IMAP is turned off for this mailbox. Turn on IMAP access in the mail "
                         "settings, then try again.")
    if "application-specific" in text or "app password" in text or "web browser" in text:
        return MailError(f"The mail server wants an app password for {user}, not your normal password. "
                         "Create an app password and use that.")
    return MailError(f"The mail server rejected the login for {user}. Check the email address and "
                     "the app password (not your normal password).")


def _open(host: str, port: int, user: str, password: str):
    try:
        conn = connect(host, port)
    except (socket.gaierror, UnicodeError):
        raise MailError(f"Could not find the mail server {host}. Check the server name "
                        "(for Gmail it is imap.gmail.com).") from None
    except ssl.SSLError:
        raise MailError(f"Could not open a secure connection to {host}:{port}. Check the server "
                        "name and port (IMAP over SSL is usually port 993).") from None
    except (OSError, imaplib.IMAP4.error):
        raise MailError(f"Could not reach the mail server {host}:{port}. Check the server name, "
                        "or try again later.") from None
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        _close(conn)
        raise _login_error(e, user) from None
    except OSError:
        _close(conn)
        raise MailError(f"Lost the connection to {host} while logging in. Try again later.") from None
    try:
        typ, _ = conn.select("INBOX", readonly=True)
    except (imaplib.IMAP4.error, OSError):
        typ = "NO"
    if typ != "OK":
        _close(conn)
        raise MailError("Logged in, but could not open the inbox. Check that IMAP access is "
                        "enabled for this mailbox.")
    return conn


def _close(conn) -> None:
    try:
        conn.logout()
    except Exception:
        pass


def check_login(host: str, port: int, user: str, password: str) -> None:
    """Connect, log in and open INBOX read-only. Raises MailError with a reason the user can act on."""
    _close(_open(host, port, user, password))


# --- fetching -----------------------------------------------------------------------------

def _uidvalidity(conn) -> int:
    typ, data = conn.response("UIDVALIDITY")
    value = data[0] if data and data[0] else None
    if value is None:
        typ, data = conn.status("INBOX", "(UIDVALIDITY)")
        m = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"") if typ == "OK" and data else None
        value = m.group(1) if m else b"0"
    return int(value)


def _search(conn, criteria: str) -> list[int]:
    typ, data = conn.uid("SEARCH", None, criteria)
    if typ != "OK":
        raise MailError("The mail server would not list new messages. Try again later.")
    return sorted({int(x) for chunk in data if chunk for x in chunk.split()})


def _fetch(conn, uids: Sequence[int], what: str) -> dict[int, bytes]:
    """UID FETCH; returns {uid: literal bytes}."""
    typ, data = conn.uid("FETCH", ",".join(map(str, uids)), what)
    if typ != "OK":
        raise MailError("The mail server would not send the messages. Try again later.")
    out = {}
    for i, part in enumerate(data):
        if isinstance(part, tuple) and len(part) >= 2:
            m = re.search(rb"\bUID (\d+)", part[0])
            if not m and i + 1 < len(data) and isinstance(data[i + 1], bytes):
                m = re.search(rb"\bUID (\d+)", data[i + 1])  # some servers put UID after the literal
            if m:
                out[int(m.group(1))] = part[1]
    return out


def _sender_matches(sender: str, senders: Sequence[str]) -> bool:
    address = parseaddr(sender)[1].lower()
    return any(s.strip().lower() in address for s in senders if s.strip())


def fetch_new(host: str, port: int, user: str, password: str, *, uidvalidity: int | None,
              last_uid: int | None, senders: Sequence[str], limit: int = 20) -> Fetched:
    """Messages newer than last_uid from the listed senders, oldest first. Raises MailError.

    The first call (no uidvalidity), or a mailbox whose UIDVALIDITY changed, returns no
    mail and the current highest UID, so old mail is never processed.
    """
    conn = _open(host, port, user, password)
    try:
        current = _uidvalidity(conn)
        if uidvalidity is None or last_uid is None or uidvalidity != current:
            uids = _search(conn, "UID *")  # the highest UID (empty mailbox: nothing)
            return Fetched(current, max(uids, default=0), ())

        # "n:*" always includes the highest UID even when it is below n, so filter.
        new = [u for u in _search(conn, f"UID {last_uid + 1}:*") if u > last_uid][:max(limit, 1)]
        if not new:
            return Fetched(current, last_uid, ())

        headers = _fetch(conn, new, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        wanted = []
        for uid in new:
            head = parse_message(headers.get(uid, b""))
            if _sender_matches(head.sender, senders):
                wanted.append(uid)

        mails = []
        if wanted:
            full = _fetch(conn, wanted, "(UID BODY.PEEK[])")
            for uid in wanted:
                if uid in full:
                    m = parse_message(full[uid])
                    mails.append(Mail(uid, m.sender, m.subject, m.body))
        return Fetched(current, max(new), tuple(mails))
    except (imaplib.IMAP4.error, OSError):
        raise MailError("Lost the connection to the mail server. Will try again later.") from None
    finally:
        _close(conn)


# --- parsing ------------------------------------------------------------------------------

def _header(msg, name: str) -> str:
    try:
        value = msg[name]
    except Exception:  # a header too malformed to decode
        value = ""
    return " ".join(str(value or "").split())


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError, AssertionError):  # unknown charset or bad encoding
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def parse_message(raw: bytes) -> Mail:
    """Parse a raw RFC 822 message (or just its headers). uid is 0."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is not None:
        text = _part_text(part)
        body = html_to_text(text) if part.get_content_type() == "text/html" else _tidy(text)
    return Mail(0, _header(msg, "From"), _header(msg, "Subject"), body)


_BLOCK = {"address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
          "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
          "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
          "tfoot", "thead", "tr", "ul"}
_BREAK = "\x00"  # our own line breaks, distinct from newlines in the HTML source
_SKIP = {"script", "style", "head", "title", "noscript", "template"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append(_BREAK)
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.out.append(_BREAK)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append(_BREAK)
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _tidy(text: str) -> str:
    lines = (" ".join(line.replace("\xa0", " ").split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Readable text from HTML: no scripts or styles, a line per block or table row."""
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    # Line breaks in the source are just whitespace in HTML.
    text = "".join(parser.out).replace("\r", " ").replace("\n", " ")
    return _tidy(text.replace(_BREAK, "\n"))
