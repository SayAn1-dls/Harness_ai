import unittest

from textutil import title_case, word_count


class TextTests(unittest.TestCase):
    def test_word_count_simple(self):
        self.assertEqual(word_count("a b  c"), 3)

    def test_title_case_keeps_hyphenated_words(self):
        self.assertEqual(title_case("state-of-the-art design"), "State-of-the-art Design")


if __name__ == "__main__":
    unittest.main()
