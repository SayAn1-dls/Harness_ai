import unittest

from billing.invoice import Invoice
from billing.money import format_price


class BillingTests(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(format_price(1234.5, "USD"), "$1,234.50")

    def test_invoice_total(self):
        inv = Invoice("acme", "GBP")
        inv.add("widget", 10)
        inv.add("gadget", 2.5)
        self.assertEqual(inv.total_line(), "Total for acme: £12.50")

    def test_unknown_currency(self):
        with self.assertRaises(ValueError):
            format_price(1, "XYZ")


if __name__ == "__main__":
    unittest.main()
