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
    def __init__(self, content: bytes, status_code: int = 200, headers=None,
                 url: str = "https://example.test/") -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = content.decode("utf-8", "replace")


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

    def test_unknown_foreign_airport_is_dropped(self):
        """Letiště, které není ani domácí, ani v seznamu uzlů."""
        xml = _fly_feed([{
            "title": "Flights from Stuttgart to Sri Lanka from €470",
            "guid": "str-1", "categories": ["europe", "cheap flights from stuttgart"],
        }])
        assert _fly_source(xml).fetch() == []

    def test_hub_departure_is_kept_but_marked(self):
        """Mnichov je uzel: parser položku pustí a označí ji.

        O tom, jestli se ta cesta vyplatí, rozhodne až
        `main.drop_pointless_hubs` podle regionu cíle.
        """
        xml = _fly_feed([{
            "title": "Flights from Munich to Sri Lanka from €470",
            "guid": "muc-1", "categories": ["europe", "cheap flights from munich"],
        }])
        offers = _fly_source(xml).fetch()

        assert len(offers) == 1
        assert offers[0].extra["hub_departure"] == "MUC"
        assert offers[0].extra["airport"] == "MUC"

    def test_home_airport_is_not_marked_as_a_hub(self):
        xml = _fly_feed([{
            "title": "Flights from Prague to Sri Lanka from €470",
            "guid": "prg-1", "categories": ["cheap flights from prague"],
        }])
        offers = _fly_source(xml).fetch()

        assert offers[0].extra["hub_departure"] is None
        assert offers[0].extra["airport"] == "PRG"

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


class TestCzechTravelFeed:
    """cestujlevne.com — česky, ceny v korunách, světadíl přímo ve feedu."""

    def _source(self, xml):
        cfg = Config()
        cfg.raw["sources"]["travel"]["delay_s"] = 0
        return TravelSource(FakeHttp([xml]), FakeFx(), cfg, {
            "name": "cestujlevne", "credibility": 0.85, "currency": "CZK",
            "feeds": [{"url": "https://www.cestujlevne.com/feed"}],
        })

    def _feed(self, title, categories=("Letenky", "Evropa"), region="evropa"):
        cats = "".join(f"<category>{c}</category>" for c in categories)
        web = f"<category-web>{region}</category-web>" if region else ""
        return FLY_TEMPLATE.format(items=f"""
  <item>
    <title>{title}</title>
    <link>https://www.cestujlevne.com/x</link>
    <guid>https://www.cestujlevne.com/x</guid>
    <pubDate>{format_datetime(dt.datetime.now(dt.timezone.utc))}</pubDate>
    {cats}{web}
  </item>""")

    def test_czech_declension_is_matched(self):
        """„z Prahy" neobsahuje „praha" — bez kmenů by nefungovalo nic."""
        for title, code in [
            ("Neapol o víkendu z Prahy. Letenky od 1 419 Kč", "PRG"),
            ("Malta z Bratislavy na podzim. Letenky od 920 Kč", "BTS"),
            ("Týden v Kalábrii z Ostravy. Zájezd od 13 590 Kč", "OSR"),
            ("Sahl Hasheesh z Pardubic. Zájezd od 13 690 Kč", "PED"),
        ]:
            offers = self._source(self._feed(title)).fetch()
            assert len(offers) == 1, title
            assert offers[0].extra["airport"] == code, title

    def test_price_is_read_in_czk_not_eur(self):
        offers = self._source(self._feed(
            "Do Boloně na týden z Prahy v říjnu. Letenky od 978 Kč")).fetch()

        assert offers[0].currency == "CZK"
        assert offers[0].price_czk == pytest.approx(978.0)

    def test_region_comes_from_the_feed(self):
        offers = self._source(self._feed(
            "Mexico City z Prahy. Letenky od 13 676 Kč",
            categories=("Letenky", "Střední Amerika a Karibik"),
            region="stredni-amerika")).fetch()

        assert offers[0].extra["region"] == "stredni-amerika"

    def test_package_tour_is_not_a_plain_flight(self):
        flight = self._source(self._feed(
            "Malta z Bratislavy. Letenky od 920 Kč")).fetch()[0]
        package = self._source(self._feed(
            "Zakynthos z Prahy na týden. Zájezd od 15 990 Kč")).fetch()[0]

        assert flight.category == "flight"
        assert package.category == "hotel"

    def test_missing_region_is_not_an_error(self):
        offers = self._source(self._feed(
            "Malta z Bratislavy. Letenky od 920 Kč", region=None)).fetch()
        assert offers[0].extra["region"] is None


class TestConditionalFetch:
    """Hlavní feed travelfree.info měří 14 MB. Při běhu každých deset minut
    by se stahovalo zhruba 3,5 GB denně, přestože se obsah skoro nemění."""

    class RecordingHttp(FakeHttp):
        """Zaznamenává odeslané hlavičky, ať se dá ověřit podmíněný dotaz."""

        def __init__(self, payloads, status=200, headers=None):
            super().__init__(payloads)
            self.status = status
            self.reply_headers = headers or {}
            self.sent: list[dict] = []

        def get(self, url, **kwargs):
            self.sent.append(kwargs.get("headers") or {})
            resp = super().get(url)
            return FakeResponse(resp.content, self.status, self.reply_headers)

    def _source(self, http, store):
        cfg = Config()
        cfg.raw["sources"]["travel"]["delay_s"] = 0
        return TravelSource(http, FakeFx(), cfg, {
            "name": "travelfree", "credibility": 0.85,
            "feeds": [{"url": "https://example.test/feed/"}],
        }, store)

    def _xml(self):
        return _fly_feed([{"title": "Flights from Prague to Nepal for €426",
                           "guid": "n-1", "categories": ["flights"]}])

    def test_etag_is_remembered_and_sent_back(self, tmp_path):
        from src.store import Store

        store = Store(tmp_path / "e.db")
        http = self.RecordingHttp([self._xml()], headers={"ETag": '"abc"'})
        assert len(self._source(http, store).fetch()) == 1

        http2 = self.RecordingHttp([self._xml()], headers={"ETag": '"abc"'})
        self._source(http2, store).fetch()
        store.close()

        assert "If-None-Match" not in http.sent[0], "poprvé nemáme co poslat"
        assert http2.sent[0]["If-None-Match"] == '"abc"'

    def test_unchanged_feed_is_skipped(self, tmp_path):
        from src.store import Store

        store = Store(tmp_path / "e.db")
        http = self.RecordingHttp([self._xml()], status=304)
        offers = self._source(http, store).fetch()
        store.close()

        assert offers == [], "304 znamená beze změny, není co zpracovat"

    def test_without_store_nothing_is_cached(self):
        """Dry-run stahuje vždycky nanovo, ať ladění ukáže všechno."""
        http = self.RecordingHttp([self._xml()], headers={"ETag": '"abc"'})
        source = self._source(http, store=None)
        source.fetch()
        source.fetch()

        assert all("If-None-Match" not in sent for sent in http.sent)

    def test_feeds_ask_for_rss_like_a_browser(self):
        """`requests` posílá holé `*/*` — u WAF nad WordPressem je to jeden
        ze signálů „tohle je bot". Prohlížeč řekne, co čeká."""
        http = self.RecordingHttp([self._xml()])
        self._source(http, store=None).fetch()

        assert "rss" in http.sent[0]["Accept"]


class TestRyanairOdkaz:
    """Odkaz musí vést na konkrétní termín, jinak je k ničemu."""

    def _fare(self, tam="2026-10-13T21:45:00", zpet="2026-10-14T11:30:00"):
        return {
            "outbound": {"departureDate": tam,
                         "arrivalAirport": {"iataCode": "BRS", "name": "Bristol",
                                            "city": {"name": "Bristol"}}},
            "inbound": {"departureDate": zpet},
            "summary": {"price": {"value": 918.48, "currencyCode": "CZK"}},
        }

    def _offer(self, fare):
        from src.sources.ryanair import RyanairSource
        return RyanairSource.__new__(RyanairSource)._to_offer.__func__(
            _Fake(), "PRG", fare)

    def test_link_carries_both_dates(self):
        """Bez `dateOut` a `dateIn` skončí rezervační stránka na
        „Nemáte aktivní vyhledávání" — ověřeno v prohlížeči."""
        url = self._offer(self._fare()).url

        assert "dateOut=2026-10-13" in url
        assert "dateIn=2026-10-14" in url
        assert "originIata=PRG" in url and "destinationIata=BRS" in url
        # Jejich aplikace si při obnovení stránky čte duplicitní `tp*` sadu.
        assert "tpStartDate=2026-10-13" in url and "tpEndDate=2026-10-14" in url

    def test_fare_without_dates_falls_back_to_search(self):
        offer = self._offer(self._fare(tam="", zpet=""))
        assert offer.url == "https://www.ryanair.com/cz/cs"


class TestTermVeZprave:
    """Nejlevnější kombinace nemusí dávat smysl a musí to jít poznat ze zprávy."""

    def test_overnight_turnaround_is_visible(self):
        from src.notify import format_term

        term = format_term({"outbound": "2026-10-13T21:45:00",
                            "outbound_arrival": "2026-10-13T22:55:00",
                            "inbound": "2026-10-14T11:30:00"})

        assert term == "út 13. 10. 21:45 → 22:55 · zpět st 14. 10. 11:30 · 1 noc"

    def test_week_long_trip(self):
        from src.notify import format_term

        term = format_term({"outbound": "2026-09-05T06:20:00",
                            "outbound_arrival": "2026-09-05T08:05:00",
                            "inbound": "2026-09-12T19:40:00"})
        assert term.endswith("7 nocí")

    def test_date_without_time_does_not_invent_midnight(self):
        """Wizz Air vrací holé datum; „00:00" by byl vymyšlený údaj o odletu."""
        from src.notify import format_term

        assert format_term({"outbound": "2026-09-11"}) == "pá 11. 9. · jednosměrná"

    def test_item_without_a_term_is_silent(self):
        from src.notify import format_term

        assert format_term({"airport": "PRG"}) is None

    def test_ryanair_offer_carries_arrival_time(self):
        fare = TestRyanairOdkaz()._fare()
        fare["outbound"]["arrivalDate"] = "2026-10-13T22:55:00"
        offer = TestRyanairOdkaz()._offer(fare)

        assert offer.extra["outbound_arrival"] == "2026-10-13T22:55:00"
        assert offer.extra["inbound"] == "2026-10-14T11:30:00"


class _Fake:
    """Minimum, které `_to_offer` potřebuje — kurz a název zdroje."""
    name = "ryanair"

    class fx:
        @staticmethod
        def to_czk(amount, currency):
            return amount


class TestBuildSources:
    def test_only_travel_skips_kinguin_and_pepper(self):
        """Rychlý timer smí sáhnout jen na cestování. Ryanair do něj patří
        taky, i když je to katalog a ne feed."""
        from src.main import build_sources
        from src.sources.ryanair import RyanairSource
        from src.sources.wizzair import WizzAirSource

        sources = build_sources(FakeHttp([]), FakeFx(), Config(), only="travel")
        types = {type(s) for s in sources}

        assert types == {TravelSource, RyanairSource, WizzAirSource}

    def test_unknown_family_fails_loudly(self):
        from src.main import build_sources

        with pytest.raises(SystemExit):
            build_sources(FakeHttp([]), FakeFx(), Config(), only="letenky")


class TestNeopakovat4xx:
    """403 se opakováním nespraví.

    Trojnásobek požadavků na WAF, který nás právě odmítl, je nejjistější
    způsob, jak si blokaci potvrdit natrvalo — a v logu to vypadá stejně
    jako jediný pokus.
    """

    def _http(self, status):
        from src.net import Http

        http = Http("test-agent", timeout_s=1, retries=3)
        pokusy = []

        def fake_get(url, **kwargs):
            pokusy.append(url)
            return FakeResponse(b"ne", status, {})

        http.session.get = fake_get
        return http, pokusy

    def test_forbidden_is_asked_only_once(self):
        from src.net import TrvaleOdmitnuto

        http, pokusy = self._http(403)
        with pytest.raises(TrvaleOdmitnuto):
            http.get("https://example.test/feed/")

        assert len(pokusy) == 1

    def test_rate_limit_is_still_retried(self):
        """429 znamená „teď ne", ne „ne" — tam opakování smysl dává."""
        http, pokusy = self._http(429)
        with pytest.raises(RuntimeError):
            http.get("https://example.test/feed/")

        assert len(pokusy) == 3

    def test_probe_returns_the_code_instead_of_raising(self):
        """Pro oťukání verze API je 404 platná odpověď, ne porucha."""
        http, _ = self._http(404)
        assert http.probe("https://example.test/x") == 404
