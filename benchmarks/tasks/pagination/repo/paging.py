import math


def page_count(total, per_page):
    return math.ceil(total / per_page) if total else 0


def paginate(items, page, per_page):
    start = page * per_page
    return list(items[start:start + per_page])
