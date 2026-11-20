import csv
import io
import itertools
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from app.bot import Bot
from app.db import DB
from app.sms import Txn
from app.telegram import TelegramError

MIHIR, RAHUL, PRIYA, STRANGER = 101, 202, 303, 999


def sms(amount="450.00", merchant="BLINKIT", ref="123456789012"):
    return (f"Sent Rs.{amount}\nFrom HDFC Bank A/C *1234\nTo {merchant}\nOn 20/11/26\nRef {ref}\n"
            "Not You?\nCall 18002586161/SMS BLOCK UPI to 7308080808")


NO_MERCHANT_SMS = "Rs.120.00 debited from A/c XX1234 on 20-11-26. Avl Bal Rs 5000"


def fake_ai_sms(text):
    """Stands in for the AI SMS reader; it understands the sample texts these tests send."""
    amount = re.search(r"Rs\.(\d+)\.(\d\d)", text)
    if amount is None or "OTP" in text:
        return None
    merchant = re.search(r"\nTo ([^\n]+)", text)
    ref = re.search(r"\nRef (\d+)", text)
    return Txn(int(amount.group(1)) * 100 + int(amount.group(2)),
               merchant.group(1).title() if merchant else None, ref.group(1) if ref else None)


class AIError(Exception):
    """Stands in for app.ai.AIError, which the bot must not import."""


class FakeMailbox:
    def __init__(self):
        self.logins = []
        self.login_error = None
        self.fetch_calls = []
        self.fetch_error = None
        self.batches = []       # each poll pops one list of mails

    def check_login(self, host, port, user, password):
        self.logins.append((host, port, user, password))
        if self.login_error:
            raise self.login_error

    def fetch_new(self, host, port, user, password, *, uidvalidity, last_uid, senders, limit=20):
        self.fetch_calls.append({"user": user, "uidvalidity": uidvalidity, "last_uid": last_uid,
                                 "senders": tuple(senders), "limit": limit})
        if self.fetch_error:
            raise self.fetch_error
        mails = self.batches.pop(0) if self.batches else []
        return SimpleNamespace(uidvalidity=7, last_uid=(last_uid or 0) + len(mails), mails=tuple(mails))


def mail(subject, uid=1):
    return SimpleNamespace(uid=uid, sender="orders@blinkit.example", subject=subject, body="secret body")


def order(total, lines, merchant="Blinkit"):
    return SimpleNamespace(merchant=merchant, total=total, lines=tuple(lines))


class FakeTelegram:
    def __init__(self):
        self.ids = itertools.count(1000)
        self.sent = []          # (chat_id, message_id, text, keyboard)
        self.edits = []         # (chat_id, message_id, text, keyboard)
        self.answers = []       # (callback_id, text)
        self.docs = []          # (chat_id, filename, data)
        self.current = {}       # (chat_id, message_id) -> (text, keyboard)
        self.fail_sends = 0
        self.fail_for = set()   # chat ids whose sends fail
        self.files = {}
        self.force_replies = {}  # (chat_id, message_id) -> placeholder
        self.deleted = []        # (chat_id, message_id)
        self.fail_delete = False

    def send(self, chat_id, text, keyboard=None, *, force_reply=False, placeholder=None):
        assert not (force_reply and keyboard), "ForceReply cannot carry buttons"
        if self.fail_sends or chat_id in self.fail_for:
            self.fail_sends = max(0, self.fail_sends - 1)
            raise TelegramError("sendMessage: network down")
        mid = next(self.ids)
        if force_reply:
            self.force_replies[(chat_id, mid)] = placeholder
        self.sent.append((chat_id, mid, text, keyboard))
        self.current[(chat_id, mid)] = (text, keyboard)
        return mid

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.edits.append((chat_id, message_id, text, keyboard))
        self.current[(chat_id, message_id)] = (text, keyboard)

    def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise TelegramError("deleteMessage: message can't be deleted")
        self.deleted.append((chat_id, message_id))

    def answer(self, callback_id, text=None, alert=False):
        self.answers.append((callback_id, text))

    def download(self, file_id):
        return self.files[file_id]

    def send_document(self, chat_id, filename, data, caption=""):
        self.docs.append((chat_id, filename, data))
        return next(self.ids)

    # helpers for assertions
    def last_to(self, chat_id):
        return [s for s in self.sent if s[0] == chat_id][-1]

    def to(self, chat_id):
        return [s for s in self.sent if s[0] == chat_id]


def buttons(keyboard):
    return {label: data for row in keyboard or [] for label, data in row}


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc)
        self.db = DB(":memory:", clock=lambda: self.now)
        self.addCleanup(self.db.close)
        self.tg = FakeTelegram()
        self.ai = mock.Mock(**{"sms.side_effect": fake_ai_sms})
        self.bot = Bot(self.db, self.tg, extractor=self.ai)
        self.update_ids = itertools.count(1)
        self.msg_ids = itertools.count(1)

    def make_bot(self, **kwargs):
        self.bot = Bot(self.db, self.tg, **kwargs)
        return self.bot

    def advance(self, **delta):
        self.now += timedelta(**delta)

    # --- driving the bot
    def message(self, uid, text=None, *, name="X", reply_to=None, chat_type="private", **extra):
        msg = {"message_id": next(self.msg_ids), "chat": {"id": uid, "type": chat_type},
               "from": {"id": uid, "first_name": name}, **extra}
        if text is not None:
            msg["text"] = text
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": reply_to}
        self.bot.handle_update({"update_id": next(self.update_ids), "message": msg})

    def tap(self, uid, data, message_id=1):
        cid = f"cb{next(self.update_ids)}"
        self.bot.handle_update({"update_id": next(self.update_ids), "callback_query": {
            "id": cid, "from": {"id": uid}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": uid, "type": "private"}}}})
        return [a[1] for a in self.tg.answers if a[0] == cid][-1]

    def join(self, uid, name):
        self.message(uid, f"/start {self.bot.join_code}", name=name)

    def token(self, uid):
        return self.db.member(uid)["ingest_token"]

    def prompt(self, uid):
        """(message_id, text, buttons) of the latest message the bot sent to uid, as it looks now."""
        _, mid, _, _ = self.tg.last_to(uid)
        text, keyboard = self.tg.current[(uid, mid)]
        return mid, text, buttons(keyboard)

    def press(self, uid, label, message_id=None):
        if message_id is None:
            message_id = self.prompt(uid)[0]
        return self.tap(uid, buttons(self.tg.current[(uid, message_id)][1])[label], message_id)

    def balance_text(self, uid):
        self.message(uid, "/balance")
        return self.tg.last_to(uid)[2]


class TwoMembers(BotTestCase):
    def setUp(self):
        super().setUp()
        self.join(MIHIR, "Mihir")
        self.join(RAHUL, "Rahul")
        self.tg.sent.clear()  # drop the welcome / joined messages so tests count only their own

    def ingest(self, text=None, uid=MIHIR):
        """Queue an SMS and let the inbox worker handle it, as the background thread would."""
        status = self.bot.ingest_sms(self.token(uid), text or sms())
        self.bot.process_inbox()
        return status


class JoinTest(BotTestCase):
    def test_join_code_is_stable(self):
        self.assertEqual(len(self.bot.join_code), 8)
        self.assertEqual(Bot(self.db, self.tg).join_code, self.bot.join_code)

    def test_wrong_then_right_code(self):
        self.message(MIHIR, "/start nope", name="Mihir")
        self.assertIsNone(self.db.member(MIHIR))
        self.assertIn("private", self.tg.last_to(MIHIR)[2])
        self.message(MIHIR, "/start ünïcode", name="Mihir")
        self.assertIsNone(self.db.member(MIHIR))
        self.join(MIHIR, "Mihir")
        member = self.db.member(MIHIR)
        self.assertEqual(member["name"], "Mihir")
        self.assertGreater(len(member["ingest_token"]), 20)
        self.assertIn("Welcome", self.tg.last_to(MIHIR)[2])

    def test_other_members_hear_about_a_join(self):
        self.join(MIHIR, "Mihir")
        self.join(RAHUL, "Rahul")
        self.assertIn("Rahul joined", self.tg.last_to(MIHIR)[2])

    def test_unjoined_user_is_blocked(self):
        self.join(MIHIR, "Mihir")
        self.message(STRANGER, "450 groceries")
        self.message(STRANGER, "/balance")
        texts = [s[2] for s in self.tg.to(STRANGER)]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all("private" in t for t in texts))
        self.bot.ingest_sms(self.token(MIHIR), sms())
        self.bot.process_inbox()
        mid, _, btns = self.prompt(MIHIR)
        answer = self.tap(STRANGER, btns["Personal"], mid)
        self.assertIn("private", answer)
        self.assertEqual(self.db.item(1)["status"], "pending")

    def test_group_chats_are_ignored(self):
        self.message(MIHIR, f"/start {self.bot.join_code}", chat_type="group")
        self.assertEqual(self.tg.sent, [])
        self.assertIsNone(self.db.member(MIHIR))

    def test_name_change(self):
        self.join(MIHIR, "Mihir")
        self.message(MIHIR, "/name  Mihir   S ")
        self.assertEqual(self.db.member(MIHIR)["name"], "Mihir S")

    def test_alone_only_personal_is_offered(self):
        self.join(MIHIR, "Mihir")
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), sms()), "queued")
        self.bot.process_inbox()
        _, text, btns = self.prompt(MIHIR)
        self.assertEqual(list(btns), ["Personal"])
        self.assertIn("/invite", text)

    def test_handle_update_never_raises(self):
        with self.assertLogs("app.bot", "ERROR") as logs:
            self.bot.handle_update({"update_id": 1, "message": None})
            self.bot.handle_update({"update_id": 2, "callback_query": {}})
        self.assertEqual(len(logs.records), 2)


class SmsFlowTest(TwoMembers):
    def test_split_flow(self):
        self.assertEqual(self.ingest(), "queued")
        mid, text, btns = self.prompt(MIHIR)
        self.assertIn("<b>₹450</b> · Blinkit", text)
        self.assertIn("Shared?", text)
        self.assertEqual(set(btns), {"Split equally", "Rahul owes all", "Paid Rahul back",
                                     "Only part is shared", "Personal"})
        self.assertTrue(all(len(d.encode()) < 64 for d in btns.values()))

        self.press(MIHIR, "Split equally")
        self.assertEqual(self.db.shares(1), {MIHIR: 22500, RAHUL: 22500})
        text, keyboard = self.tg.current[(MIHIR, mid)]
        self.assertIn("✅ <b>₹450</b> · Blinkit", text)
        self.assertIn("Split equally · Rahul owes you ₹225", text)
        self.assertEqual(set(buttons(keyboard)), {"Undo", "Always split Blinkit"})

        notice = self.tg.last_to(RAHUL)[2]
        self.assertIn("Mihir added <b>₹450</b> · Blinkit", notice)
        self.assertIn("Your share: ₹225", notice)
        self.assertIn("You owe Mihir ₹225 in total", notice)
        self.assertIn("Rahul owes you ₹225", self.balance_text(MIHIR))
        self.assertIn("You owe Mihir ₹225", self.balance_text(RAHUL))

    def test_odd_paise_split(self):
        self.ingest(sms("450.01"))
        self.press(MIHIR, "Split equally")
        self.assertEqual(self.db.shares(1), {MIHIR: 22501, RAHUL: 22500})
        self.assertIn("Rahul owes you ₹225", self.balance_text(MIHIR))

    def test_full_and_settle(self):
        self.ingest(sms("300.00", ref="111111111111"))
        self.press(MIHIR, "Rahul owes all")
        self.assertEqual(self.db.shares(1), {RAHUL: 30000})
        self.assertIn("Your share: ₹300", self.tg.last_to(RAHUL)[2])
        self.assertIn("Rahul owes all", self.prompt(MIHIR)[1])

        self.ingest(sms("1000.00", ref="222222222222"), uid=RAHUL)
        self.press(RAHUL, "Paid Mihir back")
        self.assertEqual(self.db.shares(2), {MIHIR: 100000})
        notice = self.tg.last_to(MIHIR)[2]
        self.assertIn("Rahul paid you ₹1,000", notice)
        self.assertIn("You owe Rahul ₹700 in total", notice)
        self.assertIn("Paid Mihir back", self.prompt(RAHUL)[1])
        self.assertIn("You owe Rahul ₹700", self.balance_text(MIHIR))

    def test_double_tap_is_harmless(self):
        self.ingest()
        mid, _, btns = self.prompt(MIHIR)
        self.tap(MIHIR, btns["Split equally"], mid)
        self.assertEqual(self.tap(MIHIR, btns["Split equally"], mid), "Already handled")
        self.assertEqual(self.tap(MIHIR, btns["Personal"], mid), "Already handled")
        self.assertEqual(self.db.item(1)["status"], "shared")
        self.assertEqual(len(self.tg.to(RAHUL)), 1)

    def test_only_payer_can_act(self):
        self.ingest()
        _, _, btns = self.prompt(MIHIR)
        self.assertIn("Only", self.tap(RAHUL, btns["Split equally"]))
        self.assertEqual(self.db.item(1)["status"], "pending")

    def test_undo(self):
        self.ingest()
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Split equally")
        notice_chat, notice_id, _, _ = self.tg.last_to(RAHUL)
        self.assertEqual(self.press(MIHIR, "Undo", mid), "Undone")
        self.assertEqual(self.db.shares(1), {})
        self.assertEqual(self.db.item(1)["status"], "pending")
        text, keyboard = self.tg.current[(MIHIR, mid)]
        self.assertIn("Shared?", text)
        self.assertIn("Split equally", buttons(keyboard))
        notice = self.tg.current[(RAHUL, notice_id)][0]
        self.assertIn("<s>", notice)
        self.assertIn("Removed by Mihir", notice)
        self.assertIn("settled up", notice)
        self.assertEqual(self.balance_text(MIHIR), "All settled up.")
        # double undo is harmless; re-sharing sends a fresh notice
        self.assertEqual(self.tap(MIHIR, f"u:1", mid), "Already handled")
        self.press(MIHIR, "Split equally", mid)
        self.assertEqual(len(self.tg.to(RAHUL)), 2)

    def test_personal(self):
        self.ingest()
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Personal")
        text, keyboard = self.tg.current[(MIHIR, mid)]
        self.assertIn("🚫", text)
        self.assertIn("Personal, not shared", text)
        self.assertEqual(set(buttons(keyboard)), {"Undo", "Always skip Blinkit"})
        self.assertEqual(self.tg.to(RAHUL), [])
        self.assertEqual(self.balance_text(MIHIR), "All settled up.")

    def test_always_split_rule(self):
        self.ingest(sms(ref="111111111111"))
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Split equally")
        self.press(MIHIR, "Always split Blinkit", mid)
        self.assertNotIn("Always split Blinkit", buttons(self.tg.current[(MIHIR, mid)][1]))
        self.assertEqual(self.db.get_rule(MIHIR, "blinkit"), "split")

        self.assertEqual(self.ingest(sms("200.00", ref="222222222222")), "queued")
        item = self.db.item(2)
        self.assertEqual((item["status"], item["kind"]), ("shared", "split"))
        _, text, btns = self.prompt(MIHIR)
        self.assertTrue(text.startswith("⚡"))
        self.assertIn("rule", text)
        self.assertEqual(set(btns), {"Undo"})
        self.assertIn("You owe Mihir ₹325 in total", self.tg.last_to(RAHUL)[2])

        # undo keeps the rule
        self.press(MIHIR, "Undo")
        self.assertEqual(self.db.item(2)["status"], "pending")
        self.assertEqual(self.db.get_rule(MIHIR, "blinkit"), "split")

    def test_always_skip_rule_and_rules_command(self):
        self.ingest(sms(merchant="NETFLIX", ref="111111111111"))
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Personal")
        self.press(MIHIR, "Always skip Netflix", mid)
        sent_before = len(self.tg.sent)
        self.assertEqual(self.ingest(sms(merchant="NETFLIX", ref="222222222222")), "queued")
        self.assertEqual(self.db.item(2)["status"], "personal")
        self.assertEqual(len(self.tg.sent), sent_before)

        self.message(MIHIR, "/rules")
        mid, text, btns = self.prompt(MIHIR)
        self.assertIn("Always skip Netflix", text)
        self.tap(MIHIR, next(iter(btns.values())), mid)
        self.assertIsNone(self.db.get_rule(MIHIR, "netflix"))
        self.assertIn("No rules", self.tg.current[(MIHIR, mid)][0])

    def test_duplicate_sms(self):
        self.assertEqual(self.ingest(), "queued")
        self.assertEqual(self.ingest(), "duplicate")
        self.assertEqual(len(self.tg.to(MIHIR)), 1)
        # a different text about the same transaction (same ref and amount) adds nothing
        self.assertEqual(self.ingest(sms().replace("Not You?", "Not you? Call us")), "queued")
        self.assertIsNone(self.db.item(2))
        # without a ref the whitespace-normalised text is the key
        self.assertEqual(self.ingest(NO_MERCHANT_SMS), "queued")
        self.assertEqual(self.ingest(NO_MERCHANT_SMS.replace(" ", "  ")), "duplicate")
        # the same ref for another person is a different spend
        self.assertEqual(self.ingest(uid=RAHUL), "queued")
        self.assertEqual(self.db.item(3)["payer"], RAHUL)

    def test_ignored_sms_leaves_nothing(self):
        self.assertEqual(self.ingest("Your OTP is 123456 for a payment of Rs 500. Do not share."), "ignored")
        self.assertEqual(self.ingest("Hello there"), "ignored")
        self.assertIsNone(self.db.item(1))
        self.assertEqual(self.db._all("SELECT * FROM inbox"), [])
        self.assertEqual(self.bot.ingest_sms("bad-token", sms()), "unknown")
        self.assertEqual(self.bot.ingest_sms("", sms()), "unknown")
        self.assertIsNone(self.db.item(1))

    def test_resend_when_first_prompt_failed(self):
        self.tg.fail_sends = 1
        with self.assertLogs("app.bot", "WARNING"):
            self.assertEqual(self.ingest(), "queued")  # the send failure is not the phone's problem
        self.assertIsNone(self.db.item(1)["message_id"])
        self.bot.process_inbox()
        self.assertIsNone(self.db.item(1)["message_id"])  # too fresh to resend yet
        self.advance(minutes=2)
        self.bot.process_inbox()
        mid, text, _ = self.prompt(MIHIR)
        self.assertEqual(self.db.item(1)["message_id"], mid)
        self.assertIn("Blinkit", text)
        self.advance(minutes=2)
        self.bot.process_inbox()
        self.assertEqual(len([s for s in self.tg.to(MIHIR) if "Blinkit" in s[2]]), 1)

    def test_resend_rule_added_item(self):
        self.db.set_rule(MIHIR, "blinkit", "Blinkit", "split")
        self.tg.fail_for.add(MIHIR)
        with self.assertLogs("app.bot", "WARNING"):
            self.ingest()
        self.assertEqual(self.db.item(1)["status"], "shared")
        self.assertIsNone(self.db.item(1)["message_id"])
        self.assertIn("Your share: ₹225", self.tg.last_to(RAHUL)[2])
        self.tg.fail_for.clear()
        self.advance(minutes=2)
        self.bot.process_inbox()
        self.assertTrue(self.prompt(MIHIR)[1].startswith("⚡"))

    def test_failed_notice_does_not_fail_the_tap(self):
        self.ingest()
        self.tg.fail_for.add(RAHUL)
        with self.assertLogs("app.bot", "WARNING"):
            self.press(MIHIR, "Split equally")
        self.assertEqual(self.db.item(1)["status"], "shared")
        self.assertIn("✅", self.prompt(MIHIR)[1])

    def test_unknown_merchant_shows_sms(self):
        self.ingest(NO_MERCHANT_SMS)
        mid, text, _ = self.prompt(MIHIR)
        self.assertIn("· Payment", text)
        self.assertIn("<i>Rs.120.00 debited", text)
        self.assertIn("Reply", text)
        self.message(MIHIR, "chai & snacks", reply_to=mid)
        self.assertEqual(self.db.item(1)["description"], "chai & snacks")
        self.assertIn("chai &amp; snacks", self.tg.current[(MIHIR, mid)][0])


class ManualAndEditTest(TwoMembers):
    def test_manual_entry_both_orders(self):
        self.message(MIHIR, "450 groceries")
        self.message(MIHIR, "dinner 1,200.50")
        a, b = self.db.item(1), self.db.item(2)
        self.assertEqual((a["amount"], a["description"], a["source"]), (45000, "groceries", "manual"))
        self.assertEqual((b["amount"], b["description"]), (120050, "dinner"))
        self.assertIn("<b>₹1,200.50</b> · dinner", self.prompt(MIHIR)[1])
        self.assertEqual(self.db.item(2)["message_id"], self.prompt(MIHIR)[0])

    def test_manual_hint(self):
        self.message(MIHIR, "hello")
        self.assertIn("450 groceries", self.tg.last_to(MIHIR)[2])
        self.assertIsNone(self.db.item(1))

    def test_edit_shared_item_by_reply(self):
        self.ingest()
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Split equally")
        notice_id = self.tg.last_to(RAHUL)[1]

        self.message(MIHIR, "dinner", reply_to=mid)
        self.assertEqual(self.db.item(1)["description"], "dinner")
        self.assertIn("· dinner", self.tg.current[(MIHIR, mid)][0])

        self.message(MIHIR, "520", reply_to=mid)
        self.assertEqual(self.db.item(1)["amount"], 52000)
        self.assertEqual(self.db.shares(1), {MIHIR: 26000, RAHUL: 26000})

        self.message(MIHIR, "601 team dinner", reply_to=mid)
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["description"]), (60100, "team dinner"))
        self.assertEqual(self.db.shares(1), {MIHIR: 30050, RAHUL: 30050})
        text = self.tg.current[(MIHIR, mid)][0]
        self.assertIn("<b>₹601</b> · team dinner", text)
        self.assertIn("Rahul owes you ₹300.50", text)
        notice = self.tg.current[(RAHUL, notice_id)][0]
        self.assertIn("Your share: ₹300.50", notice)
        self.assertIn("You owe Mihir ₹300.50 in total", notice)
        self.assertEqual(len(self.tg.to(RAHUL)), 1)  # edited, not re-sent
        self.assertEqual(item["merchant"], "Blinkit")  # per-merchant rules stay keyed on the SMS merchant

    def test_edit_full_item_amount(self):
        self.message(MIHIR, "300 movie")
        mid = self.prompt(MIHIR)[0]
        self.press(MIHIR, "Rahul owes all")
        self.message(MIHIR, "350", reply_to=mid)
        self.assertEqual(self.db.shares(1), {RAHUL: 35000})

    def test_reply_to_other_message_is_manual_entry(self):
        self.message(MIHIR, "/help")
        help_id = self.tg.last_to(MIHIR)[1]
        self.message(MIHIR, "90 chai", reply_to=help_id)
        self.assertEqual(self.db.item(1)["description"], "chai")


class CommandTest(TwoMembers):
    def setUp(self):
        super().setUp()
        self.message(MIHIR, "900 groceries")
        self.press(MIHIR, "Split equally")
        self.message(RAHUL, "100 <b>chai</b>")
        self.press(RAHUL, "Mihir owes all")
        self.message(MIHIR, "50 personal stuff")
        self.press(MIHIR, "Personal")

    def test_balance(self):
        self.assertEqual(self.balance_text(MIHIR), "<b>Balance</b>\nRahul owes you ₹350")
        self.assertEqual(self.balance_text(RAHUL), "<b>Balance</b>\nYou owe Mihir ₹350")

    def test_history(self):
        self.message(MIHIR, "/history")
        lines = self.tg.last_to(MIHIR)[2].splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("₹100</b> · &lt;b&gt;chai&lt;/b&gt; · Rahul · for Mihir", lines[1])
        self.assertIn("₹900</b> · groceries · Mihir · split", lines[2])

    def test_export(self):
        self.message(RAHUL, "/export")
        chat_id, filename, data = self.tg.docs[-1]
        self.assertEqual(chat_id, RAHUL)
        self.assertTrue(filename.endswith(".csv"))
        rows = list(csv.reader(io.StringIO(data.decode())))
        self.assertEqual(rows[0], ["date", "paid_by", "amount", "paid_total", "description", "kind",
                                   "Mihir owed", "Rahul owed"])
        self.assertEqual(rows[1][1:], ["Mihir", "900.00", "900.00", "groceries", "split", "450.00", "450.00"])
        self.assertEqual(rows[2][1:], ["Rahul", "100.00", "100.00", "<b>chai</b>", "full", "100.00", "0.00"])
        self.assertEqual(len(rows), 3)

    def test_phone(self):
        self.message(MIHIR, "/phone")
        text = self.tg.last_to(MIHIR)[2]
        self.assertIn(f"/sms/{self.token(MIHIR)}", text)
        self.assertIn("PUBLIC_URL", text)
        bot = Bot(self.db, self.tg, public_url="https://home.example/")
        bot.handle_update({"update_id": 99, "message": {
            "message_id": 99, "chat": {"id": MIHIR, "type": "private"}, "from": {"id": MIHIR}, "text": "/phone"}})
        self.assertIn(f"https://home.example/sms/{self.token(MIHIR)}", self.tg.last_to(MIHIR)[2])

    def test_invite_and_help(self):
        self.message(MIHIR, "/invite")
        self.assertIn(self.bot.join_code, self.tg.last_to(MIHIR)[2])
        self.message(MIHIR, "/help")
        help_text = self.tg.last_to(MIHIR)[2]
        self.assertIn("450 groceries", help_text)
        self.assertIn("carried over", help_text)
        self.assertTrue(all(f"/{c}" in help_text for c, _ in Bot.COMMANDS if c != "help"))


class PhotoTest(TwoMembers):
    def photo(self, caption=None):
        self.tg.files = {"small": b"s", "big": b"BIG"}
        extra = {"photo": [{"file_id": "small", "width": 90, "height": 90},
                           {"file_id": "big", "width": 1280, "height": 960}]}
        if caption:
            extra["caption"] = caption
        self.message(MIHIR, **extra)

    def test_reader_off(self):
        self.make_bot()  # no extractor
        self.photo()
        self.assertIn("isn't signed in to ChatGPT", self.tg.last_to(MIHIR)[2])
        self.assertIsNone(self.db.item(1))

    def test_extractor_reads_bill(self):
        extractor = mock.Mock()
        extractor.bill.return_value = (123450, "Toit")
        self.make_bot(extractor=extractor)
        self.photo()
        extractor.bill.assert_called_once_with(b"BIG")
        self.assertEqual(self.tg.to(MIHIR)[0][2], "Reading the bill…")
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["description"], item["source"]), (123450, "Toit", "photo"))
        self.assertIn("Shared?", self.prompt(MIHIR)[1])
        self.photo(caption="team lunch")
        self.assertEqual(self.db.item(2)["description"], "team lunch")
        extractor.bill.return_value = (5000, "")
        self.photo()
        self.assertEqual(self.db.item(3)["description"], "Bill")

    def test_extractor_finds_nothing(self):
        self.make_bot(extractor=mock.Mock(**{"bill.return_value": None}))
        self.photo()
        self.assertIn("No total", self.tg.last_to(MIHIR)[2])
        self.assertIsNone(self.db.item(1))

    def test_extractor_raises(self):
        self.make_bot(extractor=mock.Mock(**{"bill.side_effect": AIError("codex exited 1")}))
        with self.assertLogs("app.bot", "WARNING") as logs:
            self.photo()
        self.assertIn("couldn't read that bill right now", self.tg.last_to(MIHIR)[2])
        self.assertNotIn("codex exited", "\n".join(logs.output))
        self.assertIsNone(self.db.item(1))


class ThreeMembersTest(TwoMembers):
    def test_three_way_split(self):
        self.join(PRIYA, "Priya")
        self.ingest(sms("100.00"))
        _, _, btns = self.prompt(MIHIR)
        self.assertIn("Priya owes all", btns)
        self.press(MIHIR, "Split equally")
        self.assertEqual(self.db.shares(1), {MIHIR: 3334, RAHUL: 3333, PRIYA: 3333})
        self.assertIn("Rahul, Priya owe you ₹33.33 each", self.prompt(MIHIR)[1])
        self.assertIn("Your share: ₹33.33", self.tg.last_to(PRIYA)[2])
        self.assertIn("Your share: ₹33.33", self.tg.last_to(RAHUL)[2])
        self.message(RAHUL, "60 milk")
        self.press(RAHUL, "Priya owes all")
        # nets: Mihir +66.66, Rahul +26.67, Priya -93.33; settle-up is simplified
        self.assertEqual(self.balance_text(MIHIR),
                         "<b>Balance</b>\nPriya owes you ₹66.66\nPriya owes Rahul ₹26.67")
        self.assertIn("You owe Mihir ₹66.66", self.balance_text(PRIYA))

    def test_split_shares_survive_a_later_join(self):
        self.ingest(sms("100.00"))
        self.press(MIHIR, "Split equally")
        self.join(PRIYA, "Priya")
        self.assertEqual(self.db.shares(1), {MIHIR: 5000, RAHUL: 5000})
        mid = self.db.item(1)["message_id"]
        self.message(MIHIR, "200", reply_to=mid)
        self.assertEqual(self.db.shares(1), {MIHIR: 10000, RAHUL: 10000})


class PartShareTest(TwoMembers):
    def setUp(self):
        super().setUp()
        self.ingest(sms("451.00"))
        self.item_mid = self.prompt(MIHIR)[0]

    def ask(self):
        self.assertEqual(self.press(MIHIR, "Only part is shared", self.item_mid), "How much was shared?")
        return self.tg.last_to(MIHIR)[1]

    def reply(self, ask_mid, text):
        self.message(MIHIR, text, reply_to=ask_mid)
        return self.tg.last_to(MIHIR)[2]

    def test_button_only_with_sharing_buttons(self):
        keyboard = self.tg.current[(MIHIR, self.item_mid)][1]
        labels = [[label for label, _ in row] for row in keyboard]
        self.assertEqual(labels[-2:], [["Only part is shared"], ["Personal"]])
        alone_db = DB(":memory:")
        self.addCleanup(alone_db.close)
        alone = Bot(alone_db, FakeTelegram())
        alone.handle_update({"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": MIHIR, "type": "private"}, "from": {"id": MIHIR, "first_name": "M"},
            "text": f"/start {alone.join_code}"}})
        alone.handle_update({"update_id": 2, "message": {
            "message_id": 2, "chat": {"id": MIHIR, "type": "private"}, "from": {"id": MIHIR}, "text": "451 x"}})
        _, mid, _, _ = alone.tg.last_to(MIHIR)
        self.assertNotIn("Only part is shared", buttons(alone.tg.current[(MIHIR, mid)][1]))

    def test_tap_sends_force_reply_ask(self):
        ask_mid = self.ask()
        _, _, text, keyboard = self.tg.last_to(MIHIR)
        self.assertIn("How much of <b>₹451</b> · Blinkit was for the flat?", text)
        self.assertIsNone(keyboard)
        self.assertEqual(self.tg.force_replies[(MIHIR, ask_mid)], "e.g. 300")
        self.assertEqual(self.db.item(1)["ask_message_id"], ask_mid)
        self.assertEqual(self.db.item(1)["status"], "pending")
        self.assertIn("Split equally", buttons(self.tg.current[(MIHIR, self.item_mid)][1]))
        second = self.ask()  # asking again: the latest ask wins
        self.assertEqual(self.db.item(1)["ask_message_id"], second)

    def test_valid_part(self):
        ask_mid = self.ask()
        self.assertEqual(self.reply(ask_mid, "300"), "Recorded.")
        item = self.db.item(1)
        self.assertEqual((item["status"], item["kind"], item["amount"], item["total"]),
                         ("shared", "split", 30000, 45100))
        self.assertEqual(self.db.shares(1), {MIHIR: 15000, RAHUL: 15000})
        text, keyboard = self.tg.current[(MIHIR, self.item_mid)]
        self.assertTrue(text.startswith("✅ <b>₹300</b> of ₹451 · Blinkit"))
        self.assertIn("Split equally · Rahul owes you ₹150", text)
        self.assertEqual(set(buttons(keyboard)), {"Undo"})  # no "Always split" on a part split
        notice = self.tg.last_to(RAHUL)[2]
        self.assertEqual(notice, "Mihir added <b>₹300</b> · Blinkit\nPart of a ₹451 payment\n"
                                 "Your share: ₹150\nYou owe Mihir ₹150 in total")
        self.assertIn("Rahul owes you ₹150", self.balance_text(MIHIR))
        self.message(MIHIR, "/history")
        self.assertIn("<b>₹300</b> · Blinkit · Mihir · split", self.tg.last_to(MIHIR)[2])

    def test_odd_paise_part(self):
        self.reply(self.ask(), "₹300.01")
        self.assertEqual(self.db.shares(1), {MIHIR: 15001, RAHUL: 15000})

    def test_part_equal_to_full_amount(self):
        self.reply(self.ask(), "451")
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["total"]), (45100, None))
        text, keyboard = self.tg.current[(MIHIR, self.item_mid)]
        self.assertNotIn(" of ", text)
        self.assertIn("Always split Blinkit", buttons(keyboard))
        self.assertNotIn("Part of", self.tg.last_to(RAHUL)[2])

    def test_part_over_amount_keeps_ask(self):
        ask_mid = self.ask()
        self.assertEqual(self.reply(ask_mid, "500"), "That can't be more than what was paid (₹451).")
        self.assertEqual(self.db.item(1)["status"], "pending")
        self.reply(ask_mid, "0")
        self.assertEqual(self.db.item(1)["status"], "pending")
        self.reply(ask_mid, "300")
        self.assertEqual(self.db.item(1)["amount"], 30000)

    def test_non_numeric_reply(self):
        ask_mid = self.ask()
        self.assertIn("just the shared amount", self.reply(ask_mid, "300 groceries"))
        self.assertIn("just the shared amount", self.reply(ask_mid, "about half"))
        self.assertIsNone(self.db.item(2))  # never a manual entry
        self.assertEqual(self.db.item(1)["description"], "Blinkit")  # never an edit
        self.reply(ask_mid, "300")
        self.assertEqual(self.db.item(1)["status"], "shared")

    def test_reply_after_already_handled(self):
        ask_mid = self.ask()
        self.press(MIHIR, "Personal", self.item_mid)
        self.assertIn("already handled", self.reply(ask_mid, "300"))
        item = self.db.item(1)
        self.assertEqual((item["status"], item["amount"], item["total"]), ("personal", 45100, None))

    def test_split_button_after_ask_then_late_reply(self):
        ask_mid = self.ask()
        self.assertEqual(self.press(MIHIR, "Split equally", self.item_mid), "Split equally")
        self.assertEqual(self.db.shares(1), {MIHIR: 22550, RAHUL: 22550})
        self.assertIn("already handled", self.reply(ask_mid, "300"))
        self.assertEqual(self.db.shares(1), {MIHIR: 22550, RAHUL: 22550})
        self.assertIsNone(self.db.item(1)["total"])

    def test_undo_restores_full_amount(self):
        self.reply(self.ask(), "300")
        notice_id = self.tg.last_to(RAHUL)[1]
        self.assertEqual(self.press(MIHIR, "Undo", self.item_mid), "Undone")
        item = self.db.item(1)
        self.assertEqual((item["status"], item["amount"], item["total"]), ("pending", 45100, None))
        self.assertEqual(self.db.shares(1), {})
        text, keyboard = self.tg.current[(MIHIR, self.item_mid)]
        self.assertIn("<b>₹451</b> · Blinkit", text)
        self.assertIn("Only part is shared", buttons(keyboard))
        notice = self.tg.current[(RAHUL, notice_id)][0]
        self.assertIn("<s>Mihir added <b>₹300</b> · Blinkit</s>", notice)
        self.assertIn("Removed by Mihir", notice)
        self.assertEqual(self.balance_text(MIHIR), "All settled up.")

    def test_edit_part_by_reply(self):
        self.reply(self.ask(), "300")
        notice_id = self.tg.last_to(RAHUL)[1]
        self.message(MIHIR, "350", reply_to=self.item_mid)
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["total"]), (35000, 45100))
        self.assertEqual(self.db.shares(1), {MIHIR: 17500, RAHUL: 17500})
        self.assertIn("Your share: ₹175", self.tg.current[(RAHUL, notice_id)][0])

        self.message(MIHIR, "452", reply_to=self.item_mid)
        self.assertEqual(self.tg.last_to(MIHIR)[2], "That can't be more than what was paid (₹451).")
        self.message(MIHIR, "500 snacks", reply_to=self.item_mid)
        self.assertEqual(self.db.item(1)["amount"], 35000)
        self.assertEqual(self.db.item(1)["description"], "Blinkit")

        self.message(MIHIR, "snacks", reply_to=self.item_mid)
        self.assertEqual(self.db.item(1)["description"], "snacks")
        self.assertIn("<b>₹350</b> of ₹451 · snacks", self.tg.current[(MIHIR, self.item_mid)][0])

    def test_export_paid_total(self):
        self.reply(self.ask(), "300")
        self.message(MIHIR, "100 chai")
        self.press(MIHIR, "Split equally")
        self.message(MIHIR, "/export")
        rows = list(csv.reader(io.StringIO(self.tg.docs[-1][2].decode())))
        self.assertEqual(rows[0][2:4], ["amount", "paid_total"])
        self.assertEqual(rows[1][1:], ["Mihir", "300.00", "451.00", "Blinkit", "split", "150.00", "150.00"])
        self.assertEqual(rows[2][1:], ["Mihir", "100.00", "100.00", "chai", "split", "50.00", "50.00"])

    def test_three_members(self):
        self.join(PRIYA, "Priya")
        self.reply(self.ask(), "300")
        self.assertEqual(self.db.shares(1), {MIHIR: 10000, RAHUL: 10000, PRIYA: 10000})
        self.assertIn("Rahul, Priya owe you ₹100 each", self.tg.current[(MIHIR, self.item_mid)][0])
        for uid in (RAHUL, PRIYA):
            notice = self.tg.last_to(uid)[2]
            self.assertIn("Part of a ₹451 payment", notice)
            self.assertIn("Your share: ₹100", notice)


WEIRD_SMS = "Acct 1234: INR 451 to ZEPTO, ref 9988776655"  # a format fake_ai_sms does not know


class InboxTest(TwoMembers):
    def ai_bot(self, extractor):
        return self.make_bot(extractor=extractor, money_candidate=lambda text: True)

    def test_ingest_statuses(self):
        self.assertEqual(self.bot.ingest_sms("nope", sms()), "unknown")
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), "Lunch at 1?"), "ignored")
        self.assertEqual(self.db._all("SELECT * FROM inbox"), [])
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), sms()), "queued")
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), sms().replace("\n", "  \n")), "duplicate")
        self.assertIsNone(self.db.item(1))  # nothing happens until the worker runs
        self.assertEqual(self.tg.sent, [])
        self.assertTrue(self.bot._wake.is_set())
        self.assertEqual(self.bot.process_inbox(), 1)
        self.assertEqual(self.db.item(1)["amount"], 45000)

    def test_ai_spend(self):
        extractor = mock.Mock(**{"sms.return_value": Txn(45100, "Zepto", "9988776655")})
        self.ai_bot(extractor)
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), WEIRD_SMS), "queued")
        self.assertEqual(self.bot.process_inbox(), 1)
        extractor.sms.assert_called_once_with(WEIRD_SMS)
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["merchant"], item["dedup"]),
                         (45100, "Zepto", f"{MIHIR}:ref:9988776655:45100"))
        self.assertIn("<b>₹451</b> · Zepto", self.prompt(MIHIR)[1])
        row = self.db.inbox_row(1)
        self.assertIsNone(row["text"])
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(self.bot.process_inbox(), 0)

    def test_ai_says_not_a_spend(self):
        self.ai_bot(mock.Mock(**{"sms.return_value": None}))
        self.bot.ingest_sms(self.token(MIHIR), sms())  # looks like a spend, but the AI says it is not
        self.assertEqual(self.bot.process_inbox(), 1)
        self.assertIsNone(self.db.item(1))
        self.assertIsNone(self.db.inbox_row(1)["text"])

    def test_ai_failure_is_retried_not_guessed(self):
        self.ai_bot(mock.Mock(**{"sms.side_effect": AIError("quota")}))
        self.bot.ingest_sms(self.token(MIHIR), sms())  # a plain, well-formed bank SMS
        with self.assertLogs("app.bot", "WARNING") as logs:
            self.assertEqual(self.bot.process_inbox(), 0)
        output = "\n".join(logs.output)
        self.assertNotIn("BLINKIT", output)
        self.assertIn("AIError", output)
        self.assertIsNone(self.db.item(1))
        row = self.db.inbox_row(1)
        self.assertEqual((row["attempts"], row["done_at"]), (1, None))
        self.assertEqual(self.tg.to(MIHIR), [])

    def test_no_extractor_tells_payer_at_once(self):
        self.make_bot()  # the server is not signed in to ChatGPT
        self.assertEqual(self.bot.ingest_sms(self.token(MIHIR), sms("451.00") + " <x>"), "queued")
        self.assertEqual(self.bot.process_inbox(), 1)
        text = self.tg.last_to(MIHIR)[2]
        self.assertIn("isn't signed in to ChatGPT", text)
        self.assertIn("<i>Sent Rs.451.00 From HDFC Bank", text)
        self.assertIn("&lt;x&gt;", text)
        self.assertIn("450 groceries", text)
        row = self.db.inbox_row(1)
        self.assertIsNone(row["text"])
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(self.db.item(1))
        self.message(MIHIR, "451 blinkit")  # typing still works without AI
        self.assertEqual(self.db.item(1)["amount"], 45100)

    def test_default_money_prefilter(self):
        self.make_bot(extractor=self.ai)
        token = self.token(MIHIR)
        self.assertEqual(self.bot.ingest_sms(token, "A/c X1 debited by 450.0 on 20Nov26 trf to ZEPTO"), "queued")
        self.assertEqual(self.bot.ingest_sms(token, "₹ 99 paid to Chai Point"), "queued")
        self.assertEqual(self.bot.ingest_sms(token, "123456 is your OTP for txn of INR 450.00"), "ignored")
        self.assertEqual(self.bot.ingest_sms(token, "See you in 5 hours, bring 2 chairs"), "ignored")

    def test_unreadable_is_retried_then_given_up(self):
        self.ai_bot(mock.Mock(**{"sms.side_effect": AIError("down")}))
        self.bot.ingest_sms(self.token(MIHIR), WEIRD_SMS + " <b>")
        with self.assertLogs("app.bot", "WARNING"):
            self.assertEqual(self.bot.process_inbox(), 0)
            self.assertEqual(self.db.inbox_row(1)["attempts"], 1)
            self.assertEqual(self.bot.process_inbox(), 0)  # not due yet
            self.assertEqual(self.db.inbox_row(1)["attempts"], 1)
            for minutes, attempts in ((1, 2), (5, 3), (15, 4), (30, 5)):
                self.advance(minutes=minutes)
                self.assertEqual(self.bot.process_inbox(), 0)
                self.assertEqual(self.db.inbox_row(1)["attempts"], attempts)
            self.assertEqual(self.tg.to(MIHIR), [])
            self.advance(minutes=59)
            self.assertEqual(self.bot.process_inbox(), 0)
            self.advance(minutes=1)
            self.assertEqual(self.bot.process_inbox(), 1)
        give_up = self.tg.last_to(MIHIR)[2]
        self.assertIn("couldn't read this bank SMS", give_up)
        self.assertIn("INR 451 to ZEPTO", give_up)
        self.assertIn("&lt;b&gt;", give_up)
        self.assertIn("450 groceries", give_up)
        self.assertIsNone(self.db.inbox_row(1)["text"])
        self.assertIsNone(self.db.item(1))

    def test_ai_recovers_on_retry(self):
        extractor = mock.Mock(**{"sms.side_effect": [AIError("down"), Txn(45100, "Zepto", None)]})
        self.ai_bot(extractor)
        self.bot.ingest_sms(self.token(MIHIR), WEIRD_SMS)
        with self.assertLogs("app.bot", "WARNING"):
            self.bot.process_inbox()
        self.advance(minutes=1)
        self.assertEqual(self.bot.process_inbox(), 1)
        self.assertEqual(self.db.item(1)["amount"], 45100)

    def test_finished_rows_are_purged_after_a_week(self):
        self.ingest()
        self.advance(days=8)
        self.bot.process_inbox()
        self.assertIsNone(self.db.inbox_row(1))
        self.assertEqual(self.ingest(), "queued")  # the hash is gone, the item dedup still holds
        self.assertIsNone(self.db.item(2))

    def test_run_inbox_survives_errors_and_stops(self):
        stop = threading.Event()
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bug")
            stop.set()
            return 0

        self.bot.process_inbox = boom
        self.bot._wake.set()
        with mock.patch.object(self.bot._wake, "wait", lambda timeout: True), \
                self.assertLogs("app.bot", "ERROR"):
            self.bot.run_inbox(stop)
        self.assertEqual(len(calls), 2)


class EmailCommandTest(TwoMembers):
    def setUp(self):
        super().setUp()
        self.mailbox = FakeMailbox()
        self.make_bot(extractor=mock.Mock(), mailbox=self.mailbox,
                      mail_senders=("orders@blinkit.example", "noreply@swiggy.example"))

    def email(self, args):
        self.message(MIHIR, f"/email {args}".strip())
        return self.tg.last_to(MIHIR)[2]

    def test_status_and_instructions(self):
        text = self.email("")
        self.assertIn("No mailbox connected", text)
        self.assertIn("https://myaccount.google.com/apppasswords", text)
        self.assertIn("/email you@gmail.com abcd efgh ijkl mnop", text)

    def test_connect_gmail(self):
        mid = next(self.msg_ids) + 1  # the id the next message() call will use
        self.msg_ids = itertools.count(mid)
        text = self.email("me@gmail.com abcd efgh ijkl mnop")
        self.assertEqual(self.tg.deleted, [(MIHIR, mid)])
        self.assertEqual(self.mailbox.logins, [("imap.gmail.com", 993, "me@gmail.com", "abcdefghijklmnop")])
        acc = self.db.mail_account(MIHIR)
        self.assertEqual((acc["address"], acc["password"], acc["host"], acc["port"], acc["last_uid"]),
                         ("me@gmail.com", "abcdefghijklmnop", "imap.gmail.com", 993, None))
        self.assertIn("Connected me@gmail.com", text)
        self.assertIn("orders@blinkit.example, noreply@swiggy.example", text)
        self.assertIn("never change", text)
        self.assertNotIn("abcd", text)
        self.assertIn("Connected: me@gmail.com", self.email(""))

    def test_connect_other_host_and_replace(self):
        self.db.set_mail_account(MIHIR, "old@gmail.com", "x", "imap.gmail.com", 993)
        self.db.mail_fetched(MIHIR, 7, 99)
        self.email("me@example.in pass word imap.example.in")
        self.assertEqual(self.mailbox.logins[-1], ("imap.example.in", 993, "me@example.in", "password"))
        acc = self.db.mail_account(MIHIR)
        self.assertEqual((acc["address"], acc["uidvalidity"], acc["last_uid"]), ("me@example.in", None, None))

    def test_login_failure(self):
        self.mailbox.login_error = Exception("Invalid credentials (Failure)")
        text = self.email("me@gmail.com wrong pass word")
        self.assertEqual(len(self.tg.deleted), 1)
        self.assertIn("Invalid credentials", text)
        self.assertIsNone(self.db.mail_account(MIHIR))

    def test_delete_failure_is_reported(self):
        self.tg.fail_delete = True
        with self.assertLogs("app.bot", "WARNING"):
            text = self.email("me@gmail.com abcd efgh ijkl mnop")
        self.assertIn("delete it yourself", text)
        self.assertIsNotNone(self.db.mail_account(MIHIR))

    def test_off(self):
        self.db.set_mail_account(MIHIR, "me@gmail.com", "x", "imap.gmail.com", 993)
        self.assertIn("disconnected", self.email("off"))
        self.assertIsNone(self.db.mail_account(MIHIR))
        self.assertEqual(self.tg.deleted, [])
        self.assertIn("No mailbox", self.email("off"))

    def test_email_when_ai_is_off(self):
        self.make_bot(mailbox=self.mailbox)  # no extractor
        self.assertIn("isn't signed in to ChatGPT", self.email(""))
        text = self.email("me@gmail.com abcd efgh ijkl mnop")
        self.assertIn("isn't signed in to ChatGPT", text)
        self.assertEqual(len(self.tg.deleted), 1)  # the password still leaves the chat
        self.assertEqual(self.mailbox.logins, [])
        self.assertIsNone(self.db.mail_account(MIHIR))


LINES = [("Milk 1L", 6000), ("Chips", 4000), ("Shampoo", 20000)]


class MailBase(TwoMembers):
    def setUp(self):
        super().setUp()
        self.mailbox = FakeMailbox()
        self.orders = {}
        self.extractor = mock.Mock()
        self.extractor.sms.side_effect = fake_ai_sms
        self.extractor.order_email.side_effect = self.read_order
        self.make_bot(extractor=self.extractor, mailbox=self.mailbox, mail_senders=("orders@blinkit.example",))
        self.db.set_mail_account(MIHIR, "me@gmail.com", "pw", "imap.gmail.com", 993)

    def read_order(self, sender, subject, body):
        result = self.orders[subject]
        if isinstance(result, Exception):
            raise result
        return result

    def deliver(self, subject, result):
        self.orders[subject] = result
        self.mailbox.batches.append([mail(subject)])
        self.bot.poll_mail()

    def orders_table(self):
        return self.db._all("SELECT * FROM orders ORDER BY id")


class MailTest(MailBase):
    def test_uid_state_is_persisted(self):
        self.mailbox.batches = [[mail("a", 1), mail("b", 2)], []]
        self.orders = {"a": None, "b": None}
        self.bot.poll_mail()
        self.bot.poll_mail()
        first, second = self.mailbox.fetch_calls
        self.assertEqual((first["uidvalidity"], first["last_uid"]), (None, None))
        self.assertEqual((second["uidvalidity"], second["last_uid"]), (7, 2))
        self.assertEqual(first["senders"], ("orders@blinkit.example",))
        self.assertEqual(self.orders_table(), [])

    def test_order_attaches_to_pending_item(self):
        self.ingest(sms("451.00"))
        mid = self.prompt(MIHIR)[0]
        self.assertNotIn("Pick shared items", self.prompt(MIHIR)[2])
        self.deliver("Your Blinkit order", order(45100, LINES))
        self.assertIsNotNone(self.db.item(1)["order_id"])
        text, keyboard = self.tg.current[(MIHIR, mid)]
        self.assertIn("🧾 Order email found: 3 items", text)
        labels = [[label for label, _ in row] for row in keyboard]
        self.assertEqual(labels[-3:], [["Pick shared items"], ["Only part is shared"], ["Personal"]])

    def test_order_before_payment(self):
        self.deliver("Your Blinkit order", order(45100, LINES))
        self.assertIsNone(self.orders_table()[0]["item_id"])
        self.advance(hours=1)
        self.ingest(sms("451.00"))
        self.assertEqual(self.orders_table()[0]["item_id"], 1)
        self.assertIn("Pick shared items", self.prompt(MIHIR)[2])
        self.assertIn("Order email found", self.tg.to(MIHIR)[0][2])  # in the very first prompt

    def test_typed_entry_picks_up_waiting_order(self):
        self.deliver("Your Blinkit order", order(45100, LINES))
        self.message(MIHIR, "451 blinkit")
        self.assertIsNotNone(self.db.item(1)["order_id"])

    def test_amount_and_time_must_match(self):
        self.ingest(sms("450.00"))
        self.deliver("Your Blinkit order", order(45100, LINES))
        self.assertIsNone(self.db.item(1)["order_id"])
        self.advance(hours=13)
        self.ingest(sms("451.00", ref="222222222222"))
        self.assertIsNone(self.db.item(2)["order_id"])  # the order is too old for this payment
        self.advance(days=2)
        self.bot.poll_mail()
        self.assertEqual(self.orders_table(), [])  # unattached orders are purged after two days

    def test_second_order_is_discarded(self):
        self.ingest(sms("451.00"))
        self.deliver("Order confirmed", order(45100, LINES))
        self.deliver("Order delivered", order(45100, LINES[:1]))
        self.assertEqual(len(self.orders_table()), 1)
        self.assertEqual(len(self.db.order_lines(self.db.item(1)["order_id"])), 3)

    def test_duplicate_waiting_orders_collapse_on_attach(self):
        self.deliver("Order confirmed", order(45100, LINES))
        self.deliver("Order delivered", order(45100, LINES))
        self.ingest(sms("451.00"))
        self.assertEqual(len(self.orders_table()), 1)

    def test_extractor_raise_skips_only_that_mail(self):
        self.ingest(sms("451.00"))
        self.orders = {"bad subject": AIError("timeout"), "good": order(45100, LINES)}
        self.mailbox.batches = [[mail("bad subject", 1), mail("good", 2)]]
        with self.assertLogs("app.bot", "WARNING") as logs:
            self.bot.poll_mail()
        output = "\n".join(logs.output)
        self.assertNotIn("bad subject", output)
        self.assertNotIn("me@gmail.com", output)
        self.assertIsNotNone(self.db.item(1)["order_id"])

    def test_repeated_fetch_failures_notify_once(self):
        self.mailbox.fetch_error = OSError("connection refused")
        with self.assertLogs("app.bot", "WARNING"):
            for _ in range(4):
                self.bot.poll_mail()
            self.assertEqual(self.tg.to(MIHIR), [])
            for _ in range(3):
                self.bot.poll_mail()
        self.assertEqual(len(self.tg.to(MIHIR)), 1)
        self.assertIn("reconnect", self.tg.last_to(MIHIR)[2])
        self.mailbox.fetch_error = None
        self.bot.poll_mail()
        self.assertEqual(self.db.mail_account(MIHIR)["failures"], 0)

    def test_poll_mail_off_without_mailbox(self):
        self.make_bot(extractor=self.extractor)
        self.bot.poll_mail()
        self.assertEqual(self.mailbox.fetch_calls, [])


class PickTest(MailBase):
    def setUp(self):
        super().setUp()
        self.ingest(sms("451.00"))
        self.item_mid = self.prompt(MIHIR)[0]
        self.deliver("Your Blinkit order", order(45100, LINES))

    def view(self):
        text, keyboard = self.tg.current[(MIHIR, self.item_mid)]
        return text, buttons(keyboard)

    def pick(self, *labels):
        for label in labels:
            self.press(MIHIR, label, self.item_mid)

    def test_tick_list(self):
        self.assertEqual(self.press(MIHIR, "Pick shared items", self.item_mid), "Tick what was for the flat")
        text, btns = self.view()
        self.assertIn("Tick what was for the flat:", text)
        self.assertEqual(list(btns), ["☐ Milk 1L · ₹60", "☐ Chips · ₹40", "☐ Shampoo · ₹200",
                                      "Split ticked", "Back"])
        self.assertTrue(all(len(d.encode()) < 64 for d in btns.values()))
        self.pick("☐ Milk 1L · ₹60")
        self.assertIn("☑ Milk 1L · ₹60", self.view()[1])
        self.assertIn("Split ticked · ₹90.20", self.view()[1])  # 451 × 60/300
        self.pick("☐ Chips · ₹40", "☑ Milk 1L · ₹60", "☐ Milk 1L · ₹60")
        self.assertIn("Split ticked · ₹150.33", self.view()[1])  # 451 × 100/300 = 150.333…
        self.bot._refresh(1)  # e.g. a refresh from the mail thread keeps the tick list
        self.assertIn("Tick what was for the flat:", self.view()[0])
        self.pick("Back")
        self.assertIn("Shared?", self.view()[0])
        self.pick("Pick shared items")
        self.assertIn("☑ Chips · ₹40", self.view()[1])

    def test_nothing_ticked(self):
        self.pick("Pick shared items")
        self.assertEqual(self.press(MIHIR, "Split ticked", self.item_mid), "Nothing ticked yet")
        self.assertEqual(self.db.item(1)["status"], "pending")

    def test_split_ticked(self):
        self.pick("Pick shared items", "☐ Milk 1L · ₹60", "☐ Chips · ₹40")
        self.press(MIHIR, "Split ticked · ₹150.33", self.item_mid)
        item = self.db.item(1)
        self.assertEqual((item["status"], item["amount"], item["total"], item["picked"]),
                         ("shared", 15033, 45100, "Milk 1L, Chips"))
        self.assertEqual(self.db.shares(1), {MIHIR: 7517, RAHUL: 7516})
        text, btns = self.view()
        self.assertTrue(text.startswith("✅ <b>₹150.33</b> of ₹451 · Blinkit"))
        self.assertIn("🧾 Milk 1L, Chips", text)
        self.assertEqual(set(btns), {"Undo"})
        notice = self.tg.last_to(RAHUL)[2]
        self.assertIn("Part of a ₹451 payment\n🧾 Milk 1L, Chips\nYour share: ₹75.16", notice)
        self.assertIn("You owe Mihir ₹75.16 in total", notice)

    def test_all_ticked_is_a_full_split(self):
        self.pick("Pick shared items", "☐ Milk 1L · ₹60", "☐ Chips · ₹40", "☐ Shampoo · ₹200")
        self.press(MIHIR, "Split ticked · ₹451", self.item_mid)
        item = self.db.item(1)
        self.assertEqual((item["amount"], item["total"], item["picked"]), (45100, None, None))
        self.assertEqual(self.db.shares(1), {MIHIR: 22550, RAHUL: 22550})
        self.assertNotIn(" of ", self.view()[0])

    def test_undo_keeps_ticks(self):
        self.pick("Pick shared items", "☐ Chips · ₹40")
        self.press(MIHIR, "Split ticked · ₹60.13", self.item_mid)
        notice_id = self.tg.last_to(RAHUL)[1]
        self.pick("Undo")
        item = self.db.item(1)
        self.assertEqual((item["status"], item["amount"], item["total"], item["picked"]),
                         ("pending", 45100, None, None))
        self.assertIn("Shared?", self.view()[0])
        self.assertIn("<s>", self.tg.current[(RAHUL, notice_id)][0])
        self.pick("Pick shared items")
        self.assertIn("☑ Chips · ₹40", self.view()[1])

    def test_ticks_after_handled_are_ignored(self):
        self.pick("Pick shared items")
        tick = self.view()[1]["☐ Milk 1L · ₹60"]
        self.pick("Back", "Personal")
        self.assertEqual(self.tap(MIHIR, tick, self.item_mid), "Already handled")

    def test_no_pick_button_when_alone_or_too_many_lines(self):
        self.ingest(sms("999.00", ref="333333333333"))
        mid = self.prompt(MIHIR)[0]
        self.deliver("Big order", order(99900, [(f"thing {i}", 100) for i in range(41)]))
        self.assertIsNotNone(self.db.item(2)["order_id"])
        self.assertNotIn("Pick shared items", buttons(self.tg.current[(MIHIR, mid)][1]))


PRE_PART_ITEMS = """
CREATE TABLE items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payer        INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    description  TEXT NOT NULL,
    merchant     TEXT,
    source       TEXT NOT NULL,
    raw          TEXT,
    dedup        TEXT UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',
    kind         TEXT,
    counterparty INTEGER,
    by_rule      INTEGER NOT NULL DEFAULT 0,
    message_id   INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
INSERT INTO items(payer, amount, description, source, created_at, updated_at)
VALUES (101, 45100, 'Blinkit', 'manual', '2026-11-19T10:00:00Z', '2026-11-19T10:00:00Z');
"""


class MigrationTest(unittest.TestCase):
    def test_adds_missing_columns_to_existing_items_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot.db")
            old = sqlite3.connect(path)
            old.executescript(PRE_PART_ITEMS)
            old.close()

            db = DB(path)
            self.addCleanup(db.close)
            columns = {r["name"] for r in db._all("PRAGMA table_info(items)")}
            self.assertTrue({"total", "ask_message_id"} <= columns)
            self.assertIsNone(db.item(1)["total"])
            DB(path).close()  # opening again is a no-op

            tg = FakeTelegram()
            bot = Bot(db, tg)
            for uid, name in ((MIHIR, "Mihir"), (RAHUL, "Rahul")):
                bot.handle_update({"update_id": uid, "message": {
                    "message_id": uid, "chat": {"id": uid, "type": "private"},
                    "from": {"id": uid, "first_name": name}, "text": f"/start {bot.join_code}"}})
            bot._send_item(1)
            item_mid = db.item(1)["message_id"]
            bot.handle_update({"update_id": 10, "callback_query": {
                "id": "c1", "from": {"id": MIHIR}, "data": "o:1",
                "message": {"message_id": item_mid, "chat": {"id": MIHIR, "type": "private"}}}})
            ask_mid = db.item(1)["ask_message_id"]
            bot.handle_update({"update_id": 11, "message": {
                "message_id": 11, "chat": {"id": MIHIR, "type": "private"}, "from": {"id": MIHIR},
                "text": "300", "reply_to_message": {"message_id": ask_mid}}})
            item = db.item(1)
            self.assertEqual((item["status"], item["amount"], item["total"]), ("shared", 30000, 45100))
            self.assertEqual(db.shares(1), {MIHIR: 15000, RAHUL: 15000})


if __name__ == "__main__":
    unittest.main()
