"""Test written for a tax helper that was removed from orders_etl.cleaning in v0.3."""
from orders_etl.cleaning import legacy_compute_tax


def test_legacy_compute_tax_adds_eight_percent():
    assert legacy_compute_tax(100.0) == 108.0
