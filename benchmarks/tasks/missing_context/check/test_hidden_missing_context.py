import pytest
from app.store import UserStore
from app.users import DuplicateUser, register_user

def test_case_insensitive_duplicate():
    s = UserStore()
    register_user("alice@example.com", "A", store=s)
    with pytest.raises(DuplicateUser):
        register_user("Alice@Example.COM", "A2", store=s)
    with pytest.raises(DuplicateUser):
        register_user("  ALICE@example.com ", "A3", store=s)
    assert s.count() == 1

def test_distinct_users_still_ok():
    s = UserStore()
    register_user("a@x.io", "A", store=s)
    register_user("b@x.io", "B", store=s)
    assert s.count() == 2
