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
