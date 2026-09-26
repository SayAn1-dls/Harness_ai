def normalize_email(raw):
    """Canonical form used as the account key."""
    return raw.strip()


def slug(text):
    return "-".join(text.lower().split())
