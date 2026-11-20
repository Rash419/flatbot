import unittest

from app.ledger import (money, nets, pairwise, plain_amount, proportional_part, settle_up, shares_for,
                        split_equal)


class LedgerTest(unittest.TestCase):
    def test_split_equal_gives_odd_paise_to_payer(self):
        self.assertEqual(split_equal(45001, [1, 2], 1), {1: 22501, 2: 22500})
        self.assertEqual(split_equal(45001, [1, 2], 2), {1: 22500, 2: 22501})
        self.assertEqual(split_equal(100, [1, 2, 3], 2), {1: 33, 2: 34, 3: 33})

    def test_full_and_settle_put_everything_on_counterparty(self):
        self.assertEqual(shares_for("full", 500, 1, [1, 2], 2), {2: 500})
        self.assertEqual(shares_for("settle", 500, 1, [], 2), {2: 500})
        with self.assertRaises(ValueError):
            shares_for("full", 500, 1, [], 1)

    def test_proportional_part(self):
        self.assertEqual(proportional_part(45100, 6000, 30000), 9020)
        self.assertEqual(proportional_part(45100, 10000, 30000), 15033)  # 15033.33 rounds down
        self.assertEqual(proportional_part(45100, 20000, 30000), 30067)  # 30066.67 rounds up
        self.assertEqual(proportional_part(3, 1, 2), 2)                  # 1.5 rounds half up
        self.assertEqual(proportional_part(1, 1, 2), 1)                  # 0.5 rounds half up
        self.assertEqual(proportional_part(45100, 30000, 30000), 45100)  # everything is the full amount
        self.assertEqual(proportional_part(45100, 0, 30000), 0)
        with self.assertRaises(ValueError):
            proportional_part(100, 1, 0)
        with self.assertRaises(ValueError):
            proportional_part(100, 3, 2)

    def test_nets_and_pairwise(self):
        debts = [(2, 1, 300), (1, 2, 100), (3, 1, 50)]
        self.assertEqual(nets(debts), {1: 250, 2: -200, 3: -50})
        self.assertEqual(pairwise(debts, 1, 2), 200)
        self.assertEqual(pairwise(debts, 2, 1), -200)
        self.assertEqual(pairwise(debts, 2, 3), 0)

    def test_settle_up(self):
        self.assertEqual(settle_up({1: 100, 2: -60, 3: -40}), [(2, 1, 60), (3, 1, 40)])
        self.assertEqual(settle_up({1: 70, 2: 30, 3: -100}), [(3, 1, 70), (3, 2, 30)])
        self.assertEqual(settle_up({1: 0, 2: 0}), [])

    def test_money(self):
        self.assertEqual(money(123450), "₹1,234.50")
        self.assertEqual(money(45000), "₹450")
        self.assertEqual(money(10000000), "₹1,00,000")
        self.assertEqual(money(5), "₹0.05")
        self.assertEqual(money(-22500), "-₹225")
        self.assertEqual(money(123450, "USD"), "1,234.50 USD")
        self.assertEqual(money(100000000, "usd"), "1,000,000 USD")
        self.assertEqual(plain_amount(123405), "1234.05")


if __name__ == "__main__":
    unittest.main()
