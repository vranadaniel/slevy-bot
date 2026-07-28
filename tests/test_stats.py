"""Přehled o stavu bota.

Vzniklo proto, že po přidání tří katalogových zdrojů u cestování a po
zdvojnásobení katalogu Kinguinu nebylo jak zjistit, jestli to celé funguje.
"""

import datetime as dt

import pytest

from src.config import load_config
from src.main import _report_tesne_pod, run_stats
from src.oracles.history import HistoryOracle
from src.score import Scorer
from src.shipping import ShippingPolicy
from src.sources.base import CATALOG, FEED, Offer
from src.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "stats.db")
    yield s
    s.close()


class TestZralostHistorie:
    """`zralé` je hlavní číslo celého přehledu.

    Říká, kolik položek už má porovnání proti dřívější ceně — tedy kolik jich
    `HistoryOracle` vůbec umí ocenit. Dokud je nula, katalogový zdroj mlčí
    právem a není to porucha.
    """

    def test_first_observation_is_not_mature(self, store):
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        radek = store.stats_sources()[0]

        assert radek["source"] == "ryanair"
        assert radek["polozek"] == 1
        assert radek["zrale"] == 0, "první pozorování není historie"

    def test_second_observation_makes_it_mature(self, store):
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 800.0)

        assert store.stats_sources()[0]["zrale"] == 1

    def test_sources_are_reported_separately(self, store):
        store.record_price("ryanair", "PRG-BGY", "A", "u", "flight", 900.0)
        store.record_price("wizzair", "PRG-VIE", "B", "u", "flight", 500.0)
        store.record_price("wizzair", "PRG-OTP", "C", "u", "flight", 600.0)

        podle_zdroje = {r["source"]: r["polozek"] for r in store.stats_sources()}
        assert podle_zdroje == {"ryanair": 1, "wizzair": 2}


class TestOdeslanaUpozorneni:
    def test_only_the_asked_window_counts(self, store):
        store.mark_alerted("kinguin", "a", 100.0, "instant")
        stare = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
        store.conn.execute("UPDATE alerts SET ts = ? WHERE uid = 'a'", (stare,))
        store.mark_alerted("kinguin", "b", 50.0, "digest")

        assert sum(r["pocet"] for r in store.stats_alerts(1)) == 1
        assert sum(r["pocet"] for r in store.stats_alerts(90)) == 2

    def test_price_moves_count_only_changes(self, store):
        """Do `price_log` se zapisují jen ZMĚNY, ne každý běh."""
        for cena in (100.0, 100.0, 100.0, 80.0):
            store.record_price("kinguin", "x", "Věc", "u", "GAME", cena)

        assert store.stats_price_moves(7)["zmen"] == 2


class TestPrazdnaDatabaze:
    def test_stats_on_a_fresh_install_does_not_crash(self, tmp_path, capsys):
        cfg = load_config()
        cfg.raw["db_path"] = str(tmp_path / "nova.db")

        assert run_stats(cfg) == 0
        assert "prázdný" in capsys.readouterr().out


class TestTesnePodPrahem:
    """Jediný způsob, jak poznat, jestli jsou prahy utažené správně."""

    def _scorer(self, tmp_path):
        cfg = load_config()
        s = Store(tmp_path / "p.db")
        history = HistoryOracle(s)
        return Scorer(cfg, s, [history], history, ShippingPolicy(cfg.merchants)), s

    def _flight(self, price_czk, value_czk):
        return Offer(source="travelfree", kind=FEED, uid="t1", name="Letenka",
                     price_czk=price_czk, ref_price_czk=value_czk,
                     url="u", category="flight", merchant="travelfree",
                     credibility=0.85, extra={"airport": "PRG"})

    def test_near_miss_is_listed_with_the_gap(self, tmp_path, capsys):
        from src.oracles.declared import DeclaredOracle

        scorer, s = self._scorer(tmp_path)
        scorer.oracles.append(DeclaredOracle())
        # Práh souhrnu u letenek je 0,70; tahle je na 0,72, tedy o 3 % vedle.
        verdicts = scorer.prescore([self._flight(7_200.0, 10_000.0)])
        _report_tesne_pod(verdicts, scorer)
        s.close()

        vystup = capsys.readouterr().out
        assert "TĚSNĚ POD PRAHEM" in vystup
        assert "3 %" in vystup

    def test_unvalued_items_are_not_listed(self, tmp_path, capsys):
        """Neoceněná položka práh neminula — nikdo jí neurčil hodnotu.
        Je to jiná diagnóza a míchat je dohromady by přehled znehodnotilo."""
        scorer, s = self._scorer(tmp_path)
        verdicts = scorer.prescore([self._flight(7_200.0, None)])
        _report_tesne_pod(verdicts, scorer)
        s.close()

        assert "Nic se prahu neblíží" in capsys.readouterr().out

    def test_items_that_passed_are_not_listed(self, tmp_path, capsys):
        from src.oracles.declared import DeclaredOracle

        scorer, s = self._scorer(tmp_path)
        scorer.oracles.append(DeclaredOracle())
        verdicts = scorer.prescore([self._flight(3_000.0, 10_000.0)])
        _report_tesne_pod(verdicts, scorer)
        s.close()

        assert "Nic se prahu neblíží" in capsys.readouterr().out
