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

    def test_two_observations_are_not_enough(self, store):
        """Zralost se meri CASEM, ne poctem pozorovani.

        HistoryOracle vyzaduje aspon min_span_days od prvniho zaznamu. Sloupec,
        ktery po dvou skenech (tedy po pul hodine) tvrdi "zrale", zatimco zdroj
        pravem mlci, je horsi nez zadny - presne tohle vedlo k otazce, proc
        z Ryanairu a Wizz Airu nic nechodi.
        """
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 800.0)

        assert store.stats_sources()[0]["zrale"] == 0

    def test_old_enough_history_counts_as_mature(self, store):
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        stare_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        store.conn.execute("UPDATE price_log SET ts = ?", (stare_ts,))

        assert store.stats_sources()[0]["zrale"] == 1

    def test_maturity_matches_what_the_oracle_requires(self, store):
        """Cislo v prehledu musi znamenat "tohle uz umime ocenit"."""
        from src.oracles.history import HistoryOracle
        from src.sources.base import CATALOG, Offer

        oracle = HistoryOracle(store)
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 1500.0)
        stare_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        store.conn.execute("UPDATE price_log SET ts = ?", (stare_ts,))

        offer = Offer(source="ryanair", kind=CATALOG, uid="PRG-BGY", name="Letenky",
                      price_czk=900.0, url="u", category="flight",
                      merchant="ryanair", credibility=1.0, extra={})

        assert store.stats_sources(oracle.min_span_days)[0]["zrale"] == 1
        assert oracle.value_of(offer) is not None

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


class TestZalohaDatabaze:
    """Cenová historie roste týdny; ztratit ji znamená začít od nuly."""

    def test_backup_is_a_usable_database(self, store, tmp_path):
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        store.commit()

        cil = store.backup(tmp_path / "zalohy")

        import sqlite3
        with sqlite3.connect(cil) as kopie:
            radku = kopie.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        assert radku == 1

    def test_old_backups_are_pruned(self, store, tmp_path):
        cil_dir = tmp_path / "zalohy"
        cil_dir.mkdir()
        for den in range(10):
            (cil_dir / f"deals-2026070{den}-0420.db").write_bytes(b"x")

        store.backup(cil_dir, keep=3)

        assert len(list(cil_dir.glob("deals-*.db"))) == 3

    def test_keeping_zero_does_not_delete_everything(self, store, tmp_path):
        """Nula znamená „needit staré", ne „smaž i tu, co jsem právě udělal"."""
        cil = store.backup(tmp_path / "z", keep=0)
        assert cil.exists()


class TestZdraviZdroju:
    """Selhání zdroje bylo tiché: log napsal „přeskakuji" a tím to skončilo."""

    def test_single_failure_stays_quiet(self, store):
        from src.main import collect

        class Rozbity:
            name = "kinguin"

            def fetch(self):
                raise RuntimeError("403")

        _, hlasit = collect([Rozbity()], store)
        assert hlasit == [], "jeden výpadek sítě není porucha"

    def test_third_failure_in_a_row_reports_once(self, store):
        from src.main import PRAH_SELHANI, collect

        class Rozbity:
            name = "kinguin"

            def fetch(self):
                raise RuntimeError("403")

        hlaseni = [collect([Rozbity()], store)[1] for _ in range(PRAH_SELHANI + 2)]
        neprazdna = [h for h in hlaseni if h]

        assert len(neprazdna) == 1, "zablokovaný zdroj nesmí hlásit každý běh"
        assert "kinguin" in neprazdna[0][0]

    def test_recovery_is_reported_and_resets(self, store):
        from src.main import PRAH_SELHANI, collect

        class Rozbity:
            name = "kinguin"

            def fetch(self):
                raise RuntimeError("403")

        class Funkcni:
            name = "kinguin"

            def fetch(self):
                return []

        for _ in range(PRAH_SELHANI):
            collect([Rozbity()], store)
        _, hlasit = collect([Funkcni()], store)

        assert "zase funguje" in hlasit[0]
        assert store.source_health() == {}, "po zotavení se čítač nuluje"


class TestEtagAzPoParsovani:
    def test_broken_body_does_not_silence_the_feed(self, tmp_path):
        """200 s rozbitým XML nesmí uložit ETag.

        Kdyby se uložil, další běh pošle If-None-Match, dostane 304 a ten feed
        nám zmlkne NATRVALO — dokud se obsah náhodou nezmění.
        """
        from src.sources.travel import TravelSource

        class RozbitaOdpoved:
            status_code = 200
            headers = {"ETag": '"abc"'}
            content = b"<rss><item>chybi zavorka"

        class FakeHttp:
            def get(self, url, **kwargs):
                return RozbitaOdpoved()

        s = Store(tmp_path / "e.db")
        src = TravelSource(FakeHttp(), None, load_config(),
                           {"name": "x", "feeds": [{"url": "http://x/feed"}]}, s)
        try:
            src._load("http://x/feed")
        except Exception:
            pass  # rozbité XML má vyhodit výjimku, o to nejde

        assert s.get_meta("feed:etag:http://x/feed") is None
        s.close()


class TestRozptylPodMedian:
    """Kde vlastně leží dosažitelné minimum.

    Práh „30 % pod vlastním mediánem" je odhad, dokud se nezměří, jak hluboko
    se položky doopravdy dostanou. U katalogového cestování je to podstatné:
    neměří se tam cena letenky, ale nejlevnější nabídka na trase — a ta je
    z podstaty blízko dna, takže se hýbe míň než cena konkrétního termínu.
    """

    def _historie(self, store, body):
        """`body` je [(cena, kolik dní zpátky)] — pořadí od nejstaršího.

        Časy se musí zadat ručně: `price_profile` váží cenu dobou, po kterou
        platila, takže na rozestupech tady záleží.
        """
        for cena, _ in body:
            store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", cena)
        radky = store.conn.execute(
            "SELECT rowid FROM price_log ORDER BY rowid").fetchall()
        assert len(radky) == len(body), "do price_log jdou jen ZMĚNY ceny"
        for radek, (_, zpet) in zip(radky, body):
            ts = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=zpet)).isoformat()
            store.conn.execute("UPDATE price_log SET ts = ? WHERE rowid = ?",
                               (ts, radek["rowid"]))

    def test_ratio_is_minimum_over_own_median(self, store):
        """Tisícovka platila devět dní, pětistovka jeden — běžná cena je tedy
        tisíc a dosažitelné minimum je půlka."""
        self._historie(store, [(1000.0, 10), (500.0, 1)])
        pomery = store.stats_price_spread()

        assert len(pomery["ryanair"]) == 1
        assert pomery["ryanair"][0] == pytest.approx(0.5, abs=0.01)

    def test_immature_item_is_not_counted(self, store):
        """Stejná mez jako u `HistoryOracle` — jinak by to číslo nešlo
        porovnat s tím, co bot doopravdy počítá."""
        store.record_price("ryanair", "PRG-BGY", "Letenky", "u", "flight", 900.0)
        assert store.stats_price_spread() == {}

    def test_flat_price_shows_no_room(self, store):
        """Nehybná cena znamená poměr 1,0 — a to je ta odpověď na otázku,
        proč dopravce mlčí i po měsíci sbírání."""
        self._historie(store, [(900.0, 10)])
        assert store.stats_price_spread()["ryanair"][0] == pytest.approx(1.0)
