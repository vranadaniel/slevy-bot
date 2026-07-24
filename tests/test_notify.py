"""Formátování telegramových zpráv."""

from src.notify import format_digest, format_instant, group_of
from src.oracles.base import Value
from src.score import INSTANT, Verdict
from src.sources.base import CATALOG, FEED, Offer


def _verdict():
    offer = Offer(
        source="kinguin", kind=CATALOG, uid="g1",
        name="Google Gemini Top-Up > AI Pro > 18 Months",
        price_czk=65.0, url="https://www.kinguin.net/x",
        category="INGAME_TOPUP", merchant="kinguin", extra={"stock": 12},
    )
    return Verdict(
        offer=offer, level=INSTANT,
        value=Value(real_value_czk=8820.0, origin="references",
                    note="ceník: gemini + ai pro × 18 měsíců"),
        value_ratio=65.0 / 8820.0,
        reasons=["ceník: gemini + ai pro × 18 měsíců", "4 Kč za měsíc"],
    )


NBSP = " "  # ceny se sázejí s pevnou mezerou, ať se nezalomí


class TestInstant:
    def test_contains_price_value_and_link(self):
        text = format_instant(_verdict())
        assert f"65{NBSP}Kč" in text
        assert f"8{NBSP}820{NBSP}Kč" in text
        assert "https://www.kinguin.net/x" in text
        assert "12 ks skladem" in text

    def test_extreme_ratio_keeps_one_decimal(self):
        """0,7 % zaokrouhlené na '1 %' by zahodilo to podstatné."""
        assert "0,7 %" in format_instant(_verdict())

    def test_escapes_html_in_name(self):
        verdict = _verdict()
        verdict.offer.name = "Deal <script>alert(1)</script> & spol"
        text = format_instant(verdict)
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


class TestDigest:
    def test_empty_digest(self):
        assert "nic" in format_digest([]).lower()

    def test_groups_and_caps(self):
        items = [
            {"name": f"Věc {i}", "url": f"http://x/{i}", "price_czk": 100.0,
             "value_ratio": i / 100.0, "group": "🎮 Klíče a předplatné"}
            for i in range(1, 40)
        ]
        text = format_digest(items, max_items=25)
        assert "25 nejlepších z 39" in text
        assert "Věc 1" in text
        assert "Věc 39" not in text, "má se ořezat na nejlepších 25"


class TestGrouping:
    def test_flight_group(self):
        offer = Offer(source="fly4free", kind=FEED, uid="f", name="Let",
                      price_czk=1.0, url="http://x", category="flight")
        assert "Cestování" in group_of(offer)

    def test_fashion_group(self):
        offer = Offer(source="mydealz", kind=FEED, uid="f", name="Bunda",
                      price_czk=1.0, url="http://x", category="Fashion & Accessoires")
        assert "Móda" in group_of(offer)
