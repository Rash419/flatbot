"""The Telegram side: private chats only, one shared household ledger."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from email.utils import parseaddr
from html import escape
from zoneinfo import ZoneInfo

from .db import DB
from .ledger import money, nets, pairwise, plain_amount, proportional_part, settle_up, shares_for, split_equal
from .sms import merchant_key, parse_manual
from .telegram import Keyboard, Telegram, TelegramError

log = logging.getLogger(__name__)

PRIVATE = "This bot is private. Send /start &lt;join code&gt; to join."
MANUAL_HINT = "To add a spend, type the amount and what it was, e.g. <code>450 groceries</code>. /help for more."
GMAIL_HOST = "imap.gmail.com"
IMAP_PORT = 993
# Delays before asking the AI again about an SMS it failed to read.
RETRY_DELAYS = [timedelta(minutes=m) for m in (1, 5, 15, 30, 60)]
RESEND_AFTER = timedelta(minutes=1)    # leave fresh items to whoever is sending them right now
ORDER_WINDOW = timedelta(hours=12)     # an order email and its payment are this close in time
MAX_PICK_LINES = 40
MAIL_FAILURES_BEFORE_WARNING = 5
MAX_BANK_MAIL_CHARS = 4000  # bank alerts are short; the rest is disclaimers and footers
_HOSTNAME = re.compile(r"^(?=.*[a-z])[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
# Default prefilter when main.py injects none: an amount with a currency marker (or "debited by 450"),
# and not a one-time-password message. The real one lives in app.extract.
_MONEY = re.compile(r"(?<![a-z])(?:rs\.?|inr)\s*\d|₹\s*\d|\bdebited\s+(?:by|for|with)\s+\d", re.I)
_OTP = re.compile(r"\b(?:otp|one[- ]time (?:password|pin)|verification code|security code|passcode"
                  r"|auth(?:entication)? code)\b", re.I)
_AMOUNT_ONLY = re.compile(r"^(?:rs\.?|inr|₹)?\s*(\d[\d,]*)(?:\.(\d{1,2}))?$", re.I)


def _snippet(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def looks_like_money(text: str) -> bool:
    return bool(_MONEY.search(text)) and not _OTP.search(text)


def _sender_matches(sender: str, senders: Sequence[str]) -> bool:
    address = parseaddr(sender)[1].lower()
    return any(s in address for s in senders)


def bank_mail_text(subject: str, body: str) -> str | None:
    """The text of a bank alert email to read like an SMS, or None for an OTP email.

    Bank emails end with warnings such as "never share your OTP", which would make the whole
    message look like an OTP, so lines mentioning one are left out instead of sent anywhere.
    """
    if _OTP.search(subject):
        return None
    lines = [line for line in body.splitlines() if not _OTP.search(line)]
    return "\n".join([subject, *lines])[:MAX_BANK_MAIL_CHARS]


def _amount_only(text: str) -> int | None:
    m = _AMOUNT_ONLY.match(text.strip())
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int((m.group(2) or "").ljust(2, "0"))


class Bot:
    COMMANDS = [
        ("balance", "Who owes whom"),
        ("history", "Last 15 shared entries"),
        ("rules", "Merchants you always split or skip"),
        ("phone", "Set up SMS forwarding from your phone"),
        ("invite", "Join code for your flatmate"),
        ("name", "Change your display name"),
        ("export", "CSV of all shared entries"),
        ("email", "Read bank alert and order emails"),
        ("help", "How this works"),
    ]

    def __init__(self, db: DB, tg: Telegram, *, currency: str = "INR", public_url: str = "",
                 tz: str = "Asia/Kolkata", extractor=None, mailbox=None, mail_senders: Sequence[str] = (),
                 bank_senders: Sequence[str] = (), money_candidate: Callable[[str], bool] | None = None):
        self.db = db
        self.tg = tg
        self.currency = currency
        self.public_url = public_url.rstrip("/")
        self.tz = ZoneInfo(tz)
        self.extractor = extractor          # AI reader for SMS, order emails and bills; None = AI off
        self.mailbox = mailbox              # IMAP helper (app.mail); None = order emails off
        self.mail_senders = tuple(mail_senders)
        self.bank_senders = tuple(s.strip().lower() for s in bank_senders if s.strip())
        self.money_candidate = money_candidate or looks_like_money
        self._wake = threading.Event()
        self._code = db.set_default("join_code", secrets.token_hex(4))

    @property
    def join_code(self) -> str:
        return self._code

    # --- small helpers
    def _m(self, paise: int) -> str:
        return money(paise, self.currency)

    def _names(self) -> dict[int, str]:
        return {m["tg_id"]: m["name"] for m in self.db.members()}

    def _name(self, tg_id: int | None) -> str:
        return escape(self._names().get(tg_id, "someone"))

    def _local_date(self, iso: str, fmt: str) -> str:
        return datetime.fromisoformat(iso).astimezone(self.tz).strftime(fmt)

    def _part_of(self, item) -> int | None:
        """The full payment when only part of it is shared, else None."""
        total = item["total"]
        return total if total is not None and total != item["amount"] else None

    def _head(self, item, mark: str = "", with_total: bool = True) -> str:
        total = self._part_of(item) if with_total else None
        of = f" of {self._m(total)}" if total is not None else ""
        return f"{mark}<b>{self._m(item['amount'])}</b>{of} · {escape(item['description'])}"

    def _safe_send(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int | None:
        try:
            return self.tg.send(chat_id, text, keyboard)
        except TelegramError as e:
            log.warning("message to %s failed: %s", chat_id, e)
            return None

    # --- entry points
    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except Exception:
            log.exception("update %s failed", update.get("update_id"))

    def ingest_sms(self, token: str, text: str) -> str:
        """Queue a bank SMS for the inbox worker. Fast, and never talks to Telegram."""
        member = self.db.member_by_token(token) if token else None
        if member is None:
            return "unknown"
        return self._enqueue(member["tg_id"], text)

    def _enqueue(self, payer: int, text: str) -> str:
        """Queue a bank message (SMS or alert email) for the AI reader."""
        if not self.money_candidate(text):
            return "ignored"  # not about money: keep nothing about it
        digest = hashlib.sha256(f"{payer}:{' '.join(text.split())}".encode()).hexdigest()
        if not self.db.enqueue_sms(payer, digest, text):
            return "duplicate"
        self._wake.set()
        return "queued"

    def run_inbox(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._wake.clear()
            try:
                self.process_inbox()
            except Exception:
                log.exception("inbox pass failed")
            self._wake.wait(30)

    def run_mail(self, stop: threading.Event, interval: float = 120) -> None:
        while not stop.is_set():
            try:
                self.poll_mail()
            except Exception:
                log.exception("mail pass failed")
            stop.wait(interval)

    def process_inbox(self) -> int:
        """One pass over due SMS; returns how many were finished."""
        finished = 0
        for row in self.db.due_sms():
            if self.extractor is None:  # a retry would not change anything
                self._cant_read(row, "I can't read bank SMS: the server isn't signed in to ChatGPT.")
            elif not self._handle_sms(row):
                attempts = row["attempts"] + 1
                if attempts <= len(RETRY_DELAYS):
                    self.db.retry_sms(row["id"], attempts, RETRY_DELAYS[attempts - 1])
                    continue
                self._cant_read(row, "I couldn't read this bank SMS:")
            self.db.finish_sms(row["id"])
            finished += 1
        self._resend_unsent()
        self.db.purge()
        return finished

    def _handle_sms(self, row) -> bool:
        """Read one queued SMS with the AI and record it if it is a spend; False = try again later."""
        try:
            txn = self.extractor.sms(row["text"])
        except Exception as e:  # AIError lives in app.ai, which this module does not import
            log.warning("AI could not read an SMS (%s)", type(e).__name__)
            return False
        try:
            if txn is not None and txn.amount > 0:  # None: not a completed spend
                self._record_sms(row["payer"], row["text"], txn, row["hash"])
        except Exception:
            log.exception("inbox row %s failed", row["id"])
            return False  # same back-off, so a row that keeps failing cannot spin
        return True

    def _cant_read(self, row, why: str) -> None:
        self._safe_send(row["payer"], (
            f"{why}\n<i>{escape(_snippet(row['text']))}</i>\n"
            "If it was a spend, type it like <code>450 groceries</code>."))

    def _record_sms(self, payer: int, text: str, txn, digest: str) -> None:
        dedup = f"{payer}:ref:{txn.ref}:{txn.amount}" if txn.ref else digest
        item_id = self._new_item(payer, txn.amount, txn.merchant or "Payment", "sms",
                                 merchant=txn.merchant, raw=text, dedup=dedup)
        if item_id is None:
            return  # the same spend arrived in another SMS
        key = merchant_key(txn.merchant) if txn.merchant else ""
        rule = self.db.get_rule(payer, key) if key else None
        if rule == "personal":
            self.db.mark_personal(item_id, by_rule=True)
            return
        members = [m["tg_id"] for m in self.db.members()]
        if rule == "split" and len(members) > 1:
            if self.db.share(item_id, "split", split_equal(txn.amount, members, payer), by_rule=True):
                self._notify(item_id)
        try:
            self._send_item(item_id)
        except TelegramError as e:
            log.warning("could not show item %s yet: %s", item_id, e)  # _resend_unsent retries

    def _resend_unsent(self) -> None:
        for item in self.db.unsent_items(self.db.stamp(-RESEND_AFTER)):
            try:
                self._send_item(item["id"])
            except TelegramError as e:
                log.warning("still could not show item %s: %s", item["id"], e)
                return  # Telegram is probably down; try again next pass

    def _new_item(self, payer: int, amount: int, description: str, source: str, **extra) -> int | None:
        """Create an item and attach an order email that arrived before it, if one matches."""
        item_id = self.db.add_item(payer, amount, description, source, **extra)
        if item_id is not None:
            waiting = self.db.waiting_orders(payer, amount, ORDER_WINDOW)
            if waiting and self.db.attach_order(waiting[0]["id"], item_id):
                for dup in waiting[1:]:  # the same order mailed twice (confirmation, delivery…)
                    self.db.delete_order(dup["id"])
        return item_id

    # --- order emails
    def poll_mail(self) -> None:
        if self.mailbox is None or self.extractor is None:
            return
        for acc in self.db.mail_accounts():
            owner = acc["tg_id"]
            try:
                fetched = self.mailbox.fetch_new(
                    acc["host"], acc["port"], acc["address"], acc["password"],
                    uidvalidity=acc["uidvalidity"], last_uid=acc["last_uid"],
                    senders=self.mail_senders + self.bank_senders, limit=20)
            except Exception as e:
                log.warning("mailbox of member %s could not be read (%s)", owner, type(e).__name__)
                if self.db.mail_failed(owner) == MAIL_FAILURES_BEFORE_WARNING:
                    self._safe_send(owner, "I haven't been able to read your mailbox for a while. "
                                           "You may need to reconnect it: send /email for how.")
                continue
            self.db.mail_fetched(owner, fetched.uidvalidity, fetched.last_uid)
            for mail in fetched.mails:
                if _sender_matches(mail.sender, self.bank_senders):
                    # A bank alert email is read exactly like a bank SMS from this mailbox's owner.
                    text = bank_mail_text(mail.subject, mail.body)
                    if text is not None:
                        self._enqueue(owner, text)
                    continue
                try:
                    order = self.extractor.order_email(mail.sender, mail.subject, mail.body)
                except Exception as e:
                    log.warning("an order email could not be read (%s)", type(e).__name__)
                    continue
                if order is not None and order.total > 0:
                    self._store_order(owner, order)
        self.db.purge()

    def _store_order(self, payer: int, order) -> None:
        lines = [(" ".join(str(name).split())[:80] or "Item", int(amount))
                 for name, amount in order.lines if int(amount) > 0]
        order_id = self.db.add_order(payer, (order.merchant or "")[:80], order.total, lines)
        item = self.db.item_for_order(payer, order.total, ORDER_WINDOW, attached=False)
        if item is not None and self.db.attach_order(order_id, item["id"]):
            if item["status"] == "pending" and item["message_id"] is not None:
                self._refresh(item["id"])
            return
        if self.db.item_for_order(payer, order.total, ORDER_WINDOW, attached=True) is not None:
            self.db.delete_order(order_id)  # a second email about an order that is already attached
        # otherwise it waits for its payment (see _new_item)

    # --- rendering an item for its payer
    def _render(self, item) -> tuple[str, Keyboard]:
        if item["status"] == "shared":
            return self._render_shared(item)
        if item["status"] == "personal":
            return self._render_personal(item)
        return self._render_pending(item)

    def _pickable_lines(self, item) -> list:
        """The attached order's lines, if there are few enough to tick one by one."""
        if item["order_id"] is None:
            return []
        lines = self.db.order_lines(item["order_id"])
        return lines if 1 <= len(lines) <= MAX_PICK_LINES else []

    def _render_pending(self, item) -> tuple[str, Keyboard]:
        iid = item["id"]
        others = [m for m in self.db.members() if m["tg_id"] != item["payer"]]
        order_lines = self._pickable_lines(item) if others else []
        if item["view"] == "pick" and order_lines:
            return self._render_pick(item, order_lines)
        lines = [self._head(item)]
        if item["merchant"] is None and item["raw"]:
            lines += [f"<i>{escape(_snippet(item['raw']))}</i>", "Reply to this message with a name to label it."]
        if not others:
            lines.append("Nobody else has joined yet. Share the code from /invite.")
            return "\n".join(lines), [[("Personal", f"p:{iid}")]]
        if order_lines:
            count = len(order_lines)
            lines.append(f"🧾 Order email found: {count} item{'' if count == 1 else 's'}")
        lines.append("Shared?")
        keyboard: Keyboard = [[("Split equally", f"s:{iid}")]]
        for m in others:
            keyboard.append([(f"{m['name']} owes all", f"f:{iid}:{m['tg_id']}"),
                             (f"Paid {m['name']} back", f"b:{iid}:{m['tg_id']}")])
        if order_lines:
            keyboard.append([("Pick shared items", f"k:{iid}")])
        keyboard.append([("Only part is shared", f"o:{iid}")])
        keyboard.append([("Personal", f"p:{iid}")])
        return "\n".join(lines), keyboard

    def _picked_part(self, item, order_lines) -> int:
        ticked = sum(line["amount"] for line in order_lines if line["ticked"])
        whole = sum(line["amount"] for line in order_lines)
        return proportional_part(item["total"] or item["amount"], ticked, whole)

    def _render_pick(self, item, order_lines) -> tuple[str, Keyboard]:
        iid = item["id"]
        keyboard: Keyboard = []
        for line in order_lines:
            name = line["name"] if len(line["name"]) <= 28 else line["name"][:27] + "…"
            box = "☑" if line["ticked"] else "☐"
            keyboard.append([(f"{box} {name} · {self._m(line['amount'])}", f"t:{iid}:{line['id']}")])
        if any(line["ticked"] for line in order_lines):
            keyboard.append([(f"Split ticked · {self._m(self._picked_part(item, order_lines))}", f"x:{iid}")])
        else:
            keyboard.append([("Split ticked", f"x:{iid}")])
        keyboard.append([("Back", f"v:{iid}")])
        return f"{self._head(item)}\nTick what was for the flat:", keyboard

    def _rule_button(self, item, action: str, label: str) -> list:
        key = merchant_key(item["merchant"]) if item["merchant"] else ""
        if not key or self.db.get_rule(item["payer"], key):
            return []
        return [[(f"{label} {item['merchant']}", f"r{action[0]}:{item['id']}")]]

    def _render_shared(self, item) -> tuple[str, Keyboard]:
        names = self._names()
        shares = self.db.shares(item["id"])
        cp = escape(names.get(item["counterparty"], "someone"))
        if item["kind"] == "split":
            owing = {tg: owed for tg, owed in shares.items() if tg != item["payer"] and owed}
            amounts = set(owing.values())
            who = [escape(names.get(tg, "someone")) for tg in owing]
            if not owing:
                detail = "Split equally"
            elif len(owing) == 1:
                detail = f"Split equally · {who[0]} owes you {self._m(next(iter(amounts)))}"
            elif len(amounts) == 1:
                detail = f"Split equally · {', '.join(who)} owe you {self._m(amounts.pop())} each"
            else:
                detail = "Split equally · " + ", ".join(
                    f"{escape(names.get(tg, 'someone'))} owes you {self._m(v)}" for tg, v in owing.items())
        elif item["kind"] == "full":
            detail = f"{cp} owes all · {cp} owes you {self._m(item['amount'])}"
        else:
            detail = f"Paid {cp} back"
        lines = [self._head(item, "⚡ " if item["by_rule"] else "✅ "), detail]
        if item["picked"]:
            lines.append(f"🧾 {escape(item['picked'])}")
        if item["by_rule"]:
            lines.append(f"Added by your rule for {escape(item['merchant'] or '')}")
        keyboard: Keyboard = [[("Undo", f"u:{item['id']}")]]
        if item["kind"] == "split" and item["total"] is None:  # a part split is not a pattern to remember
            keyboard += self._rule_button(item, "split", "Always split")
        return "\n".join(lines), keyboard

    def _render_personal(self, item) -> tuple[str, Keyboard]:
        text = f"{self._head(item, '🚫 ')}\nPersonal, not shared"
        return text, [[("Undo", f"u:{item['id']}")]] + self._rule_button(item, "personal", "Always skip")

    def _send_item(self, item_id: int) -> None:
        """Show an item to its payer. Raises TelegramError so SMS ingest can ask for a retry."""
        item = self.db.item(item_id)
        text, keyboard = self._render(item)
        self.db.update_item(item_id, message_id=self.tg.send(item["payer"], text, keyboard))

    def _refresh(self, item_id: int) -> None:
        item = self.db.item(item_id)
        if item["message_id"] is None:
            return
        text, keyboard = self._render(item)
        try:
            self.tg.edit(item["payer"], item["message_id"], text, keyboard)
        except TelegramError as e:
            log.warning("could not update item %s: %s", item_id, e)

    # --- notices to the other members
    def _balance_line(self, me: int, other: int) -> str:
        name = self._name(other)
        owed = pairwise(self.db.debts(), me, other)
        if owed > 0:
            return f"{name} owes you {self._m(owed)} in total"
        if owed < 0:
            return f"You owe {name} {self._m(-owed)} in total"
        return f"You and {name} are settled up"

    def _notice_body(self, item, tg_id: int, shares: dict[int, int]) -> str:
        payer = self._name(item["payer"])
        if item["kind"] == "settle":
            return f"{payer} paid you {self._m(item['amount'])}"
        part = self._part_of(item)
        of = f"\nPart of a {self._m(part)} payment" if part is not None else ""
        if item["picked"]:
            of += f"\n🧾 {escape(item['picked'])}"
        return (f"{payer} added {self._head(item, with_total=False)}{of}\n"
                f"Your share: {self._m(shares.get(tg_id, 0))}")

    def _notify(self, item_id: int) -> None:
        """Send or update the notice for every other member involved. Failures are only logged."""
        item = self.db.item(item_id)
        shares = self.db.shares(item_id)
        sent = self.db.notices(item_id)
        for tg_id, owed in shares.items():
            if tg_id == item["payer"] or not (owed or tg_id == item["counterparty"]):
                continue
            text = f"{self._notice_body(item, tg_id, shares)}\n{self._balance_line(tg_id, item['payer'])}"
            try:
                if tg_id in sent:
                    self.tg.edit(tg_id, sent[tg_id], text)
                else:
                    self.db.set_notice(item_id, tg_id, self.tg.send(tg_id, text))
            except TelegramError as e:
                log.warning("notice for item %s to %s failed: %s", item_id, tg_id, e)

    def _retract_notices(self, item, shares: dict[int, int], sent: dict[int, int]) -> None:
        for tg_id, message_id in sent.items():
            body = self._notice_body(item, tg_id, shares).replace("\n", "</s>\n<s>")
            text = (f"<s>{body}</s>\nRemoved by {self._name(item['payer'])}\n"
                    f"{self._balance_line(tg_id, item['payer'])}")
            try:
                self.tg.edit(tg_id, message_id, text)
            except TelegramError as e:
                log.warning("could not retract notice for item %s to %s: %s", item["id"], tg_id, e)
        self.db.delete_notices(item["id"])

    # --- button taps
    def _on_callback(self, cq: dict) -> None:
        cid = cq["id"]
        uid = cq.get("from", {}).get("id")
        if (cq.get("message") or {}).get("chat", {}).get("type") != "private":
            self.tg.answer(cid)
            return
        if self.db.member(uid) is None:
            self.tg.answer(cid, "This bot is private. Send /start <join code> to join.")
            return
        action, _, rest = (cq.get("data") or "").partition(":")
        try:
            args = [int(x) for x in rest.split(":")]
        except ValueError:
            self.tg.answer(cid, "Unknown button")
            return

        if action == "rd":
            self.db.delete_rule(args[0], uid)
            text, keyboard = self._rules_view(uid)
            self.tg.edit(uid, cq["message"]["message_id"], text, keyboard)
            self.tg.answer(cid, "Rule deleted")
            return

        item = self.db.item(args[0])
        if item is None:
            self.tg.answer(cid, "That entry no longer exists")
            return
        if item["payer"] != uid:
            self.tg.answer(cid, "Only the person who paid can change this", alert=True)
            return
        handlers = {"s": self._tap_split, "f": self._tap_counterparty, "b": self._tap_counterparty,
                    "p": self._tap_personal, "o": self._tap_part, "u": self._tap_undo,
                    "k": self._tap_pick_view, "v": self._tap_pick_view, "t": self._tap_tick, "x": self._tap_split_ticked, "rs": self._tap_rule, "rp": self._tap_rule}
        handler = handlers.get(action)
        if handler is None:
            self.tg.answer(cid, "Unknown button")
            return
        self.tg.answer(cid, handler(item, action, args[1:]))

    def _tap_split(self, item, action, args) -> str:
        members = [m["tg_id"] for m in self.db.members()]
        if len(members) < 2:
            return "Nobody to split with yet"
        if not self.db.share(item["id"], "split", split_equal(item["amount"], members, item["payer"])):
            return "Already handled"
        self._refresh(item["id"])
        self._notify(item["id"])
        return "Split equally"

    def _tap_counterparty(self, item, action, args) -> str:
        other = args[0] if args else None
        if other == item["payer"] or self.db.member(other) is None:
            return "Unknown member"
        kind = "full" if action == "f" else "settle"
        shares = shares_for(kind, item["amount"], item["payer"], [], other)
        if not self.db.share(item["id"], kind, shares, counterparty=other):
            return "Already handled"
        self._refresh(item["id"])
        self._notify(item["id"])
        return "Recorded"

    def _tap_part(self, item, action, args) -> str:
        if item["status"] != "pending":
            return "Already handled"
        text = (f"How much of {self._head(item)} was for the flat? "
                "Reply with the amount and I'll split it equally.")
        try:
            ask = self.tg.send(item["payer"], text, force_reply=True, placeholder="e.g. 300")
        except TelegramError as e:
            log.warning("could not ask about item %s: %s", item["id"], e)
            return "Couldn't ask, please try again"
        self.db.update_item(item["id"], ask_message_id=ask)  # the latest ask wins
        return "How much was shared?"

    def _tap_pick_view(self, item, action, args) -> str:
        if item["status"] != "pending":
            return "Already handled"
        if action == "k" and not self._pickable_lines(item):
            return "No order items to pick"
        self.db.update_item(item["id"], view="pick" if action == "k" else None)
        self._refresh(item["id"])
        return "Tick what was for the flat" if action == "k" else ""

    def _tap_tick(self, item, action, args) -> str:
        if item["status"] != "pending" or item["order_id"] is None or not args:
            return "Already handled"
        if not self.db.toggle_line(item["order_id"], args[0]):
            return "That item is gone"
        self._refresh(item["id"])
        return ""

    def _tap_split_ticked(self, item, action, args) -> str:
        if item["status"] != "pending":
            return "Already handled"
        order_lines = self._pickable_lines(item)
        ticked = [line for line in order_lines if line["ticked"]]
        if not ticked:
            return "Nothing ticked yet"
        members = [m["tg_id"] for m in self.db.members()]
        if len(members) < 2:
            return "Nobody to split with yet"
        part = self._picked_part(item, order_lines)
        if part <= 0:
            return "The ticked items come to nothing"
        everything = len(ticked) == len(order_lines)
        picked = None if everything else _snippet(", ".join(line["name"] for line in ticked))
        if not self.db.share_part(item["id"], part, split_equal(part, members, item["payer"]), picked):
            return "Already handled"
        self._refresh(item["id"])
        self._notify(item["id"])
        return "Split equally"

    def _tap_personal(self, item, action, args) -> str:
        if not self.db.mark_personal(item["id"]):
            return "Already handled"
        self._refresh(item["id"])
        return "Marked personal"

    def _tap_undo(self, item, action, args) -> str:
        shares, sent = self.db.shares(item["id"]), self.db.notices(item["id"])
        if not self.db.unshare(item["id"]):
            return "Already handled"
        self._refresh(item["id"])
        if sent:
            self._retract_notices(item, shares, sent)
        return "Undone"

    def _tap_rule(self, item, action, args) -> str:
        key = merchant_key(item["merchant"]) if item["merchant"] else ""
        if not key:
            return "No merchant to remember"
        if self.db.get_rule(item["payer"], key):
            return "Already handled"
        rule = "split" if action == "rs" else "personal"
        self.db.set_rule(item["payer"], key, item["merchant"], rule)
        self._refresh(item["id"])
        verb = "split" if rule == "split" else "skipped"
        return f"{item['merchant']} will be {verb} automatically"[:190]

    # --- messages
    def _on_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        user = msg.get("from") or {}
        uid = user.get("id")
        text = (msg.get("text") or "").strip()
        cmd, arg = "", ""
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            cmd, arg = head[1:].split("@")[0].lower(), arg.strip()

        member = self.db.member(uid)
        if member is None:
            if cmd == "start" and arg and secrets.compare_digest(arg.encode(), self._code.encode()):
                self._join(uid, user)
            else:
                self.tg.send(chat["id"], PRIVATE)
            return

        if cmd == "email":
            self._cmd_email(member, arg, msg.get("message_id"))
        elif cmd:
            self._command(member, cmd, arg)
        elif msg.get("photo"):
            self._on_photo(uid, msg)
        elif text and msg.get("reply_to_message"):
            replied = msg["reply_to_message"].get("message_id")
            # An answer to "how much was shared?" is never an edit or a new entry.
            if asked := self.db.item_by_ask(uid, replied):
                self._part_reply(asked, text)
                return
            item = self.db.item_by_message(uid, replied)
            if item:
                self._edit_by_reply(item, text)
            else:
                self._manual(uid, text)
        elif text:
            self._manual(uid, text)
        else:
            self.tg.send(uid, MANUAL_HINT)

    def _join(self, uid: int, user: dict) -> None:
        name = (user.get("first_name") or user.get("username") or "Member").strip()[:32] or "Member"
        others = [m["tg_id"] for m in self.db.members()]
        self.db.add_member(uid, name, secrets.token_urlsafe(24))
        self.tg.send(uid, f"Welcome, {escape(name)}! You're in.\n\n"
                          "Send /phone to set up SMS forwarding, or /help to see how this works.")
        for other in others:
            self._safe_send(other, f"{escape(name)} joined.")

    def _manual(self, uid: int, text: str) -> None:
        parsed = parse_manual(text)
        if parsed is None:
            self.tg.send(uid, MANUAL_HINT)
            return
        amount, description = parsed
        self._send_item(self._new_item(uid, amount, description, "manual"))

    def _part_reply(self, item, text: str) -> None:
        payer = item["payer"]
        if item["status"] != "pending":
            self.tg.send(payer, "That entry was already handled, so nothing changed.")
            return
        part = _amount_only(text)
        if part is None:
            self.tg.send(payer, "Reply with just the shared amount, e.g. <code>300</code>.")
            return
        if part <= 0:
            self.tg.send(payer, "The amount has to be more than zero.")
            return
        if part > item["amount"]:
            self.tg.send(payer, f"That can't be more than what was paid ({self._m(item['amount'])}).")
            return
        members = [m["tg_id"] for m in self.db.members()]
        if len(members) < 2:
            self.tg.send(payer, "Nobody to split with yet. Share the code from /invite.")
            return
        if not self.db.share_part(item["id"], part, split_equal(part, members, payer)):
            self.tg.send(payer, "That entry was already handled, so nothing changed.")
            return
        self._refresh(item["id"])
        self._notify(item["id"])
        self.tg.send(payer, "Recorded.")

    def _edit_by_reply(self, item, text: str) -> None:
        amount, description = item["amount"], item["description"]
        new_amount = _amount_only(text)
        if new_amount is not None:
            amount = new_amount
        elif parsed := parse_manual(text):
            amount, description = parsed
        else:
            description = " ".join(text.split())[:80]
        if amount <= 0:
            self.tg.send(item["payer"], "The amount has to be more than zero.")
            return
        if item["total"] is not None and amount > item["total"]:
            self.tg.send(item["payer"], f"That can't be more than what was paid ({self._m(item['total'])}).")
            return
        shares = None
        if item["status"] == "shared":
            if item["kind"] == "split":
                members = list(self.db.shares(item["id"])) or [m["tg_id"] for m in self.db.members()]
                shares = split_equal(amount, members, item["payer"])
            else:
                shares = shares_for(item["kind"], amount, item["payer"], [], item["counterparty"])
        self.db.edit_item(item["id"], amount, description, shares)
        self._refresh(item["id"])
        if shares is not None:
            self._notify(item["id"])
        self.tg.send(item["payer"], "Updated.")

    def _on_photo(self, uid: int, msg: dict) -> None:
        if self.extractor is None:
            self.tg.send(uid, "Photo reading is off: the server isn't signed in to ChatGPT. "
                              "Type it instead, e.g. <code>450 groceries</code>.")
            return
        best = max(msg["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0))
        image = self.tg.download(best["file_id"])
        self.tg.send(uid, "Reading the bill…")
        try:
            result = self.extractor.bill(image)
        except Exception as e:  # AIError lives in app.ai, which this module does not import
            log.warning("bill photo could not be read (%s)", type(e).__name__)
            self.tg.send(uid, "I couldn't read that bill right now. Type it instead, e.g. "
                              "<code>450 groceries</code>.")
            return
        if result is None:
            self.tg.send(uid, "No total found on that photo. Type it instead, e.g. <code>450 groceries</code>.")
            return
        amount, merchant = result
        description = (" ".join((msg.get("caption") or "").split()) or merchant or "Bill")[:80]
        self._send_item(self._new_item(uid, amount, description, "photo"))

    # --- commands
    def _command(self, member, cmd: str, arg: str) -> None:
        uid = member["tg_id"]
        handlers = {
            "start": self._cmd_start, "help": self._cmd_help, "balance": self._cmd_balance,
            "history": self._cmd_history, "phone": self._cmd_phone, "rules": self._cmd_rules,
            "invite": self._cmd_invite, "name": self._cmd_name, "export": self._cmd_export,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self.tg.send(uid, "Unknown command. /help lists what I can do.")
            return
        handler(member, arg)

    def _cmd_start(self, member, arg: str) -> None:
        self.tg.send(member["tg_id"], f"You're already in, {escape(member['name'])}.\n\n{self._help_text()}")

    def _help_text(self) -> str:
        return (
            "<b>How this works</b>\n"
            "When your bank texts you about a spend, I ask whether it was shared. One tap records it "
            "and tells your flatmate their share.\n\n"
            "• Cash spend: type <code>450 groceries</code>\n"
            "• Bill photo: send it and I'll read the total\n"
            "• Fix an entry: reply to its message with <code>dinner</code>, <code>520</code> "
            "or <code>520 dinner</code>\n"
            "• Only part of a payment was for the flat: tap <i>Only part is shared</i> and reply "
            "with that part\n"
            "• Mixed online order: connect your mailbox with /email. When the order email matches a "
            "payment, tap <i>Pick shared items</i> and tick what was for the flat\n"
            "• Bringing in a balance from elsewhere: type <code>1240 carried over</code> and tap "
            "<i>&lt;Name&gt; owes all</i>\n\n"
            "/balance who owes whom\n/history last 15 shared entries\n/rules merchants you always split or skip\n"
            "/phone set up SMS forwarding\n/invite join code for your flatmate\n/name change your name\n"
            "/export CSV of everything shared\n/email read bank alert and order emails"
        )

    def _email_off_reason(self) -> str | None:
        if self.mailbox is None or self.extractor is None:
            return "Order emails are off: the server isn't signed in to ChatGPT."
        return None

    def _cmd_email(self, member, arg: str, message_id: int | None) -> None:
        uid = member["tg_id"]
        tokens = arg.split()
        if not tokens:
            self.tg.send(uid, self._email_status(uid))
            return
        if len(tokens) == 1 and tokens[0].lower() == "off":
            gone = self.db.delete_mail_account(uid)
            self.tg.send(uid, "Mailbox disconnected." if gone else "No mailbox was connected.")
            return
        # The message holds a password: remove it from the chat whatever happens next.
        try:
            self.tg.delete_message(uid, message_id)
            leftover = ""
        except TelegramError as e:
            log.warning("could not delete an /email message: %s", e)
            leftover = "\n\nI couldn't delete your message with the password; please delete it yourself."
        off = self._email_off_reason()
        if off:
            self.tg.send(uid, off + leftover)
            return
        address, rest = tokens[0], tokens[1:]
        host = GMAIL_HOST
        if len(tokens) >= 3 and _HOSTNAME.match(rest[-1]):
            host = rest.pop().lower()
        password = "".join(rest)  # app passwords are shown in groups of four
        if "@" not in address or not password:
            self.tg.send(uid, "Usage: <code>/email you@gmail.com abcd efgh ijkl mnop</code>" + leftover)
            return
        try:
            self.mailbox.check_login(host, IMAP_PORT, address, password)
        except Exception as e:
            self.tg.send(uid, f"Couldn't sign in to {escape(host)}: {escape(str(e))}{leftover}")
            return
        self.db.set_mail_account(uid, address, password, host, IMAP_PORT)
        banks = ", ".join(escape(s) for s in self.bank_senders) or "none configured on the server"
        senders = ", ".join(escape(s) for s in self.mail_senders) or "none configured on the server yet"
        self.tg.send(uid, f"Connected {escape(address)}.\nI'll read bank alert emails from: {banks}.\n"
                          f"I'll read order emails from: {senders}.\n"
                          "I read nothing else, and never change or delete anything in your mailbox."
                          + leftover)

    def _email_status(self, uid: int) -> str:
        off = self._email_off_reason()
        if off:
            return off
        acc = self.db.mail_account(uid)
        status = (f"Connected: {escape(acc['address'])} ({escape(acc['host'])}). "
                  "Send <code>/email off</code> to disconnect." if acc else "No mailbox connected.")
        return (
            f"<b>Bank alert and order emails</b>\n{status}\n\n"
            "Bank alert emails (turn on email alerts in your bank's app) are read like bank SMS, so "
            "you don't need SMS forwarding.\n\n"
            "When an order email (Swiggy, Zomato, Blinkit, Amazon…) matches a payment, you can tick "
            "which items were for the flat.\n\n"
            "<b>Gmail</b>\n1. Turn on 2-step verification.\n"
            "2. Create an app password at https://myaccount.google.com/apppasswords\n"
            "3. Send <code>/email you@gmail.com abcd efgh ijkl mnop</code>\n\n"
            "<b>Other providers</b>: add the IMAP host at the end, e.g. "
            "<code>/email you@example.com password imap.example.com</code>\n\n"
            "I delete your message with the password straight away.")

    def _cmd_help(self, member, arg: str) -> None:
        self.tg.send(member["tg_id"], self._help_text())

    def _cmd_balance(self, member, arg: str) -> None:
        uid = member["tg_id"]
        names = self._names()
        plan = settle_up(nets(self.db.debts()))
        if not plan:
            self.tg.send(uid, "All settled up.")
            return
        lines = ["<b>Balance</b>"]
        for debtor, creditor, amount in plan:
            who = "You owe" if debtor == uid else f"{escape(names.get(debtor, 'someone'))} owes"
            whom = "you" if creditor == uid else escape(names.get(creditor, "someone"))
            lines.append(f"{who} {whom} {self._m(amount)}")
        self.tg.send(uid, "\n".join(lines))

    def _kind_word(self, item, names: dict[int, str]) -> str:
        cp = names.get(item["counterparty"], "someone")
        return {"split": "split", "full": f"for {cp}", "settle": f"paid {cp} back"}.get(item["kind"], "")

    def _cmd_history(self, member, arg: str) -> None:
        items = self.db.shared_items(limit=15, newest_first=True)
        if not items:
            self.tg.send(member["tg_id"], "Nothing shared yet.")
            return
        names = self._names()
        lines = ["<b>Recent shared entries</b>"]
        for it in items:
            lines.append(f"{self._local_date(it['created_at'], '%d %b')} · <b>{self._m(it['amount'])}</b> · "
                         f"{escape(it['description'])} · {escape(names.get(it['payer'], 'someone'))} · "
                         f"{escape(self._kind_word(it, names))}")
        self.tg.send(member["tg_id"], "\n".join(lines))

    def _cmd_phone(self, member, arg: str) -> None:
        path = f"/sms/{member['ingest_token']}"
        if self.public_url:
            where = f"Your personal webhook URL:\n<code>{escape(self.public_url + path)}</code>"
        else:
            where = ("The server's <code>PUBLIC_URL</code> is not set, so here is just the path; "
                     f"put your server's address in front of it:\n<code>{escape(path)}</code>")
        self.tg.send(member["tg_id"], (
            f"{where}\n\nKeep it to yourself: anyone with it can add spends in your name.\n\n"
            "<b>Setup on Android</b>\n"
            "1. Install an SMS forwarder app that can call a webhook.\n"
            "2. Add a webhook rule: method POST, the URL above, JSON body with the message in a "
            "<code>text</code> field, e.g. <code>{\"text\": \"&lt;message body&gt;\"}</code>.\n"
            "3. If the app can filter by sender, limit it to your banks.\n"
            "4. Make a small payment to test. Messages that aren't spends are dropped and not stored."))

    def _rules_view(self, uid: int) -> tuple[str, Keyboard]:
        rules = self.db.rules(uid)
        if not rules:
            return ("No rules yet. Use the <i>Always split</i> / <i>Always skip</i> buttons after "
                    "handling a spend to add one."), []
        lines = ["<b>Your rules</b>"]
        keyboard: Keyboard = []
        for r in rules:
            verb = "Always split" if r["action"] == "split" else "Always skip"
            lines.append(f"• {verb} {escape(r['merchant'])}")
            keyboard.append([(f"Delete: {verb.lower()} {r['merchant']}", f"rd:{r['id']}")])
        return "\n".join(lines), keyboard

    def _cmd_rules(self, member, arg: str) -> None:
        text, keyboard = self._rules_view(member["tg_id"])
        self.tg.send(member["tg_id"], text, keyboard)

    def _cmd_invite(self, member, arg: str) -> None:
        self.tg.send(member["tg_id"], f"Join code: <code>{self._code}</code>\n"
                                      f"Your flatmate should open this bot and send:\n"
                                      f"<code>/start {self._code}</code>")

    def _cmd_name(self, member, arg: str) -> None:
        name = " ".join(arg.split())[:32]
        if not name:
            self.tg.send(member["tg_id"], "Usage: <code>/name Rahul</code>")
            return
        self.db.rename_member(member["tg_id"], name)
        self.tg.send(member["tg_id"], f"You're now {escape(name)}.")

    def _cmd_export(self, member, arg: str) -> None:
        items = self.db.shared_items()
        if not items:
            self.tg.send(member["tg_id"], "Nothing shared yet.")
            return
        members = self.db.members()
        names = {m["tg_id"]: m["name"] for m in members}
        shares = self.db.all_shares()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "paid_by", "amount", "paid_total", "description", "kind"]
                        + [f"{m['name']} owed" for m in members])
        for it in items:
            owed = shares.get(it["id"], {})
            writer.writerow([self._local_date(it["created_at"], "%Y-%m-%d"), names.get(it["payer"], ""),
                             plain_amount(it["amount"]), plain_amount(it["total"] or it["amount"]),
                             it["description"], it["kind"]]
                            + [plain_amount(owed.get(m["tg_id"], 0)) for m in members])
        stamp = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.tg.send_document(member["tg_id"], f"ledger-{stamp}.csv", out.getvalue().encode(),
                              caption=f"{len(items)} shared entries")
