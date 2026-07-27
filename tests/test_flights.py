"""Ceník letenek.

Používá OSTRÝ `flights.yaml` z repozitáře, ne vymyšlený — testy tedy hlídají
i to, jestli ceník pořád dává smysl. Titulky jsou skutečné, stažené ze zdrojů
27. 7. 2026.
"""

import pytest

from src.config import load_config
from src.oracles.flights import FlightOracle
from src.sources.base import FEED, Offer


@pytest.fixture
def oracle():
    return FlightOracle(load_config().flights)


def _flight(name, price_czk=10_000.0, category="flight", **extra):
    return Offer(source="travelfree", kind=FEED, uid=name[:12], name=name,
                 price_czk=price_czk, url="http://x", category=category,
                 merchant="travelfree", credibility=0.85, extra=extra)


class TestRegionMatching:
    @pytest.mark.parametrize("name,region", [
        ("Cheap flights from Vienna to Phuket, THAILAND from €458", "asie-jihovychodni"),
        ("Cheap full-service flights from Vienna to Hong Kong for €447", "asie-vychodni"),
        ("Flights from Vienna to ARGENTINA from €688", "jizni-amerika"),
        ("Cheap flights from many European cities to Pakistan from €322",
         "indie-subkontinent"),
        ("KLM flights from Vienna & Frankfurt to Cape Town, South Africa for €543",
         "afrika-subsaharska"),
        ("Z Bratislavy do arménského Jerevanu na podzim. Letenky od 970 Kč",
         "blizky-vychod"),
        ("Malta z Bratislavy na podzim. Letenky od 920 Kč", "evropa-nizkonakladove"),
    ])
    def test_real_titles_land_in_the_right_region(self, oracle, name, region):
        offer = _flight(name)
        assert oracle.value_of(offer) is not None
        assert offer.extra["flight_region"] == region

    def test_airline_name_does_not_decide_the_destination(self, oracle):
        """„Air France … to Tunisia" míří do Tuniska, ne do Francie. Názvy
        aerolinek obsahují názvy zemí a je to nejzrádnější past celého ceníku."""
        offer = _flight("Air France flights from many European cities to Tunisia from €166")
        oracle.value_of(offer)
        assert offer.extra["flight_region"] == "afrika-sever"

    def test_departure_city_loses_to_the_destination(self, oracle):
        """Zdroje píšou i výchozí město. Bere se ten vzdálenější region —
        cíl je u těchhle webů skoro vždycky ta exotičtější půlka."""
        offer = _flight("Full service flights from Milan to PHUKET for €454")
        oracle.value_of(offer)
        assert offer.extra["flight_region"] == "asie-jihovychodni"

    def test_unknown_destination_is_not_guessed(self, oracle):
        assert oracle.value_of(_flight("Flights to Nowhereland for €100")) is None

    def test_czech_declension_that_changes_a_consonant(self, oracle):
        """„Boloňa" se skloňuje na „do Boloně" — `ň` se před měkkým `ě` mění
        na `n`, takže výraz `boloň` v titulku doslova není. Řeší se srovnáním
        bez diakritiky, ne dalším výrazem v ceníku."""
        offer = _flight("Do Boloně na týden z Prahy v říjnu. Letenky od 978 Kč")
        assert oracle.value_of(offer) is not None
        assert offer.extra["flight_region"] == "evropa-nizkonakladove"

    def test_destination_written_without_diacritics(self, oracle):
        offer = _flight("Letenky z Prahy do Recka od 2 400 Kc")
        assert oracle.value_of(offer) is not None


class TestValuation:
    def test_value_is_the_typical_fare(self, oracle):
        offer = _flight("Cheap flights from Vienna to Phuket, THAILAND from €458")
        value = oracle.value_of(offer)

        assert value.origin == "flights"
        assert value.real_value_czk > 10_000, "dálkový let nemá běžnou cenu pár tisíc"

    def test_great_price_is_carried_as_an_absolute_limit(self, oracle):
        """Jednotný poměr nefunguje: skvělá cena do Evropy leží na 36 %
        běžné ceny, do jihovýchodní Asie na 62 %."""
        europe = _flight("Malta z Bratislavy. Letenky od 920 Kč")
        asia = _flight("Cheap flights from Vienna to Phuket, THAILAND from €458")
        oracle.value_of(europe)
        oracle.value_of(asia)

        assert europe.extra["instant_below_czk"] < asia.extra["instant_below_czk"]

    def test_package_tour_is_left_alone(self, oracle):
        """Zájezd má v ceně i ubytování, takže cena letenky o něm nic neříká."""
        offer = _flight("Zakynthos s plnou penzí z Prahy na týden. Zájezd od 15 990 Kč",
                        category="hotel")
        assert oracle.value_of(offer) is None

    def test_empty_pricelist_does_not_crash(self):
        assert FlightOracle([]).value_of(_flight("Flights to Phuket")) is None


class TestScoringWithPricelist:
    """Ceník musí umět zapnout okamžité upozornění i u dálkového letu."""

    def _scorer(self, tmp_path):
        from src.oracles.declared import DeclaredOracle
        from src.oracles.history import HistoryOracle
        from src.score import Scorer
        from src.shipping import ShippingPolicy
        from src.store import Store

        cfg = load_config()
        store = Store(tmp_path / "f.db")
        history = HistoryOracle(store)
        scorer = Scorer(cfg, store, [history, FlightOracle(cfg.flights),
                                     DeclaredOracle()], history,
                        ShippingPolicy(cfg.merchants))
        return scorer, store

    def test_bargain_long_haul_reaches_instant(self, tmp_path):
        """Poměr 0,45 kalibrovaný na Evropu by tenhle let umlčel — do Asie je
        i skvělá cena kolem 60 % té běžné."""
        from src.score import INSTANT

        scorer, store = self._scorer(tmp_path)
        offer = _flight("Cheap flights from Vienna to Phuket, THAILAND from €300",
                        price_czk=7_500.0)
        verdict = scorer.prescore([offer])[0]
        store.close()

        assert verdict.value_ratio > 0.45, "na poměr by to nedosáhlo"
        assert verdict.level == INSTANT
        assert any("skvělé ceny" in r for r in verdict.reasons)

    def test_ordinary_long_haul_stays_out_of_instant(self, tmp_path):
        from src.score import INSTANT

        scorer, store = self._scorer(tmp_path)
        offer = _flight("Cheap flights from Vienna to Phuket, THAILAND from €500",
                        price_czk=12_500.0)
        verdict = scorer.prescore([offer])[0]
        store.close()

        assert verdict.level != INSTANT


class TestRyanairSource:
    """Jediný katalogový zdroj u cestování — vidíme tutéž trasu opakovaně."""

    class FakeHttp:
        def __init__(self, fares):
            self.fares = fares
            self.params: list[dict] = []

        def get_json(self, url, params=None, **kwargs):
            self.params.append(params or {})
            return {"fares": self.fares}

    class FakeFx:
        def to_czk(self, amount, currency):
            return amount * (1.0 if currency.upper() == "CZK" else 25.0)

    def _fare(self, dest="BGY", city="Milan Bergamo", price=815.0):
        return {
            "outbound": {"departureDate": "2026-09-14T06:00:00",
                         "arrivalAirport": {"iataCode": dest, "name": city,
                                            "city": {"name": city}}},
            "summary": {"price": {"value": price, "currencyCode": "CZK"}},
        }

    def _source(self, fares):
        from src.sources.ryanair import RyanairSource

        cfg = load_config()
        cfg.raw["sources"]["ryanair"]["airports"] = ["PRG"]
        cfg.raw["sources"]["ryanair"]["delay_s"] = 0
        http = self.FakeHttp(fares)
        return RyanairSource(http, self.FakeFx(), cfg), http

    def test_route_is_the_identity_not_the_date(self):
        """Uid je trasa, ne termín — jinak by se cenová historie nikdy
        nenasbírala a celý smysl zdroje by padl."""
        source, _ = self._source([self._fare(), self._fare(price=1200.0)])
        offers = source.fetch()

        assert len(offers) == 1
        assert offers[0].uid == "PRG-BGY"

    def test_offer_is_a_catalog_item(self):
        from src.sources.base import CATALOG

        source, _ = self._source([self._fare()])
        offer = source.fetch()[0]

        assert offer.kind == CATALOG
        assert offer.category == "flight"
        assert offer.price_czk == pytest.approx(815.0)
        assert offer.extra["airport"] == "PRG"

    def test_broken_airport_does_not_kill_the_source(self):
        from src.sources.ryanair import RyanairSource

        class Broken:
            def get_json(self, *a, **kw):
                raise RuntimeError("Ryanair spadl")

        cfg = load_config()
        assert RyanairSource(Broken(), self.FakeFx(), cfg).fetch() == []

    def test_pricelist_leaves_carrier_fares_alone(self):
        """Ceník vznikl z cen, které weby vypsaly jako AKCI. Posuzovat jím
        ceník samotného dopravce je kruh — o té ceně smí rozhodnout jen
        vlastní historie."""
        source, _ = self._source([self._fare()])
        offer = source.fetch()[0]

        assert FlightOracle(load_config().flights).value_of(offer) is None
