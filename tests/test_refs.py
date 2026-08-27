"""Ceník a přepočet délky předplatného.

Právě ten přepočet dělá z Gemini za 65 Kč trhák: 18 měsíců po 490 Kč je 8 820 Kč,
takže poměr vychází na sedm desetin procenta.
"""

import pytest

from src.oracles.refs import ReferenceOracle, parse_months
from src.sources.base import CATALOG, Offer


@pytest.mark.parametrize("name,expected", [
    ("Google Gemini Top-Up > AI Pro > 18 Months", 18),
    ("Google AI Pro: Gemini Advanced - 18-Month Subscription", 18),
    ("ChatGPT Plus 1-Month Subscription ACCOUNT", 1),
    ("Perplexity PRO - 1 Year Subscription Key EU", 12),
    ("Spotify Premium 3 měsíce", 3),
    ("NordVPN 2 Years", 24),
    ("Windows 11 Pro Retail Key", None),
])
def test_parse_months(name, expected):
    assert parse_months(name) == expected


def _offer(name, price_czk=65.0):
    return Offer(source="kinguin", kind=CATALOG, uid="x", name=name,
                 price_czk=price_czk, url="http://x")


class TestReferenceOracle:
    def setup_method(self):
        self.oracle = ReferenceOracle([
            {"match": ["gemini", "ai pro"], "value_czk_per_month": 490},
            {"match": ["windows 11 pro"], "value_czk": 4500},
        ])

    def test_subscription_multiplies_by_months(self):
        value = self.oracle.value_of(_offer("Google Gemini Top-Up > AI Pro > 18 Months"))
        assert value is not None
        assert value.real_value_czk == 490 * 18
        assert value.origin == "references"

    def test_one_off_value(self):
        value = self.oracle.value_of(_offer("Windows 11 Pro Retail Key"))
        assert value.real_value_czk == 4500

    def test_unknown_product_is_not_priced(self):
        assert self.oracle.value_of(_offer("Nějaká úplně neznámá hra")) is None

    def test_missing_duration_is_conservative(self):
        """Bez délky bereme jeden měsíc — radši podstřelit než vyrobit falešný trhák."""
        value = self.oracle.value_of(_offer("Gemini AI Pro Subscription"))
        assert value.real_value_czk == 490
        assert value.confidence < 1.0


class TestRozlisovaciSchopnost:
    """Pravidlo, které pálí vždycky, není signál.

    Ceník obchází práh důvěryhodnosti, takže vadné pravidlo neznamená jednu
    falešnou zprávu, ale desítky — a pohledem se nepozná. Cena v něm může být
    úplně správná ceníková cena výrobce a přesto k ničemu: antivirus se za ni
    nikdy neprodává, takže pravidlo hlásí slevu pořád.
    """

    def _store(self, tmp_path, polozky):
        import datetime as dt

        from src.store import Store

        s = Store(tmp_path / "r.db")
        for uid, (nazev, cena) in enumerate(polozky, start=1):
            s.record_price("kinguin", str(uid), nazev, "u", "SOFTWARE", cena)
        stare = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        s.conn.execute("UPDATE price_log SET ts = ?", (stare,))
        s.commit()
        s.close()
        return s

    def _run(self, tmp_path, polozky, capsys):
        from src.config import load_config
        from src.main import run_check_references

        cfg = load_config()
        cfg.raw["db_path"] = str(tmp_path / "r.db")
        self._store(tmp_path, polozky)
        run_check_references(cfg)
        return capsys.readouterr().out

    def test_rule_that_always_fires_is_reported(self, tmp_path, capsys):
        """ESET na 100 Kč za měsíc dá u dvouleté licence 2 400 Kč, jenže
        běžná cena je 470 Kč. Takové pravidlo projde vždycky."""
        vystup = self._run(tmp_path, [
            ("ESET NOD32 Antivirus (2 Years / 1 PC)", 470.0),
        ], capsys)

        assert "eset" in vystup
        assert "100%" in vystup

    def test_healthy_rule_is_not_listed(self, tmp_path, capsys):
        """Položka, která se běžně prodává blízko ceníku, prahem neprojde —
        a právě proto je to použitelné pravidlo."""
        vystup = self._run(tmp_path, [
            ("Windows 11 Home Retail Key", 3200.0),
        ], capsys)

        assert "windows 11 home" not in vystup
        assert "V pořádku je 1 pravidel" in vystup

    def test_item_without_history_is_skipped(self, tmp_path, capsys):
        """Bez zralé historie není proti čemu ceník porovnat."""
        from src.config import load_config
        from src.main import run_check_references
        from src.store import Store

        cfg = load_config()
        cfg.raw["db_path"] = str(tmp_path / "r.db")
        s = Store(cfg.db_path)
        s.record_price("kinguin", "1", "ESET NOD32 Antivirus (1 Year)", "u",
                       "SOFTWARE", 300.0)
        s.commit()
        s.close()
        run_check_references(cfg)

        assert "chybí zralá cenová historie" in capsys.readouterr().out


class TestRuleFor:
    def test_matches_the_same_rule_as_value_of(self):
        """Diagnostika nesmí porovnávat jinak než ocenění — jinak by ukazovala
        na pravidlo, které se ve skutečnosti neuplatnilo."""
        from src.config import load_config
        from src.oracles.refs import ReferenceOracle
        from src.sources.base import CATALOG, Offer

        oracle = ReferenceOracle(load_config().references)
        offer = Offer(source="kinguin", kind=CATALOG, uid="x",
                      name="ESET NOD32 Antivirus (2 Years / 1 PC)",
                      price_czk=470.0, url="u", category="SOFTWARE",
                      merchant="kinguin", credibility=0.9, extra={})

        pravidlo = oracle.rule_for(offer)
        hodnota = oracle.value_of(offer)

        assert pravidlo is not None and hodnota is not None
        assert " + ".join(pravidlo["match"]) in hodnota.note

    def test_trial_matches_nothing(self):
        """Zkušební verze nemá cenu plného předplatného — dostane ji zadarmo."""
        from src.config import load_config
        from src.oracles.refs import ReferenceOracle
        from src.sources.base import CATALOG, Offer

        oracle = ReferenceOracle(load_config().references)
        offer = Offer(source="kinguin", kind=CATALOG, uid="x",
                      name="Discord Nitro - 3 Months Trial", price_czk=12.0,
                      url="u", category="INGAME_TOPUP", merchant="kinguin",
                      credibility=0.9, extra={})

        assert oracle.rule_for(offer) is None
