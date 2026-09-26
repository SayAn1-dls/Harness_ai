We need to bill in euros.

- `format_price(1234.5, "EUR")` should return `"€1,234.50"` (same layout as USD/GBP).
- Creating `Invoice(customer="acme", currency="EUR")` currently raises `ValueError: unsupported currency EUR`;
  it should work, and `total_line()` should use the euro formatting.
- Unknown currencies must still be rejected with `ValueError`.
