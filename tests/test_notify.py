"""Formátování telegramových zpráv."""

from src.notify import (CESTOVANI, HRY, OSTATNI, PREDPLATNE, format_digest,
                        format_instant, group_of, split_message)
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


def _item(name, group, ratio=0.1, popularity=None, **extra):
    item = {"name": name, "url": f"http://x/{name}", "price_czk": 100.0,
            "value_ratio": ratio, "group": group, "popularity": popularity}
    item.update(extra)
    return item


class TestDigest:
    def test_empty_digest(self):
        assert "nic" in format_digest([]).lower()

    def test_quota_applies_per_group(self):
        items = [_item(f"Hra {i}", HRY, ratio=i / 100.0) for i in range(1, 40)]
        text = format_digest(items, per_group=8)

        assert "8 nejlepších z 39" in text
        assert "Hra 1" in text
        assert "Hra 39" not in text

    def test_flood_of_games_does_not_crowd_out_other_sections(self):
        """Přesně ta situace, kvůli které se souhrn dělí: super deal na
        předplatném nesmí zapadnout pod čtyřicítkou her za pár korun."""
        items = [_item(f"Hra {i}", HRY, ratio=0.01) for i in range(40)]
        items.append(_item("Gemini AI Pro 18 měsíců", PREDPLATNE, ratio=0.007))

        text = format_digest(items, per_group=8)

        assert "Gemini AI Pro 18 měsíců" in text
        assert sum(1 for line in text.split("\n")
                   if line.startswith("•") and "Hra " in line) == 8

    def test_games_are_ranked_by_popularity_not_by_discount(self):
        items = [
            _item("Stará šunta", HRY, ratio=0.01, popularity=0.2),
            _item("Elden Ring", HRY, ratio=0.30, popularity=0.95),
        ]
        text = format_digest(items)
        assert text.index("Elden Ring") < text.index("Stará šunta")

    def test_items_without_popularity_keep_ordering_by_discount(self):
        items = [_item("Dražší", OSTATNI, ratio=0.30),
                 _item("Levnější", OSTATNI, ratio=0.05)]
        text = format_digest(items)
        assert text.index("Levnější") < text.index("Dražší")

    def test_reviews_and_year_are_shown(self):
        items = [_item("Elden Ring", HRY, popularity=0.9,
                       reviews_score=92, reviews_count=742000,
                       released="2022-02-25")]
        text = format_digest(items)
        assert "★ 92 % z 742 tis." in text
        assert "2022" in text

    def test_long_digest_is_split_under_telegram_limit(self):
        items = [_item(f"Položka s dost dlouhým názvem číslo {i}", HRY)
                 for i in range(40)]
        parts = split_message(format_digest(items, per_group=40, max_items=40))
        assert all(len(part) <= 3800 for part in parts)
        assert len(parts) > 1


class TestGrouping:
    def _kinguin(self, product_type):
        return Offer(source="kinguin", kind=CATALOG, uid="k", name="X",
                     price_czk=1.0, url="http://x", category=product_type,
                     extra={"product_type": product_type})

    def test_games(self):
        for product_type in ("GAME", "DLC", "GAME_ACCOUNT", "ALTERGIFT"):
            assert group_of(self._kinguin(product_type)) == HRY

    def test_subscription_top_up_is_not_a_game(self):
        """`INGAME_TOPUP` je navzdory názvu škatulka na předplatné —
        Gemini za 65 Kč, YouTube Premium, Spotify."""
        assert group_of(self._kinguin("INGAME_TOPUP")) == PREDPLATNE

    def test_software(self):
        assert group_of(self._kinguin("SOFTWARE")) == PREDPLATNE

    def test_flight_group(self):
        offer = Offer(source="fly4free", kind=FEED, uid="f", name="Let",
                      price_czk=1.0, url="http://x", category="flight")
        assert group_of(offer) == CESTOVANI

    def test_rest_falls_into_ostatni(self):
        offer = Offer(source="mydealz", kind=FEED, uid="f", name="Bunda",
                      price_czk=1.0, url="http://x", category="Fashion & Accessoires")
        assert group_of(offer) == OSTATNI
