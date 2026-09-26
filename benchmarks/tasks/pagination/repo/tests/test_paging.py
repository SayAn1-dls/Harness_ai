import unittest

from paging import page_count


class PageCountTests(unittest.TestCase):
    def test_page_count(self):
        self.assertEqual(page_count(10, 3), 4)
        self.assertEqual(page_count(0, 3), 0)


if __name__ == "__main__":
    unittest.main()
