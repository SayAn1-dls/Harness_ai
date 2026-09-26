import unittest

from app.store import UserStore
from app.users import DuplicateUser, register_user


class RegisterTests(unittest.TestCase):
    def test_register_and_duplicate(self):
        s = UserStore()
        register_user("bob@example.com", "Bob", store=s)
        with self.assertRaises(DuplicateUser):
            register_user("bob@example.com", "Bob again", store=s)
        self.assertEqual(s.count(), 1)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            register_user("nope", "x", store=UserStore())


if __name__ == "__main__":
    unittest.main()
