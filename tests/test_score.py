"""Akceptační testy scoringu.

Dva fixtury drží obě strany problému a jsou obě z živých dat:

* **Gemini AI Pro na 18 měsíců za 65 Kč** — přesně ta nabídka, kvůli které projekt
  vznikl. Musí projít jako okamžité upozornění.
* **Tanks Battle Steam CD Key** — šunta, která si sama deklaruje cenu 97,50 €
  a 99% slevu. Nesmí projít vůbec. Kdyby prošla, bot by posílal samý brak.

Testy schválně používají OSTROU konfiguraci z repozitáře, ne vymyšlenou — ověřují
tedy i to, že prahy a ceník v `config.yaml` a `references.yaml` dávají smysl.
"""

import pytest

from src.config import load_config
from src.oracles.declared import DeclaredOracle
from src.oracles.history import HistoryOracle
from src.oracles.refs import ReferenceOracle
from src.score import DIGEST, INSTANT, NONE, Scorer
from src.shipping import ShippingPolicy
from src.sources.base import CATALOG, FEED, Offer
from src.store import Store


@pytest.fixture
def scorer(tmp_path):
    cfg = load_config()
    store = Store(tmp_path / "test.db")
    history = HistoryOracle(store)
    oracles = [history, ReferenceOracle(cfg.references), DeclaredOracle()]
    yield Scorer(cfg, store, oracles, history, ShippingPolicy(cfg.merchants))
    store.close()


def _kinguin(name, price_czk, credibility=0.99, ref_czk=None, **extra):
    return Offer(
        source="kinguin", kind=CATALOG, uid=name[:12], name=name,
        price_czk=price_czk, url="https://www.kinguin.net/x",
        credibility=credibility, ref_price_czk=ref_czk,
        category="INGAME_TOPUP", merchant="kinguin", extra=extra,
    )


class TestGeminiMustAlert:
    """Referenční případ celého projektu."""

    def test_gemini_18_months_for_65_czk_is_instant(self, scorer):
        offer = _kinguin("Google Gemini Top-Up > AI Pro > 18 Months", 65.0, extra={})
        verdict = scorer.prescore([offer])[0]

        assert verdict.value is not None, "ceník musí Gemini ocenit"
        assert verdict.value.real_value_czk == 490 * 18
        assert verdict.value_ratio < 0.01, "má to být pod procentem reálné ceny"
        assert verdict.level == INSTANT

    def test_gemini_still_instant_at_current_price(self, scorer):
        """I dnešních 149 Kč je pořád 1,7 % ceny, takže pořád trhák."""
        offer = _kinguin("Google Gemini Top-Up > AI Pro > 18 Months", 149.0)
        assert scorer.prescore([offer])[0].level == INSTANT

    def test_gemini_alerts_even_when_unpopular(self, scorer):
        """Ruční ceník je důvěryhodné ocenění, takže žebříček prodejnosti nerozhoduje.

        Dvanáctiměsíční varianta je hluboko v katalogu (credibility 0.19), ale za
        41 Kč je to pořád 0,7 % ceny — a to je zpráva, kterou chceme hned.
        """
        offer = _kinguin("Google AI Pro: Gemini Advanced - 12-Month Subscription",
                         41.0, credibility=0.19)
        assert scorer.prescore([offer])[0].level == INSTANT


class TestJunkMustNotAlert:
    """Obrana proti vymyšleným původním cenám."""

    def test_tanks_battle_does_not_pass(self, scorer):
        """Deklarovaných 99 % slevy nesmí samo o sobě nic spustit."""
        offer = _kinguin(
            "Tanks Battle Steam CD Key", price_czk=7.0,
            credibility=0.05,           # hluboko v žebříčku prodejnosti
            ref_czk=2400.0,             # prodejcem vymyšlená MSRP 97,50 €
            claimed_discount=99,
        )
        verdict = scorer.prescore([offer])[0]

        assert verdict.value is None, "market od prodejce nesmí sloužit jako hodnota"
        assert verdict.level == NONE

    def test_declared_oracle_ignores_catalog(self):
        """Deklarovaná cena platí jen u feedů, kde ji píše komunita."""
        offer = _kinguin("Cokoliv", 7.0, ref_czk=2400.0)
        assert DeclaredOracle().value_of(offer) is None

    def test_unknown_cheap_item_is_silent(self, scorer):
        offer = _kinguin("Naprosto neznámá hra XYZ", 12.0, credibility=0.9)
        assert scorer.prescore([offer])[0].level == NONE


class TestFeedScoring:
    def _feed_offer(self, **kwargs):
        defaults = dict(
            source="mydealz", kind=FEED, uid="g1", name="Nějaký deal",
            price_czk=100.0, url="https://www.mydealz.de/deals/x",
            credibility=0.8, category="Elektronik", merchant="Amazon",
            extra={"temperature": 400},
        )
        defaults.update(kwargs)
        return Offer(**defaults)

    def test_declared_price_drives_ratio(self, scorer):
        offer = self._feed_offer(price_czk=100.0, ref_price_czk=5000.0)
        verdict = scorer.prescore([offer])[0]
        assert verdict.value.origin == "feed"
        assert verdict.value_ratio == pytest.approx(0.02)
        assert verdict.level == INSTANT

    def test_merchant_that_does_not_ship_is_dropped(self, scorer):
        offer = self._feed_offer(merchant="MediaMarkt", ref_price_czk=5000.0)
        verdict = scorer.prescore([offer])[0]
        assert verdict.ships_to_cz is False
        assert verdict.level == NONE

    def test_unknown_merchant_falls_back_to_digest(self, scorer):
        """Mlčet o možném trháku je horší než poslat ho s poznámkou."""
        offer = self._feed_offer(merchant="Nějaký Neznámý Shop", ref_price_czk=5000.0)
        verdict = scorer.prescore([offer])[0]
        assert verdict.ships_to_cz is None
        assert verdict.level == DIGEST


class TestTravelScoring:
    """Cestování má vlastní prahy. Bez nich by mlčelo, ať přidáme zdrojů kolik chceme."""

    def _flight(self, price_czk, value_czk, category="flight", credibility=0.85):
        return Offer(
            source="travelfree", kind=FEED, uid="t1",
            name=f"Flights from Prague to Nepal for €{price_czk / 25:.0f}",
            price_czk=price_czk, ref_price_czk=value_czk,
            url="https://www.travelfree.info/x", category=category,
            merchant="travelfree", credibility=credibility,
            extra={"airport": "PRG"},
        )

    def test_half_price_flight_reaches_instant(self, scorer):
        """Na digitální klíče kalibrovaný práh 5 % by tuhle letenku umlčel."""
        verdict = scorer.prescore([self._flight(10_000.0, 25_000.0)])[0]

        assert verdict.value_ratio == pytest.approx(0.4)
        assert verdict.level == INSTANT

    def test_mild_discount_only_reaches_digest(self, scorer):
        verdict = scorer.prescore([self._flight(16_000.0, 25_000.0)])[0]

        assert verdict.value_ratio == pytest.approx(0.64)
        assert verdict.level == DIGEST

    def test_ordinary_fare_stays_silent(self, scorer):
        verdict = scorer.prescore([self._flight(20_000.0, 25_000.0)])[0]
        assert verdict.level == NONE

    def test_hotel_uses_the_same_thresholds(self, scorer):
        verdict = scorer.prescore([self._flight(10_000.0, 25_000.0, category="hotel")])[0]
        assert verdict.level == INSTANT

    def test_kinguin_thresholds_are_untouched(self, scorer):
        """Práh pro cestování nesmí povolit klíče — 40 % u hry není nález."""
        offer = _kinguin("Nějaká hra", 10_000.0, ref_czk=25_000.0)
        assert scorer.prescore([offer])[0].level == NONE


class TestHistorickeMinimum:
    """Proč bot první den hlásil obyčejné letenky jako trháky.

    `record_price` v `main.py` běží dřív než scoring, takže `min_ever` už
    aktuální cenu obsahuje. Historické minimum se proto musí počítat proti
    ceně PŘED zápisem — jinak platí pro každou položku hned napoprvé.
    """

    def _ryanair(self, price_czk):
        return Offer(
            source="ryanair", kind=CATALOG, uid="PRG-BRS",
            name="Letenky z Prahy do Bristol", price_czk=price_czk,
            url="https://www.ryanair.com/x", category="flight",
            merchant="ryanair", credibility=1.0, extra={"airport": "PRG"},
        )

    def _observe(self, scorer, price_czk):
        offer = self._ryanair(price_czk)
        scorer.store.record_price(offer.source, offer.uid, offer.name,
                                  offer.url, offer.category, offer.price_czk)
        return scorer.prescore([offer])[0]

    def test_first_observation_is_not_a_low(self, scorer):
        assert self._observe(scorer, 918.0).all_time_low is False

    def test_unchanged_price_is_not_a_low(self, scorer):
        """Nehybná cena by se jinak hlásila jako minimum donekonečna."""
        self._observe(scorer, 918.0)
        assert self._observe(scorer, 918.0).all_time_low is False

    def test_real_drop_is_a_low(self, scorer):
        self._observe(scorer, 918.0)
        assert self._observe(scorer, 640.0).all_time_low is True

    def test_catalog_flight_never_reaches_the_ai_judge(self, scorer):
        """Ceník dopravce je z podstaty levnější než „běžná cena", kterou by
        trase vymyslela AI. Tenhle kruh smí rozseknout jen vlastní historie."""
        self._observe(scorer, 918.0)
        verdict = self._observe(scorer, 640.0)

        assert verdict.value is None, "první dny ještě není z čeho ocenit"
        assert scorer.ai_candidates([verdict], limit=25) == []
        assert verdict.level == NONE

    def test_history_still_alerts_once_it_exists(self, scorer):
        """Pojistka nesmí Ryanair umlčet natrvalo — po nasbírání historie
        má propad projít, a to na základě vlastního měření."""
        import datetime as dt

        offer = self._ryanair(1800.0)
        scorer.store.record_price(offer.source, offer.uid, offer.name,
                                  offer.url, offer.category, offer.price_czk)
        staré = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
        scorer.store.conn.execute(
            "UPDATE price_log SET ts = ? WHERE uid = 'PRG-BRS'", (staré,))

        verdict = self._observe(scorer, 640.0)

        assert verdict.value.origin == "history"
        assert verdict.level == INSTANT


class TestAiCandidates:
    def test_only_promising_items_reach_ai(self, scorer):
        offers = [
            _kinguin("Populární věc s obří slevou", 50.0,
                     credibility=0.9, claimed_discount=95),
            _kinguin("Neznámá šunta", 50.0, credibility=0.05, claimed_discount=99),
            _kinguin("Populární bez slevy", 50.0, credibility=0.9, claimed_discount=10),
        ]
        verdicts = scorer.prescore(offers)
        names = [o.name for o in scorer.ai_candidates(verdicts, limit=25)]

        assert names == ["Populární věc s obří slevou"]

    def test_already_valued_items_do_not_cost_ai_tokens(self, scorer):
        offer = _kinguin("Google Gemini Top-Up > AI Pro > 18 Months", 65.0,
                         claimed_discount=99)
        verdicts = scorer.prescore([offer])
        assert scorer.ai_candidates(verdicts, limit=25) == []

    def test_curated_travel_reaches_ai(self, scorer):
        """Letenku neocení žádný levný oracle. Bez téhle cesty by se
        cestování nikdy nedostalo ani k ocenění, natož do zprávy."""
        offer = Offer(
            source="travelfree", kind=FEED, uid="t1",
            name="Flights from Prague to Nepal for €426", price_czk=10_650.0,
            url="https://www.travelfree.info/x", category="flight",
            merchant="travelfree", credibility=0.85, extra={"airport": "PRG"},
        )
        verdicts = scorer.prescore([offer])

        assert verdicts[0].value is None, "levné oracles letenku ocenit neumí"
        assert scorer.ai_candidates(verdicts, limit=25) == [offer]

    def test_unvalued_travel_is_kept_for_the_next_run(self, scorer):
        """Kdyby soudce nedojel a položka se zapsala do `seen`, umlčelo by ji
        to natrvalo — a to je přesně ta nabídka, kvůli které bot existuje."""
        from src.main import _retry_later

        offer = Offer(
            source="travelfree", kind=FEED, uid="t1",
            name="Flights from Prague to Nepal for €426", price_czk=10_650.0,
            url="https://www.travelfree.info/x", category="flight",
            merchant="travelfree", credibility=0.85, extra={"airport": "PRG"},
        )
        verdict = scorer.prescore([offer])[0]
        assert _retry_later(scorer, verdict) is True

    def test_valued_item_is_not_retried(self, scorer):
        from src.main import _retry_later

        offer = Offer(
            source="travelfree", kind=FEED, uid="t2",
            name="Flights from Prague to Nepal for €426", price_czk=10_650.0,
            ref_price_czk=25_000.0, url="https://www.travelfree.info/x",
            category="flight", merchant="travelfree", credibility=0.85,
        )
        verdict = scorer.prescore([offer])[0]
        assert _retry_later(scorer, verdict) is False

    def test_lukewarm_feed_item_does_not_cost_ai_tokens(self, scorer):
        offer = Offer(
            source="mydealz", kind=FEED, uid="m1", name="Vlažný deal",
            price_czk=100.0, url="https://www.mydealz.de/deals/x",
            category="Elektronik", merchant="Amazon",
            credibility=0.3, extra={"temperature": 150},
        )
        verdicts = scorer.prescore([offer])
        assert scorer.ai_candidates(verdicts, limit=25) == []
