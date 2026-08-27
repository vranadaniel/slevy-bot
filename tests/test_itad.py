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

    def __init__(self, lookup=None, lows=None, info=None) -> None:
        self.lookup = lookup or {}
        self.lows = lows or []
        self.info = info or {}
        self.calls: list[str] = []

    def post_json(self, url, payload, headers=None, timeout_s=None):
        self.calls.append(url)
        if "lookup" in url:
            return {title: self.lookup.get(title) for title in payload}
        return [row for row in self.lows if row["id"] in payload]

    def get_json(self, url, params=None, headers=None, **kwargs):
        self.calls.append(url)
        return self.info.get((params or {}).get("id"), {})


def _info_row(score=90, count=50_000, rank=120, released="2023-05-01"):
    return {
        "reviews": [{"source": "Steam", "score": score, "count": count}],
        "stats": {"rank": rank, "waitlisted": 10, "collected": 20},
        "releaseDate": released,
    }


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


class TestPopularity:
    """Souhrn se jinak zaplní starými hrami za pár korun — čím míň lidí hru
    chce, tím hlouběji jde cena."""

    def _oracle(self, setup, info):
        cfg, store = setup
        http = FakeHttp(lookup={"Gothic 1 Remake": "uuid-1"},
                        lows=[_low_row("uuid-1")], info={"uuid-1": info})
        return http, ItadOracle(http, store, FakeFx(), cfg)

    def test_fills_popularity_and_review_details(self, setup):
        http, oracle = self._oracle(setup, _info_row(score=92, count=742_000))
        offer = _game()
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])

        assert offer.extra["popularity"] > 0.9
        assert offer.extra["reviews_score"] == 92
        assert offer.extra["reviews_count"] == 742_000
        assert offer.extra["released"] == "2023-05-01"

    def test_obscure_game_scores_low(self, setup):
        _, oracle = self._oracle(setup, _info_row(score=55, count=18))
        offer = _game()
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])

        assert offer.extra["popularity"] < 0.5

    def test_game_without_reviews_falls_back_to_bestseller_rank(self, setup):
        """Battlefield ani Call of Duty nejsou na Steamu, a přitom je to
        přesně to, co má projít."""
        _, oracle = self._oracle(setup, _info_row(score=None, count=None))
        offer = _game()
        offer.credibility = 0.98
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])

        assert offer.extra["popularity"] == pytest.approx(0.98)

    def test_second_run_uses_cache(self, setup):
        http, oracle = self._oracle(setup, _info_row())
        offer = _game()
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])
        calls = len(http.calls)

        oracle.enrich_popularity([offer])
        assert len(http.calls) == calls

    def test_api_failure_does_not_raise(self, setup):
        cfg, store = setup

        class Broken(FakeHttp):
            def get_json(self, *a, **kw):
                raise RuntimeError("ITAD spadlo")

        http = Broken(lookup={"Gothic 1 Remake": "uuid-1"}, lows=[_low_row("uuid-1")])
        oracle = ItadOracle(http, store, FakeFx(), cfg)
        offer = _game()
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])       # nesmí vyhodit výjimku
        assert "popularity" not in offer.extra

    def test_non_game_is_not_asked_about(self, setup):
        cfg, store = setup
        http = FakeHttp()
        oracle = ItadOracle(http, store, FakeFx(), cfg)
        offer = Offer(source="kinguin", kind=CATALOG, uid="s1",
                      name="Google Gemini AI Pro 18 Months", price_czk=65.0,
                      url="http://x", extra={"product_type": "INGAME_TOPUP"})
        oracle.prepare([offer])
        oracle.enrich_popularity([offer])
        assert http.calls == []


class TestDropUnpopular:
    def test_known_low_popularity_is_dropped(self, setup):
        from src.main import drop_unpopular
        from src.score import DIGEST, Verdict

        junk = Verdict(offer=_game(uid="a"), level=DIGEST)
        junk.offer.extra["popularity"] = 0.2
        hit = Verdict(offer=_game(uid="b"), level=DIGEST)
        hit.offer.extra["popularity"] = 0.8

        kept = drop_unpopular([junk, hit], 0.5)
        assert [v.offer.uid for v in kept] == ["b"]

    def test_game_with_unknown_popularity_is_dropped(self, setup):
        """Dřív se neznámé pouštěly. Po rozšíření skenu na celých 10 000
        produktů se jimi sekce zaplnila — brak má nejextrémnější poměr ceny,
        takže se v žebříčku dostal nahoru. Neznámá popularita znamená buď že
        hru ITAD nezná, nebo že došel strop dotazů; obojí je slabší kandidát
        než hra, o které víme, že ji lidi chtějí."""
        from src.main import drop_unpopular
        from src.score import DIGEST, Verdict

        unknown = Verdict(offer=_game(uid="c"), level=DIGEST)
        assert drop_unpopular([unknown], 0.5) == []

    def test_unknown_can_still_be_allowed_from_config(self, setup):
        from src.main import drop_unpopular
        from src.score import DIGEST, Verdict

        unknown = Verdict(offer=_game(uid="c"), level=DIGEST)
        assert drop_unpopular([unknown], 0.5, require_known=False) == [unknown]

    def test_non_games_are_never_touched(self, setup):
        """U předplatného a cestování se popularita nezjišťuje vůbec —
        stejný filtr by vymazal celý zbytek souhrnu."""
        from src.main import drop_unpopular
        from src.score import DIGEST, Verdict
        from src.sources.base import FEED, Offer

        letenka = Verdict(offer=Offer(
            source="zaletsi", kind=FEED, uid="t", name="Letenky z Prahy do Ria",
            price_czk=9000.0, url="u", category="flight", merchant="zaletsi",
            credibility=0.85, extra={}), level=DIGEST)
        predplatne = Verdict(offer=Offer(
            source="kinguin", kind="catalog", uid="s", name="Gemini AI Pro",
            price_czk=65.0, url="u", category="INGAME_TOPUP", merchant="kinguin",
            credibility=0.9, extra={"product_type": "INGAME_TOPUP"}), level=DIGEST)

        kept = drop_unpopular([letenka, predplatne], 0.5)
        assert [v.offer.uid for v in kept] == ["t", "s"]

    def test_zero_threshold_disables_the_filter(self, setup):
        from src.main import drop_unpopular
        from src.score import DIGEST, Verdict

        junk = Verdict(offer=_game(uid="a"), level=DIGEST)
        junk.offer.extra["popularity"] = 0.01
        assert drop_unpopular([junk], 0.0) == [junk]


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

    def test_itad_alone_never_triggers_instant(self, setup):
        """Klíčové omezení, změřené na živých datech.

        Kinguin prodává regionální klíče pod cenami, na které oficiální obchody
        nikdy nejdou — zhruba třetina her je levnější než jejich historické
        minimum. Jako spouštěč okamžité zprávy je tedy ITAD k ničemu: dával
        153 "extrémů" v jediném běhu. Trhák u her pozná až vlastní historie.
        """
        from src.score import DIGEST
        verdict = self._verdict(setup, price=5.0, low=40.0)

        assert verdict.value_ratio < 0.01, "proti doporučené ceně vypadá jako trhák"
        assert verdict.level == DIGEST, "a přesto jen do souhrnu"

    def test_below_official_low_reaches_digest(self, setup):
        from src.score import DIGEST
        verdict = self._verdict(setup, price=38.0, low=40.0)

        assert verdict.level == DIGEST
        assert any("historickým minimem" in r for r in verdict.reasons)

    def test_ordinary_grey_market_price_is_silent(self, setup):
        """300 Kč při oficiálním minimu 120 Kč je běžná cena, ne nález."""
        from src.score import NONE
        verdict = self._verdict(setup, price=300.0, low=120.0)

        assert verdict.value_ratio == pytest.approx(0.2), "proti doporučené ceně 20 %"
        assert verdict.level == NONE


class TestDropCheapGames:
    """Poměr systémově zvýhodňuje drobnosti.

    Hra za 3 Kč z původních 100 vyjde na 3 %, AAA za 200 Kč z patnácti stovek
    na 13 %. Souhrn se tím plnil věcmi za jednotky korun, o které nikdo
    nestojí, a velký titul v obrovské slevě mezi nimi zapadl.
    """

    def _verdict(self, hodnota, cena=200.0, uid="g", level=None, offer=None):
        from src.oracles.base import Value
        from src.score import DIGEST, Verdict

        v = Verdict(offer=offer or _game(uid=uid, price=cena),
                    level=level or DIGEST)
        v.value = Value(real_value_czk=hodnota, origin="itad")
        v.value_ratio = cena / hodnota
        return v

    def test_junk_loses_to_a_big_title(self):
        from src.main import drop_cheap_games

        drobnost = self._verdict(100.0, cena=3.0, uid="drobnost")
        aaa = self._verdict(1500.0, cena=200.0, uid="aaa")

        assert [v.offer.uid for v in drop_cheap_games([drobnost, aaa], 600.0)] \
            == ["aaa"], "rozhoduje ceníková cena, ne poměr"

    def test_popularity_would_not_have_caught_it(self):
        """Povedená indie hra má hodnocení jako AAA — popularita měří, jestli
        hru někdo hrál, ne jestli je to velký titul."""
        from src.main import drop_cheap_games, drop_unpopular

        indie = self._verdict(150.0, cena=4.0, uid="indie")
        indie.offer.extra["popularity"] = 0.9

        assert drop_unpopular([indie], 0.6) == [indie], "filtrem popularity projde"
        assert drop_cheap_games([indie], 600.0) == []

    def test_instant_is_filtered_too(self):
        """Vlastní cenová historie umí u her spustit i okamžité upozornění,
        takže samotný souhrn by nestačil."""
        from src.main import drop_cheap_games
        from src.score import INSTANT

        drobnost = self._verdict(120.0, cena=2.0, uid="d", level=INSTANT)
        assert drop_cheap_games([drobnost], 600.0) == []

    def test_non_games_are_never_touched(self):
        """Gemini AI Pro za 65 Kč nesmí padnout na prahu určeném hrám —
        a stejně tak letenka, kde je hodnota v úplně jiném řádu."""
        from src.main import drop_cheap_games
        from src.sources.base import FEED, Offer

        predplatne = self._verdict(2000.0, cena=65.0, offer=Offer(
            source="kinguin", kind=CATALOG, uid="s", name="Gemini AI Pro",
            price_czk=65.0, url="u", category="INGAME_TOPUP",
            merchant="kinguin", credibility=0.9,
            extra={"product_type": "INGAME_TOPUP"}))
        # Levná letenka: hodnota pod prahem, ale s hrami nemá nic společného.
        letenka = self._verdict(500.0, cena=200.0, offer=Offer(
            source="ryanair", kind=CATALOG, uid="t", name="Letenky do Neapole",
            price_czk=200.0, url="u", category="flight", merchant="ryanair",
            credibility=1.0, extra={}))

        kept = drop_cheap_games([predplatne, letenka], 600.0)
        assert [v.offer.uid for v in kept] == ["s", "t"]

    def test_unvalued_item_is_kept(self):
        """Neoceněná položka práh neminula — nikdo jí neurčil hodnotu."""
        from src.main import drop_cheap_games
        from src.score import NONE, Verdict

        neocenena = Verdict(offer=_game(uid="n"), level=NONE)
        assert drop_cheap_games([neocenena], 600.0) == [neocenena]

    def test_zero_disables_the_filter(self):
        from src.main import drop_cheap_games

        drobnost = self._verdict(100.0, cena=3.0)
        assert drop_cheap_games([drobnost], 0.0) == [drobnost]

    def test_live_config_keeps_the_reference_case(self):
        """Ostrá konfigurace nesmí umlčet Gemini AI Pro za 65 Kč."""
        from src.config import load_config
        from src.main import drop_cheap_games
        from src.sources.base import Offer

        prah = float(load_config().get("games.min_value_czk", 0))
        gemini = self._verdict(2000.0, cena=65.0, offer=Offer(
            source="kinguin", kind=CATALOG, uid="s", name="Gemini AI Pro",
            price_czk=65.0, url="u", category="INGAME_TOPUP",
            merchant="kinguin", credibility=0.9,
            extra={"product_type": "INGAME_TOPUP"}))

        assert prah > 0, "filtr má být v ostré konfiguraci zapnutý"
        assert drop_cheap_games([gemini], prah) == [gemini]
