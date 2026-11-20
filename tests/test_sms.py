import unittest

from app.sms import merchant_key, parse_manual


class MerchantKeyTest(unittest.TestCase):
    def test_merchant_key(self):
        self.assertEqual(merchant_key("Amazon Pay India"), "amazonpayindia")
        self.assertEqual(merchant_key("BLINKIT"), merchant_key("Blinkit"))


class ParseManualTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_manual("450 groceries"), (45000, "groceries"))
        self.assertEqual(parse_manual("groceries 450"), (45000, "groceries"))
        self.assertEqual(parse_manual("₹1,250.50 gas cylinder"), (125050, "gas cylinder"))
        self.assertEqual(parse_manual("maid salary rs 3000"), (300000, "maid salary"))
        self.assertEqual(parse_manual("Rs.99 milk"), (9900, "milk"))

    def test_rejects(self):
        for text in ["hello", "450", "groceries", "0 nothing", ""]:
            with self.subTest(text=text):
                self.assertIsNone(parse_manual(text))


if __name__ == "__main__":
    unittest.main()
