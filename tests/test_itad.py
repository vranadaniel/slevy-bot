"""ITAD oracle: dávkování, cache a brána na historické minimum."""

import pytest

from src.config import load_config
from src.oracles.itad import ItadOracle
from src.sources.base import CATALOG, Offer
from src.store import Store


class FakeFx:
    def to_czk(self, amount: float, currency: str) -> float:
        return amount * {"CZK": 1.0, "EUR": 25.0, "USD": 21.0}[currency.upper()]


class FakeHttp:
    """Zaznamenává volání, ať se dá ověřit dávkování a cache."""

    def __init__(self, lookup=None, lows=None) -> None:
        self.lookup = lookup or {}
        self.lows = lows or []
        self.calls: list[str] = []

    def post_json(self, url, payload, headers=None, timeout_s=None):
        self.calls.append(url)
        if "lookup" in url:
            return {title: self.lookup.get(title) for title in payload}
        return [row for row in self.lows if row["id"] in payload]


def _low_row(game_id, low=120.0, regular=1500.0, shop="Steam", cut=92):
    return {
        "id": game_id,
        "low": {
            "shop": {"id": 61, "name": shop},
            "price": {"amount": low, "currency": "CZK"},
            "regular": {"amount": regular, "currency": "CZK"},
            "cut": cut,
        },
    }


def _game(name="Gothic 1 Remake PC Steam CD Key", uid="g1", price=300.0):
    return Offer(source="kinguin", kind=CATALOG, uid=uid, name=name,
                 price_czk=price, url="http://x",
                 extra={"product_type": "GAME"})


@pytest.fixture
def setup(tmp_path):
    cfg = load_config()
    cfg.itad_key = "test-key"
    store = Store(tmp_path / "i.db")
    yield cfg, store
    store.close()


class TestResolveAndValue:
    def test_resolves_title_and_returns_regular_price(self, setup):
        cfg, store = setup
        http = FakeHttp(lookup={"Gothic 1 Remake": "uuid-1"},
                        lows=[_low_row("uuid-1", low=120.0, regular=1500.0)])
        oracle = ItadOracle(http, store, FakeFx(), cfg)

        offer = _game()
        oracle.prepare([offer])
        value = oracle.value_of(offer)

        assert value is not None
        assert value.real_value_czk == 1500.0
        assert value.origin == "itad"
        assert offer.extra["itad_low_czk"] == 120.0
        assert offer.extra["itad_shop"] == "Steam"

    def test_unknown_game_is_not_priced(self, setup):
        cfg, store = setup
        oracle = ItadOracle(FakeHttp(lookup={}), store, FakeFx(), cfg)
        offer = _game(name="Uplne Neznama Hra PC Steam CD Key")
        oracle.prepare([offer])
        assert oracle.value_of(offer) is None

    def test_non_game_is_ignored(self, setup):
        cfg, store = setup
        http = FakeHttp()
        oracle = ItadOracle(http, store, FakeFx(), cfg)
        offer = Offer(source="kinguin", kind=CATALOG, uid="s1",
                      name="Google Gemini AI Pro 18 Months", price_czk=65.0,
                      url="http://x", extra={"product_type": "INGAME_TOPUP"})
        oracle.prepare([offer])

        assert http.calls == [], "u předplatného se ITAD ptát nemá"
        assert oracle.value_of(offer) is None

    def test_foreign_currency_is_converted(self, setup):
        cfg, store = setup
        row = _low_row("uuid-1")
        row["low"]["price"] = {"amount": 5.0, "currency": "EUR"}
        row["low"]["regular"] = {"amount": 60.0, "currency": "EUR"}
        oracle = ItadOracle(FakeHttp(lookup={"Gothic 1 Remake": "uuid-1"}, lows=[row]),
                            store, FakeFx(), cfg)
        offer = _game()
        oracle.prepare([offer])

        assert oracle.value_of(offer).real_value_czk == 60.0 * 25
        assert offer.extra["itad_low_czk"] == 5.0 * 25


class TestCaching:
    def test_second_run_does_not_refetch(self, setup):
        cfg, store = setup
        http = FakeHttp(lookup={"Gothic 1 Remake": "uuid-1"}, lows=[_low_row("uuid-1")])

        ItadOracle(http, store, FakeFx(), cfg).prepare([_game()])
        calls_after_first = len(http.calls)
        ItadOracle(http, store, FakeFx(), cfg).prepare([_game()])

        assert len(http.calls) == calls_after_first, "druhý běh má jet celý z cache"

    def test_unmatched_titles_are_cached_too(self, setup):
        """Bez toho by se marné dotazy opakovaly každou půlhodinu."""
        cfg, store = setup
        http = FakeHttp(lookup={})

        ItadOracle(http, store, FakeFx(), cfg).prepare([_game(name="Nic PC Steam CD Key")])
        calls_after_first = len(http.calls)
        ItadOracle(http, store, FakeFx(), cfg).prepare([_game(name="Nic PC Steam CD Key")])

        assert len(http.calls) == calls_after_first

    def test_missing_key_skips_everything(self, setup):
        cfg, store = setup
        cfg.itad_key = ""
        http = FakeHttp()
        ItadOracle(http, store, FakeFx(), cfg).prepare([_game()])
        assert http.calls == []

    def test_api_failure_does_not_raise(self, setup):
        cfg, store = setup

        class Broken:
            def post_json(self, *a, **kw):
                raise RuntimeError("ITAD spadlo")

        oracle = ItadOracle(Broken(), store, FakeFx(), cfg)
        oracle.prepare([_game()])          # nesmí vyhodit výjimku
        assert oracle.value_of(_game()) is None


class TestHistoricalLowGate:
    """Jádro toho, proč ITAD vůbec přidáváme."""

    def _scorer(self, store, cfg):
        from src.oracles.declared import DeclaredOracle
        from src.oracles.history import HistoryOracle
        from src.score import Scorer
        from src.shipping import ShippingPolicy
        history = HistoryOracle(store)
        return history, Scorer(cfg, store, [history, DeclaredOracle()],
                               history, ShippingPolicy(cfg.merchants))

    def _verdict(self, setup, price, low, regular=1500.0):
        cfg, store = setup
        http = FakeHttp(lookup={"Gothic 1 Remake": "uuid-1"},
                        lows=[_low_row("uuid-1", low=low, regular=regular)])
        oracle = ItadOracle(http, store, FakeFx(), cfg)
        offer = _game(price=price)
        offer.credibility = 0.9
        oracle.prepare([offer])

        _, scorer = self._scorer(store, cfg)
        scorer.oracles.insert(0, oracle)
        return scorer.prescore([offer])[0]

    def test_deal_that_was_once_cheaper_is_not_instant(self, setup):
        """Jádro věci: proti doporučené ceně 1 500 Kč vypadá 60 Kč jako trhák,
        ale hra už jinde byla za 40 Kč — takže to žádná zpráva není.

        Poměr k doporučené ceně u her nerozlišuje nic; na šedém trhu jsou
        skoro všechny za pár procent."""
        from src.score import NONE
        verdict = self._verdict(setup, price=60.0, low=40.0)

        assert verdict.value_ratio == pytest.approx(0.04), "poměr by sám o sobě stačil"
        assert verdict.level == NONE

    def test_new_all_time_low_is_instant(self, setup):
        """Výrazně levnější, než to kdy kde bylo — to je zpráva."""
        from src.score import INSTANT
        verdict = self._verdict(setup, price=25.0, low=40.0)

        assert verdict.level == INSTANT
        assert any("historické minimum" in r for r in verdict.reasons)

    def test_matching_the_low_only_reaches_digest(self, setup):
        """Vyrovnání minima je hezké, ale na okamžité upozornění to nestačí."""
        from src.score import DIGEST
        verdict = self._verdict(setup, price=38.0, low=40.0)

        assert verdict.level == DIGEST

    def test_ordinary_grey_market_price_is_silent(self, setup):
        """300 Kč při minimu 120 Kč je běžná cena, ne nález."""
        from src.score import NONE
        verdict = self._verdict(setup, price=300.0, low=120.0)

        assert verdict.value_ratio == pytest.approx(0.2), "proti doporučené ceně 20 %"
        assert verdict.level == NONE
