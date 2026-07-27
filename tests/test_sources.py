"""Parsery feedů.

XML se skládá v testu, ne ze statického souboru — u cestování totiž záleží na
stáří položky a zamrzlé datum by test časem rozbilo.
"""

import datetime as dt
from email.utils import format_datetime

import pytest

from src.config import Config
from src.sources.pepper import PepperSource
from src.sources.travel import TravelSource


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeHttp:
    """Vrací připravené XML podle pořadí volání."""

    def __init__(self, payloads: list[str]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return FakeResponse(self.payloads[index].encode("utf-8"))


class FakeFx:
    RATES = {"EUR": 25.0, "GBP": 29.0, "PLN": 5.8, "CZK": 1.0}

    def to_czk(self, amount: float, currency: str) -> float:
        return amount * self.RATES[currency.upper()]


PEPPER_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:pepper="http://www.pepper.com/rss">
  <channel>{items}</channel>
</rss>"""

PEPPER_ITEM = """
  <item>
    <category>{category}</category>
    <pepper:merchant name="{merchant}" price="{price}"/>
    <title>{temperature}° - {title}</title>
    <description><![CDATA[{description}]]></description>
    <link>https://www.mydealz.de/deals/{guid}</link>
    <guid>https://www.mydealz.de/deals/{guid}</guid>
  </item>"""


def _pepper_feed(items):
    return PEPPER_TEMPLATE.format(items="".join(PEPPER_ITEM.format(**i) for i in items))


def _pepper_source(xml, min_temperature=150):
    site = {"name": "mydealz", "domain": "www.mydealz.de",
            "currency": "EUR", "feeds": ["hot"]}
    return PepperSource(FakeHttp([xml]), FakeFx(), site, min_temperature, delay_s=0)


class TestPepperParser:
    def test_extracts_temperature_price_and_merchant(self):
        xml = _pepper_feed([{
            "category": "Elektronik", "merchant": "HP", "price": "1.259,29€",
            "temperature": 116, "title": "HP OMEN Gaming Laptop",
            "description": "Über CB gibt's den Laptop", "guid": "hp-1",
        }])
        offers = _pepper_source(xml, min_temperature=100).fetch()

        assert len(offers) == 1
        offer = offers[0]
        assert offer.name == "HP OMEN Gaming Laptop"
        assert offer.merchant == "HP"
        assert offer.extra["temperature"] == 116
        assert offer.price_czk == pytest.approx(1259.29 * 25.0)
        assert offer.category == "Elektronik"

    def test_cold_deals_are_filtered_out(self):
        """Teplota je u feedů jediná obrana proti braku."""
        xml = _pepper_feed([{
            "category": "Elektronik", "merchant": "X", "price": "10€",
            "temperature": 20, "title": "Vlažný deal",
            "description": "nic moc", "guid": "cold-1",
        }])
        assert _pepper_source(xml, min_temperature=150).fetch() == []

    def test_original_price_from_description(self):
        xml = _pepper_feed([{
            "category": "Fashion", "merchant": "Zalando", "price": "29,99€",
            "temperature": 300, "title": "Bunda levně",
            "description": "Tolle Jacke statt 149,99€ nur jetzt", "guid": "j-1",
        }])
        offer = _pepper_source(xml).fetch()[0]
        assert offer.ref_price_czk == pytest.approx(149.99 * 25.0)

    def test_percent_discount_backfills_original_price(self):
        xml = _pepper_feed([{
            "category": "Fashion", "merchant": "Zalando", "price": "50€",
            "temperature": 300, "title": "Výprodej -75% auf alles",
            "description": "Rabatt", "guid": "p-1",
        }])
        offer = _pepper_source(xml).fetch()[0]
        assert offer.ref_price_czk == pytest.approx(200.0 * 25.0)

    def test_item_without_price_is_skipped(self):
        """Bez ceny nejde spočítat procento slevy, takže položka nemá cenu držet."""
        xml = _pepper_feed([{
            "category": "Elektronik", "merchant": "X", "price": "",
            "temperature": 500, "title": "Něco zadarmo?",
            "description": "bez ceny", "guid": "n-1",
        }])
        assert _pepper_source(xml).fetch() == []


FLY_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>{items}</channel></rss>"""

FLY_ITEM = """
  <item>
    <title>{title}</title>
    <link>https://www.fly4free.com/{guid}</link>
    <guid>https://www.fly4free.com/{guid}</guid>
    <pubDate>{pub_date}</pubDate>
    {categories}
  </item>"""


def _fly_feed(items):
    parts = []
    for item in items:
        cats = "".join(f"<category>{c}</category>" for c in item.get("categories", []))
        parts.append(FLY_ITEM.format(
            title=item["title"], guid=item["guid"],
            pub_date=format_datetime(
                dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(days=item.get("age_days", 1))
            ),
            categories=cats,
        ))
    return FLY_TEMPLATE.format(items="".join(parts))


def _fly_source(xml, error_fare=False, airport=None):
    cfg = Config()
    cfg.raw["sources"]["travel"]["delay_s"] = 0
    feed = {"url": "https://example.test/feed/", "error_fare": error_fare}
    if airport:
        feed["airport"] = airport
    return TravelSource(FakeHttp([xml]), FakeFx(), cfg,
                        {"name": "fly4free", "credibility": 0.8, "feeds": [feed]})


class TestTravelParser:
    def test_keeps_flights_from_configured_airports(self):
        xml = _fly_feed([{
            "title": "Turkish Airlines flights from Vienna to Uganda for €497",
            "guid": "vie-1", "categories": ["europe", "cheap flights from vienna"],
        }])
        offers = _fly_source(xml).fetch()

        assert len(offers) == 1
        assert offers[0].extra["airport"] == "VIE"
        assert offers[0].price_czk == pytest.approx(497 * 25.0)

    def test_drops_airports_we_do_not_fly_from(self):
        """Německá a polská letiště jsou ze zadání vynechaná."""
        xml = _fly_feed([{
            "title": "Flights from Munich to Sri Lanka from €470",
            "guid": "muc-1", "categories": ["europe", "cheap flights from munich"],
        }])
        assert _fly_source(xml).fetch() == []

    def test_drops_stale_items(self):
        """Error-fare feedy jsou z velké části archiv z let 2020–2021."""
        xml = _fly_feed([{
            "title": "Cheap flights from Vienna to Bali for €300",
            "guid": "old-1", "categories": ["cheap flights from vienna"],
            "age_days": 900,
        }])
        assert _fly_source(xml).fetch() == []

    def test_europe_wide_error_fare_passes_without_a_city(self):
        xml = _fly_feed([{
            "title": "CRAZY HOT Cheap flights from Europe to Latin America from €150",
            "guid": "ef-1", "categories": ["europe", "error fare"],
        }])
        offers = _fly_source(xml, error_fare=True).fetch()

        assert len(offers) == 1
        assert offers[0].extra["airport"] == "EU"
        assert offers[0].credibility > 0.9

    def test_europe_wide_item_is_dropped_when_not_error_fare(self):
        xml = _fly_feed([{
            "title": "Flights from Europe to Asia from €400",
            "guid": "eu-1", "categories": ["europe"],
        }])
        assert _fly_source(xml, error_fare=False).fetch() == []

    def test_airport_feed_takes_everything_without_matching_a_city(self):
        """`travelfree.info/tag/prague/feed/` je pro Prahu celý relevantní —
        podchytí i nabídky, které z hlavního proudu už vypadly."""
        xml = _fly_feed([{
            "title": "Turkish Airlines: flights from European cities to Bangkok from €541",
            "guid": "prg-1", "categories": ["flights"],
        }])
        offers = _fly_source(xml, airport="PRG").fetch()

        assert len(offers) == 1
        assert offers[0].extra["airport"] == "PRG"

    def test_hotel_deal_is_categorised_as_hotel(self):
        xml = _fly_feed([{
            "title": "4* hotel in Prague with spa from €89",
            "guid": "h-1", "categories": ["hotel deals"],
        }])
        offers = _fly_source(xml).fetch()

        assert len(offers) == 1
        assert offers[0].category == "hotel"

    def test_same_deal_in_two_feeds_is_deduplicated(self):
        """Tatáž nabídka bývá v hlavním i v letištním feedu."""
        item = {"title": "Flights from Prague to Nepal for €426",
                "guid": "dup-1", "categories": ["flights"]}
        cfg = Config()
        cfg.raw["sources"]["travel"]["delay_s"] = 0
        xml = _fly_feed([item])
        source = TravelSource(
            FakeHttp([xml, xml]), FakeFx(), cfg,
            {"name": "travelfree", "credibility": 0.85, "feeds": [
                {"url": "https://example.test/feed/"},
                {"url": "https://example.test/tag/prague/feed/", "airport": "PRG"},
            ]},
        )
        assert len(source.fetch()) == 1


class TestBuildSources:
    def test_only_travel_skips_the_catalog(self):
        from src.main import build_sources

        sources = build_sources(FakeHttp([]), FakeFx(), Config(), only="travel")
        assert sources, "cestovatelské zdroje se mají postavit"
        assert all(isinstance(s, TravelSource) for s in sources)

    def test_unknown_family_fails_loudly(self):
        from src.main import build_sources

        with pytest.raises(SystemExit):
            build_sources(FakeHttp([]), FakeFx(), Config(), only="letenky")
