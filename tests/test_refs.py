"""Ceník a přepočet délky předplatného.

Právě ten přepočet dělá z Gemini za 65 Kč trhák: 18 měsíců po 490 Kč je 8 820 Kč,
takže poměr vychází na sedm desetin procenta.
"""

import pytest

from src.oracles.refs import ReferenceOracle, parse_months
from src.sources.base import CATALOG, Offer


@pytest.mark.parametrize("name,expected", [
    ("Google Gemini Top-Up > AI Pro > 18 Months", 18),
    ("Google AI Pro: Gemini Advanced - 18-Month Subscription", 18),
    ("ChatGPT Plus 1-Month Subscription ACCOUNT", 1),
    ("Perplexity PRO - 1 Year Subscription Key EU", 12),
    ("Spotify Premium 3 měsíce", 3),
    ("NordVPN 2 Years", 24),
    ("Windows 11 Pro Retail Key", None),
])
def test_parse_months(name, expected):
    assert parse_months(name) == expected


def _offer(name, price_czk=65.0):
    return Offer(source="kinguin", kind=CATALOG, uid="x", name=name,
                 price_czk=price_czk, url="http://x")


class TestReferenceOracle:
    def setup_method(self):
        self.oracle = ReferenceOracle([
            {"match": ["gemini", "ai pro"], "value_czk_per_month": 490},
            {"match": ["windows 11 pro"], "value_czk": 4500},
        ])

    def test_subscription_multiplies_by_months(self):
        value = self.oracle.value_of(_offer("Google Gemini Top-Up > AI Pro > 18 Months"))
        assert value is not None
        assert value.real_value_czk == 490 * 18
        assert value.origin == "references"

    def test_one_off_value(self):
        value = self.oracle.value_of(_offer("Windows 11 Pro Retail Key"))
        assert value.real_value_czk == 4500

    def test_unknown_product_is_not_priced(self):
        assert self.oracle.value_of(_offer("Nějaká úplně neznámá hra")) is None

    def test_missing_duration_is_conservative(self):
        """Bez délky bereme jeden měsíc — radši podstřelit než vyrobit falešný trhák."""
        value = self.oracle.value_of(_offer("Gemini AI Pro Subscription"))
        assert value.real_value_czk == 490
        assert value.confidence < 1.0
