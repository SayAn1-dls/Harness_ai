CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£"}


def format_price(amount, currency):
    try:
        symbol = CURRENCY_SYMBOLS[currency]
    except KeyError:
        raise ValueError(f"unsupported currency {currency}") from None
    return f"{symbol}{amount:,.2f}"
