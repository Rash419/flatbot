import unittest

from app.ai import AIError
from app.extract import (BILL_SCHEMA, MAX_ITEMS, ORDER_SCHEMA, SMS_SCHEMA, Extractor, Order,
                         money_candidate, to_paise)
from app.sms import Txn


class FakeAI:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def ask(self, prompt, schema, image=None):
        self.calls.append((prompt, schema, image))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


SPENDS = [
    "Sent Rs.450.00\nFrom HDFC Bank A/C *1234\nTo BLINKIT\nOn 20/11/26\nRef 123456789012",
    "Spent Rs.1234.50 On HDFC Bank Card 1234 At SWIGGY On 2026-11-20:20:15:33.",
    "ICICI Bank Acct XX123 debited for Rs 450.00 on 20-Nov-26; BLINKIT credited. UPI:123456789012.",
    "INR 1,234.00 spent using ICICI Bank Card XX1234 on 20-Nov-26 on SWIGGY.",
    "Dear UPI user A/C X1234 debited by 450.0 on date 20Nov26 trf to ZEPTO MARKETPLACE Refno 123456789012.",
    "Spent\nCard no. XX1234\nINR 450\n20-11-26 20:15:33\nSWIGGY\nAvl Lmt INR 100000",
    "Rs.1,234.00 spent on your SBI Credit Card ending 1234 at AMAZON PAY INDIA on 20/11/26.",
    "₹450 paid to Blinkit via UPI",
    "Paid 1,200/- to the milkman",
]
# Money-looking but not spends: still candidates (the AI decides).
OTHER_MONEY = [
    "Money Received - Rs.500.00 in HDFC Bank A/c xx1234 on 20-11-26 from VPA rahul@upi",
    "Rs.999 will be debited from your HDFC Bank A/c 1234 on 25/11/26 for NETFLIX autopay.",
    "Refund of Rs.450.00 from SWIGGY has been credited to your card ending 1234.",
]
NOT_CANDIDATES = [
    "123456 is your OTP for txn of INR 450.00 at SWIGGY on HDFC Bank card ending 1234. Do not share.",
    "Your verification code is 482913. Valid for 10 minutes.",
    "Use one-time password 4821 to pay Rs.200 at Zomato",
    "Hey, are we still on for dinner at 8?",
    "Your Blinkit order is out for delivery.",
    "Your order 4058123 has shipped",
    "Meet me at gate 3 at 10:30",
    "",
]


class MoneyCandidateTest(unittest.TestCase):
    def test_spends_and_money_messages(self):
        for text in SPENDS + OTHER_MONEY:
            with self.subTest(text=text):
                self.assertTrue(money_candidate(text))

    def test_otps_and_chat(self):
        for text in NOT_CANDIDATES:
            with self.subTest(text=text):
                self.assertFalse(money_candidate(text))


class PaiseTest(unittest.TestCase):
    def test_no_float_drift(self):
        self.assertEqual(to_paise(1234.5), 123450)
        self.assertEqual(to_paise(0.29), 29)
        self.assertEqual(to_paise(1.005), 101)
        self.assertEqual(to_paise(19.99), 1999)
        self.assertEqual(to_paise("1,23,456.70"), 12345670)
        self.assertEqual(to_paise(450), 45000)

    def test_rejects_non_numbers(self):
        for bad in (None, True, "abc", float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(AIError):
                to_paise(bad)


def _strict(test, schema, path="schema"):
    """OpenAI strict mode: every object lists all its properties as required, no extras."""
    if schema.get("type") == "object":
        test.assertIs(schema.get("additionalProperties"), False, path)
        test.assertEqual(sorted(schema["required"]), sorted(schema["properties"]), path)
        for name, sub in schema["properties"].items():
            _strict(test, sub, f"{path}.{name}")
    if schema.get("type") == "array":
        _strict(test, schema["items"], f"{path}[]")


class SchemaTest(unittest.TestCase):
    def test_schemas_are_strict(self):
        for schema in (SMS_SCHEMA, ORDER_SCHEMA, BILL_SCHEMA):
            _strict(self, schema)


class SmsTest(unittest.TestCase):
    TEXT = SPENDS[0]

    def test_spend(self):
        ai = FakeAI({"is_spend": True, "amount": 450, "merchant": "Blinkit", "ref": "123456789012"})
        self.assertEqual(Extractor(ai).sms(self.TEXT), Txn(45000, "Blinkit", "123456789012"))
        prompt, schema, image = ai.calls[0]
        self.assertIn(self.TEXT, prompt)
        self.assertIn("untrusted data", prompt)
        self.assertIs(schema, SMS_SCHEMA)
        self.assertIsNone(image)

    def test_amount_with_commas_and_decimals(self):
        ai = FakeAI({"is_spend": True, "amount": 1234.0, "merchant": "Swiggy", "ref": ""})
        self.assertEqual(Extractor(ai).sms(SPENDS[3]).amount, 123400)
        ai = FakeAI({"is_spend": True, "amount": 1234.5, "merchant": "Swiggy", "ref": ""})
        self.assertEqual(Extractor(ai).sms(SPENDS[1]).amount, 123450)

    def test_not_a_spend(self):
        ai = FakeAI({"is_spend": False, "amount": 0, "merchant": "", "ref": ""})
        self.assertIsNone(Extractor(ai).sms(OTHER_MONEY[0]))

    def test_empty_merchant_and_ref(self):
        ai = FakeAI({"is_spend": True, "amount": 450, "merchant": "  ", "ref": ""})
        self.assertEqual(Extractor(ai).sms(self.TEXT), Txn(45000, None, None))

    def test_long_merchant_is_trimmed(self):
        ai = FakeAI({"is_spend": True, "amount": 450, "merchant": "X" * 100, "ref": ""})
        self.assertEqual(len(Extractor(ai).sms(self.TEXT).merchant), 40)

    def test_amount_not_in_text_is_an_error(self):
        ai = FakeAI({"is_spend": True, "amount": 4500, "merchant": "Blinkit", "ref": ""})
        with self.assertRaises(AIError):
            Extractor(ai).sms(self.TEXT)

    def test_zero_or_bad_amount_is_an_error(self):
        for amount in (0, -450, "lots"):
            ai = FakeAI({"is_spend": True, "amount": amount, "merchant": "", "ref": ""})
            with self.subTest(amount=amount), self.assertRaises(AIError):
                Extractor(ai).sms(self.TEXT)

    def test_invented_ref_is_dropped(self):
        ai = FakeAI({"is_spend": True, "amount": 450, "merchant": "Blinkit", "ref": "999988887777"})
        self.assertIsNone(Extractor(ai).sms(self.TEXT).ref)

    def test_missing_keys_is_an_error(self):
        with self.assertRaises(AIError):
            Extractor(FakeAI({"is_spend": True})).sms(self.TEXT)

    def test_ai_error_propagates(self):
        with self.assertRaises(AIError):
            Extractor(FakeAI(AIError("down"))).sms(self.TEXT)


EMAIL = """Your Blinkit order #1234 has been delivered
Amul Taaza Milk 1 L x 2   Rs 136
Bread                    Rs 45
Harpic Toilet Cleaner    Rs 199
Delivery fee             Rs 25
Handling charge          Rs 4
Item total Rs 380
Grand total Rs 409.00
"""


class OrderTest(unittest.TestCase):
    def answer(self, **over):
        data = {"is_order": True, "merchant": "Blinkit", "total": 409,
                "items": [{"name": "Amul Taaza Milk 1 L x 2", "amount": 136},
                          {"name": "Bread", "amount": 45},
                          {"name": "Harpic Toilet Cleaner", "amount": 199}]}
        data.update(over)
        return data

    def test_order(self):
        ai = FakeAI(self.answer())
        order = Extractor(ai).order_email("Blinkit <no-reply@blinkit.com>", "Order delivered", EMAIL)
        self.assertEqual(order, Order("Blinkit", 40900, (
            ("Amul Taaza Milk 1 L x 2", 13600), ("Bread", 4500), ("Harpic Toilet Cleaner", 19900))))
        prompt, schema, _ = ai.calls[0]
        self.assertIs(schema, ORDER_SCHEMA)
        self.assertIn("Harpic", prompt)
        self.assertIn("no-reply@blinkit.com", prompt)

    def test_not_an_order(self):
        ai = FakeAI({"is_order": False, "merchant": "", "total": 0, "items": []})
        self.assertIsNone(Extractor(ai).order_email("a@b.c", "Sale!", "50% off"))

    def test_total_not_in_body_is_an_error(self):
        with self.assertRaises(AIError):
            Extractor(FakeAI(self.answer(total=380.5))).order_email("x@blinkit.com", "s", EMAIL)

    def test_zero_total_is_an_error(self):
        with self.assertRaises(AIError):
            Extractor(FakeAI(self.answer(total=0))).order_email("x@blinkit.com", "s", EMAIL)

    def test_bad_items_dropped_and_no_items_is_none(self):
        items = [{"name": "Bread", "amount": 45}, {"name": "Discount", "amount": -20},
                 {"name": "", "amount": 10}, {"name": "Free gift", "amount": 0},
                 {"name": "Odd", "amount": "n/a"}]
        order = Extractor(FakeAI(self.answer(items=items))).order_email("x@blinkit.com", "s", EMAIL)
        self.assertEqual(order.lines, (("Bread", 4500),))
        none_left = [{"name": "Discount", "amount": -20}]
        self.assertIsNone(Extractor(FakeAI(self.answer(items=none_left))).order_email("x@blinkit.com", "s", EMAIL))
        self.assertIsNone(Extractor(FakeAI(self.answer(items=[]))).order_email("x@blinkit.com", "s", EMAIL))

    def test_caps(self):
        items = [{"name": "N" * 100, "amount": 1} for _ in range(100)]
        order = Extractor(FakeAI(self.answer(items=items))).order_email("x@blinkit.com", "s", EMAIL)
        self.assertEqual(len(order.lines), MAX_ITEMS)
        self.assertEqual(len(order.lines[0][0]), 60)

    def test_body_truncated(self):
        ai = FakeAI(self.answer())
        long_body = EMAIL + "x" * 50_000
        Extractor(ai).order_email("x@blinkit.com", "s", long_body)
        self.assertLess(len(ai.calls[0][0]), 17_000)

    def test_merchant_falls_back_to_sender(self):
        order = Extractor(FakeAI(self.answer(merchant=""))).order_email(
            "Swiggy <noreply@swiggy.in>", "s", EMAIL)
        self.assertEqual(order.merchant, "Swiggy")


class BillTest(unittest.TestCase):
    def test_bill(self):
        ai = FakeAI({"is_bill": True, "total": 1234.56, "merchant": "Cafe Coffee Day"})
        self.assertEqual(Extractor(ai).bill(b"jpeg"), (123456, "Cafe Coffee Day"))
        _, schema, image = ai.calls[0]
        self.assertIs(schema, BILL_SCHEMA)
        self.assertEqual(image, b"jpeg")

    def test_not_a_bill(self):
        self.assertIsNone(Extractor(FakeAI({"is_bill": False, "total": 0, "merchant": ""})).bill(b"x"))
        self.assertIsNone(Extractor(FakeAI({"is_bill": True, "total": 0, "merchant": "X"})).bill(b"x"))

    def test_errors(self):
        with self.assertRaises(AIError):
            Extractor(FakeAI({"is_bill": True})).bill(b"x")
        with self.assertRaises(AIError):
            Extractor(FakeAI(AIError("down"))).bill(b"x")


if __name__ == "__main__":
    unittest.main()
