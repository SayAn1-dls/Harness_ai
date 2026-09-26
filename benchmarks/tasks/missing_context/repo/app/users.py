from app.store import UserStore
from app.validation import validate_email

store = UserStore()


class DuplicateUser(Exception):
    pass


def register_user(email, name, store=store):
    key = validate_email(email)
    if store.exists(key):
        raise DuplicateUser(email)
    store.add(key, {"email": key, "name": name})
    return key
