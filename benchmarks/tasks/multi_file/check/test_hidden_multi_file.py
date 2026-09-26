import pytest
from billing.invoice import Invoice
from billing.money import format_price

def test_eur_format():
    assert format_price(1234.5, "EUR") == "€1,234.50"

def test_eur_invoice():
    inv = Invoice(customer="acme", currency="EUR")
    inv.add("x", 3)
    assert inv.total_line() == "Total for acme: €3.00"

def test_unknown_still_rejected():
    with pytest.raises(ValueError):
        format_price(1, "XYZ")
    with pytest.raises(ValueError):
        Invoice("a", currency="XYZ")
