import unittest

from pricing import bulk_discount, price_with_tax


class PricingTests(unittest.TestCase):
    def test_tax(self):
        self.assertEqual(price_with_tax(10), 12.0)

    def test_bulk(self):
        self.assertEqual(bulk_discount(10, 1), 9.0)


if __name__ == "__main__":
    unittest.main()
