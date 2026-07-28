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
        """Zdroj sahá na dva endpointy, takže i fake musí rozlišovat podle URL.

        Bez toho by se tytéž nabídky vrátily dvakrát a testy by měřily něco
        jiného, než na co se tváří.
        """

        def __init__(self, fares, one_way_fares=None):
            self.fares = fares
            self.one_way_fares = one_way_fares or []
            self.params: list[dict] = []
            self.urls: list[str] = []

        def get_json(self, url, params=None, **kwargs):
            self.params.append(params or {})
            self.urls.append(url)
            je_jednosmerny = "oneWayFares" in url
            return {"fares": self.one_way_fares if je_jednosmerny else self.fares}

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

    def _source(self, fares, one_way_fares=None):
        from src.sources.ryanair import RyanairSource

        cfg = load_config()
        cfg.raw["sources"]["ryanair"]["airports"] = ["PRG"]
        cfg.raw["sources"]["ryanair"]["delay_s"] = 0
        http = self.FakeHttp(fares, one_way_fares)
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

    def test_one_way_has_its_own_price_history(self):
        """Jednosměrná stojí zhruba polovinu zpáteční.

        Kdyby obě sdílely uid, střídání druhů by v cenové řadě vypadalo jako
        propad a bot by hlásil trhák pokaždé, když se pořadí prohodí.
        """
        source, _ = self._source([self._fare(price=1600.0)],
                                 [self._fare(price=800.0)])
        podle_uid = {o.uid: o for o in source.fetch()}

        assert set(podle_uid) == {"PRG-BGY", "PRG-BGY:ow"}
        assert podle_uid["PRG-BGY"].price_czk == pytest.approx(1600.0)
        assert podle_uid["PRG-BGY:ow"].price_czk == pytest.approx(800.0)

    def test_one_way_is_labelled_and_has_no_return_date(self):
        source, _ = self._source([], [self._fare()])
        offer = source.fetch()[0]

        assert "jednosměrné" in offer.name
        assert offer.extra["one_way"] is True
        assert offer.extra["inbound"] is None
        # Odkaz se musí otevřít v režimu jednosměrné, jinak Ryanair čeká
        # návratový termín a stránka zůstane viset.
        assert "isReturn=false" in offer.url
        assert "dateOut=2026-09-14" in offer.url

    def test_one_way_can_be_turned_off(self):
        from src.sources.ryanair import RyanairSource

        cfg = load_config()
        cfg.raw["sources"]["ryanair"]["airports"] = ["PRG"]
        cfg.raw["sources"]["ryanair"]["delay_s"] = 0
        cfg.raw["sources"]["ryanair"]["include_one_way"] = False
        http = self.FakeHttp([self._fare()], [self._fare()])
        offers = RyanairSource(http, self.FakeFx(), cfg).fetch()

        assert [o.uid for o in offers] == ["PRG-BGY"]
        assert not any("oneWayFares" in u for u in http.urls)

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


class TestWizzAirSource:
    """Druhý katalogový zdroj. Doplňuje Ryanair — z Vídně nelétá, zato
    z Bratislavy má 38 tras."""

    class FakeSession:
        def __init__(self):
            self.cookies = self
            self.cleared = 0

        def clear(self):
            self.cleared += 1

    class FakeHttp:
        def __init__(self, mapa, ceny, session=None):
            self.mapa = mapa
            self.ceny = ceny
            self.session = session
            self.posty: list[dict] = []

        def get(self, url, **kw):
            class R:
                text = 'src="https://be.wizzair.com/29.8.0/x.js"'
            return R()

        def get_json(self, url, **kw):
            return self.mapa

        def post_json(self, url, payload, headers=None, timeout_s=None):
            self.posty.append(payload)
            return self.ceny

    class FakeFx:
        def to_czk(self, amount, currency):
            return amount

    def _mapa(self):
        return {"cities": [
            {"iata": "PRG", "connections": [{"iata": "LTN"}, {"iata": "BCN"}]},
            {"iata": "BTS", "connections": [{"iata": "AGP"}]},
            {"iata": "BUD", "connections": [{"iata": "LTN"}]},   # cizí letiště
        ]}

    def _ceny(self):
        return {"outboundFlights": [
            {"date": "2026-09-11", "price": {"amount": 1169.0, "currencyCode": "CZK"}},
            {"date": "2026-09-12", "price": {"amount": 759.0, "currencyCode": "CZK"}},
        ]}

    def _source(self, session=None, routes_per_run=10):
        from src.sources.wizzair import WizzAirSource

        cfg = load_config()
        cfg.raw["sources"]["wizzair"]["delay_s"] = 0
        cfg.raw["sources"]["wizzair"]["routes_per_run"] = routes_per_run
        http = self.FakeHttp(self._mapa(), self._ceny(), session)
        return WizzAirSource(http, self.FakeFx(), cfg), http

    def test_only_our_airports_are_used(self):
        source, _ = self._source()
        assert source.routes() == [("BTS", "AGP"), ("PRG", "BCN"), ("PRG", "LTN")]

    def test_cheapest_day_in_the_window_wins(self):
        """Historie má sledovat dosažitelné minimum na trase — to je to,
        co člověk hledá, když se dívá po levné letence."""
        source, _ = self._source()
        offer = next(o for o in source.fetch() if o.uid == "PRG-LTN")

        assert offer.price_czk == pytest.approx(759.0)
        assert offer.extra["outbound"] == "2026-09-12"

    def test_cookies_are_dropped_before_every_request(self):
        """Server přiloží RequestVerificationToken a u dalšího dotazu ho chce
        zpátky. Bez zahození projde z dávky jen první trasa."""
        session = self.FakeSession()
        source, http = self._source(session=session)
        source.fetch()

        assert session.cleared == len(http.posty) == 3

    def test_routes_rotate_between_runs(self, tmp_path):
        """58 tras při každém běhu by bylo přes osm tisíc požadavků denně."""
        from src.store import Store

        store = Store(tmp_path / "w.db")
        source, http = self._source(routes_per_run=2)
        source.store = store
        source.fetch()
        prvni = [tuple(p["flightList"][0].values())[:2] for p in http.posty]

        source2, http2 = self._source(routes_per_run=2)
        source2.store = store
        source2.fetch()
        druhy = [tuple(p["flightList"][0].values())[:2] for p in http2.posty]
        store.close()

        assert len(prvni) == 2
        assert prvni != druhy, "druhý běh má pokračovat, ne opakovat totéž"

    def test_broken_map_does_not_kill_the_run(self):
        from src.sources.wizzair import WizzAirSource

        class Broken:
            session = None

            def get(self, *a, **kw):
                raise RuntimeError("web nedostupný")

            def get_json(self, *a, **kw):
                raise RuntimeError("mapa nedostupná")

        assert WizzAirSource(Broken(), self.FakeFx(), load_config()).fetch() == []


class TestWizzAirOkno:
    """Okno farechart je 2 x dayInterval + 1 dni. API pusti nejvys desitku."""

    def _source(self, day_interval=None):
        from src.sources.wizzair import WizzAirSource

        cfg = load_config()
        if day_interval is not None:
            cfg.raw["sources"]["wizzair"]["day_interval"] = day_interval
        return WizzAirSource(None, None, cfg)

    def test_default_uses_the_widest_window_api_allows(self):
        from src.sources.wizzair import MAX_DAY_INTERVAL

        assert self._source().day_interval == MAX_DAY_INTERVAL

    def test_value_above_the_limit_is_clamped(self):
        """Mimo meze API dotaz odmítne validací a zdroj by zmlkl celý."""
        from src.sources.wizzair import MAX_DAY_INTERVAL, MIN_DAY_INTERVAL

        assert self._source(99).day_interval == MAX_DAY_INTERVAL
        assert self._source(1).day_interval == MIN_DAY_INTERVAL

    def test_days_without_a_flight_are_dropped(self):
        """`noData` má `amount: 0` — bez filtru by z toho byla letenka zdarma."""
        source = self._source()
        source.name = "wizzair"
        source.fx = type("Fx", (), {"to_czk": staticmethod(lambda a, m: a * 25)})()

        data = {"outboundFlights": [
            {"priceType": "noData", "date": "2026-09-01T00:00:00",
             "price": {"amount": 0.0, "currencyCode": "EUR"}},
            {"priceType": "regular", "date": "2026-09-05T00:00:00",
             "price": {"amount": 34.0, "currencyCode": "EUR"}},
        ]}
        offer = source._to_offer("BTS", "BER", data)

        assert offer.price_czk == 34.0 * 25
        assert offer.extra["outbound"] == "2026-09-05"

    def test_route_without_any_price_is_skipped(self):
        source = self._source()
        data = {"outboundFlights": [
            {"priceType": "noData", "price": {"amount": 0.0, "currencyCode": "EUR"}}]}
        assert source._to_offer("BTS", "BER", data) is None


class TestTravelpayouts:
    """Odpověď se bez tokenu ověřit nedala, proto tvrdší testy než jinde.

    Mapování polí vzniklo z dokumentace, ne z měření. Parser tedy musí přežít
    to, že se pole jmenuje jinak — jedna výjimka by uzemnila celý zdroj.
    """

    def _source(self, **prepis):
        from src.sources.travelpayouts import TravelpayoutsSource

        cfg = load_config()
        cfg.raw["sources"]["travelpayouts"].update(prepis)
        source = TravelpayoutsSource(None, None, cfg)
        source.fx = type("Fx", (), {"to_czk": staticmethod(lambda a, m: a)})()
        return source

    def _row(self, **zmeny):
        row = {
            "origin": "PRG", "destination": "BKK", "price": 11900,
            "transfers": 1, "airline": "QR",
            "departure_at": "2026-10-13T21:45:00+02:00",
            "return_at": "2026-10-24T09:15:00+07:00",
            "link": "/searches/PRG1310BKK2410",
        }
        row.update(zmeny)
        return row

    def test_route_is_the_uid_not_the_date(self):
        """Stejný důvod jako u Ryanairu — jinak se historie nikdy nenasbírá."""
        from src.sources.base import CATALOG

        offer = self._source()._to_offer("PRG", self._row())

        assert offer.uid == "PRG-BKK"
        assert offer.kind == CATALOG
        assert offer.category == "flight"
        assert offer.price_czk == 11900

    def test_relative_link_becomes_a_real_url(self):
        offer = self._source()._to_offer("PRG", self._row())
        assert offer.url == "https://www.aviasales.com/searches/PRG1310BKK2410"

    def test_too_many_transfers_are_dropped(self):
        """Dva přestupy bývají levné na papíře a nepoužitelné v praxi."""
        source = self._source(max_transfers=1)
        assert source._to_offer("PRG", self._row(transfers=3)) is None
        assert source._to_offer("PRG", self._row(transfers=1)) is not None

    def test_token_never_travels_in_the_url(self):
        """V query stringu by klíč skončil v logu proxy i v historii serveru."""
        params = self._source()._params("PRG")
        assert not any("token" in k.lower() for k in params)

    def test_query_asks_for_every_destination(self):
        """Bez cílové stanice vrátí nejlevnější cíle — celý smysl zdroje."""
        params = self._source()._params("PRG")
        assert "destination" not in params
        assert params["origin"] == "PRG"
        assert params["sorting"] == "price"

    def test_row_without_the_essentials_is_skipped(self):
        source = self._source()
        for rozbita in ({}, {"price": "nesmysl"}, {"destination": "BKK", "price": []},
                        {"destination": "", "price": 100},
                        {"destination": "BKK", "price": 0}):
            assert source._to_offer("PRG", rozbita) is None

    def test_renamed_fields_do_not_raise(self):
        """Nejhorší scénář: API se změní. Zdroj smí zmlknout, ne spadnout.

        Nesmyslný počet přestupů se bere jako nula — přijít o údaj o přestupu
        je pořád lepší než přijít o celou nabídku.
        """
        source = self._source()
        offer = source._to_offer("PRG", {"destination": "BKK", "price": 100,
                                         "transfers": "ne", "link": None})

        assert offer.extra["transfers"] == 0
        assert offer.url == "https://www.aviasales.com"

    def test_without_token_the_source_is_silent(self):
        source = self._source()
        source.token = ""
        assert source.fetch() == []


class TestTravelpayoutsDotaz:
    """Parametry vznikly z dokumentace a API na ne odpovedelo 400."""

    def _params(self):
        from src.sources.travelpayouts import TravelpayoutsSource

        source = TravelpayoutsSource(None, None, load_config())
        return source._params("PRG")

    def test_no_date_filter_at_all(self):
        """`departure_at` neni mez okna, ale skutecny termin letu.

        Rozsah pul roku od sebe vracel 400 a i spravne zadany mesic odpoved
        zuzil ze 100 tras na 31. Zmereno s ostrym tokenem 28. 7. 2026.
        """
        params = self._params()
        assert "departure_at" not in params
        assert "return_at" not in params

    def test_limit_is_sent(self):
        """Bez `limit` vrati API 30 tras, s nim 100."""
        assert self._params()["limit"] > 30

    def test_no_destination_means_anywhere(self):
        assert "destination" not in self._params()


class TestChybovaHlaskaNeprozradiKlic:
    def test_query_string_is_stripped_from_errors(self):
        """`requests` pise do hlasky celou URL i s query stringem."""
        from src.net import _bez_query

        text = _bez_query(RuntimeError(
            "400 Client Error for url: https://api.example.com/v3/x?origin=PRG&token=tajne"))

        assert "tajne" not in text
        assert "origin" not in text
        assert "https://api.example.com/v3/x?…" in text
