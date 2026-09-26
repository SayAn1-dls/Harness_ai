from utils.text import normalize_email


def validate_email(raw):
    email = normalize_email(raw)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError(f"invalid email: {raw!r}")
    return email
