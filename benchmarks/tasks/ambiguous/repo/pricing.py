TAX_RATE = 0.2


def price_with_tax(net):
    return round(net * (1 + TAX_RATE), 2)


def bulk_discount(qty, unit_price):
    if qty >= 10:
        return round(qty * unit_price * 0.9, 2)
    return qty * unit_price
