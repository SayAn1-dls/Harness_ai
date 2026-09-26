from billing.money import format_price

SUPPORTED = ("USD", "GBP")


class Invoice:
    def __init__(self, customer, currency="USD"):
        if currency not in SUPPORTED:
            raise ValueError(f"unsupported currency {currency}")
        self.customer = customer
        self.currency = currency
        self.lines = []

    def add(self, description, amount):
        self.lines.append((description, amount))

    def total(self):
        return sum(amount for _, amount in self.lines)

    def total_line(self):
        return f"Total for {self.customer}: {format_price(self.total(), self.currency)}"
