import unittest

from files import read_upload


class UploadTests(unittest.TestCase):
    def test_read(self):
        self.assertEqual(read_upload("hello.txt"), "hello\n")


if __name__ == "__main__":
    unittest.main()
