"""Hlídání konkrétního záměru.

Opačný směr než zbytek bota: ten čeká, co propadne prahem, tohle hlídá to,
co si člověk zadal. Testy drží dvě věci, na kterých to stojí — že se zadání
z Telegramu přečte správně, a že se táž letenka nehlásí pořád dokola.
"""

import datetime as dt

import pytest

from src.config import load_config
from src.store import Store
from src.watch import (ChybaZadani, Watch, WatchEngine, _vyhovuje, parse_watch)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "w.db")
    yield s
    s.close()


def _watch(**zmeny):
    zaklad = dict(id=1, origin="PRG", destination="BCN",
                  od=dt.date(2099, 9, 1), do=dt.date(2099, 10, 31),
                  nights_min=9, nights_max=9)
    zaklad.update(zmeny)
    return Watch(**zaklad)


class TestZadani:
    """Píše se to z mobilu, takže syntaxe musí být krátká a odpouštět."""

    def test_minimal_form(self):
        v = parse_watch("/hlidat BCN 2099-09-01 2099-10-31 9")

        assert v["destination"] == "BCN"
        assert (v["od"], v["do"]) == ("2099-09-01", "2099-10-31")
        assert v["nights_min"] == v["nights_max"] == 9
        assert v["origin"] == "PRG"

    def test_full_form(self):
        v = parse_watch("/hlidat bcn 1.9.2099 31.10.2099 7-10 "
                        "odkud=VIE tam=pá@17-23 zpet=ne@11-18")

        assert (v["origin"], v["destination"]) == ("VIE", "BCN")
        assert (v["nights_min"], v["nights_max"]) == (7, 10)
        assert (v["out_day"], v["out_after_h"], v["out_before_h"]) == (4, 17, 23)
        assert (v["back_day"], v["back_after_h"], v["back_before_h"]) == (6, 11, 18)

    def test_second_date_counts_from_the_first(self):
        """„15.8. 15.10." zadané koncem srpna nesmí znamenat srpen příštího
        roku a říjen letošního, tedy konec dřív než začátek."""
        v = parse_watch("/hlidat BCN 1.12. 10.1. 5")

        assert v["od"] < v["do"]
        assert v["do"].endswith("-01-10")

    def test_day_without_time_and_time_without_day(self):
        assert parse_watch("/hlidat BCN 1.9.2099 1.10.2099 3 tam=pá")["out_day"] == 4
        assert parse_watch("/hlidat BCN 1.9.2099 1.10.2099 3 "
                           "zpet=@6-12")["back_after_h"] == 6

    def test_diacritics_are_optional(self):
        """Z mobilu se diakritika píše špatně a `/hlidat` musí fungovat
        stejně jako `/hlídat`."""
        v = parse_watch("/hlídat BCN 1.9.2099 1.10.2099 3 tam=ctvrtek")
        assert v["out_day"] == 3

    @pytest.mark.parametrize("text,cast_hlasky", [
        ("/hlidat BCN 1.9.2099 1.10.2099", "Chybí"),
        ("/hlidat XX 1.9.2099 1.10.2099 9", "kód letiště"),
        ("/hlidat BCN 1.9.2099 1.10.2099 devět", "nocí"),
        ("/hlidat BCN 1.9.2099 1.10.2099 9 tam=půlnoc", "Dni"),
        ("/hlidat BCN 1.10.2099 1.9.2099 9", "dřív"),
    ])
    def test_errors_explain_themselves(self, text, cast_hlasky):
        """Hláška jde rovnou uživateli do Telegramu, takže musí říct co opravit."""
        with pytest.raises(ChybaZadani) as chyba:
            parse_watch(text)
        assert cast_hlasky in str(chyba.value)


class TestVyberDnu:
    """Filtrovat podle letového řádu je celý trik.

    Na trase bývá jeden let denně, takže „pátek večer" nejde vybrat mezi lety
    — jde vybrat pátek, na který ten večerní let padne.
    """

    class FakeHttp:
        """Řád: v září 2099 se letí každý den, časy se střídají."""

        def __init__(self, casy_podle_dne=None):
            self.casy = casy_podle_dne or {}
            self.dotazy = []

        def get_json(self, url, params=None, **kw):
            self.dotazy.append(params or url)
            if "timtbl" not in url:
                return {"fares": []}
            mesic = int(url.rstrip("/").split("/")[-1])
            dny = []
            for den in range(1, 31):
                cas = self.casy.get(den, "08:00")
                dny.append({"day": den, "flights": [{"departureTime": cas}]})
            return {"days": dny} if mesic == 9 else {"days": []}

    def _engine(self, http):
        return WatchEngine(http, delay_s=0, max_queries=3)

    def test_only_days_with_a_matching_flight(self):
        # 4. 9. 2099 je pátek; dáme mu večerní let, ostatním ranní.
        http = self.FakeHttp({4: "20:15", 11: "06:00", 18: "21:30"})
        watch = _watch(od=dt.date(2099, 9, 1), do=dt.date(2099, 9, 30),
                       out_day=4, out_after_h=17)

        dny = self._engine(http).candidate_days(watch)

        assert [d.day for d in dny] == [4, 18], "pátek s ranním letem vypadne"

    def test_no_day_limit_keeps_every_flying_day(self):
        http = self.FakeHttp()
        watch = _watch(od=dt.date(2099, 9, 1), do=dt.date(2099, 9, 5))

        assert len(self._engine(http).candidate_days(watch)) == 5

    def test_timetable_is_asked_once_per_month(self):
        """Řád je jeden požadavek na měsíc a cachuje se — jinak by široké
        okno znamenalo dotaz na každý den."""
        http = self.FakeHttp()
        engine = self._engine(http)
        watch = _watch(od=dt.date(2099, 9, 1), do=dt.date(2099, 9, 30))

        engine.candidate_days(watch)
        engine.candidate_days(watch)

        assert len(http.dotazy) == 1


class TestOvereniKombinace:
    """Řád říká, že ten den let existuje. Jestli se konkrétní kombinace
    trefila do časů, se musí ověřit na tom, co vrátilo API."""

    def _trip(self, out, back):
        from src.watch import Trip

        return Trip(price_czk=1000.0, out_dep=out, out_arr=out,
                    back_dep=back, back_arr=back, url="u")

    def test_time_window_needs_both_bounds(self):
        """„Neděle do 15:00" splní i let v 5:45, jenže ten tě o víkend
        připraví. Smysl zadání je opačný."""
        watch = _watch(back_day=6, back_before_h=15)
        rano = self._trip(dt.datetime(2099, 9, 4, 20, 0),
                          dt.datetime(2099, 9, 13, 5, 45))

        assert _vyhovuje(watch, rano) is True
        assert _vyhovuje(_watch(back_day=6, back_after_h=11,
                                back_before_h=18), rano) is False

    def test_nights_are_checked_too(self):
        watch = _watch(nights_min=9, nights_max=9)
        kratky = self._trip(dt.datetime(2099, 9, 4, 20, 0),
                            dt.datetime(2099, 9, 8, 12, 0))

        assert _vyhovuje(watch, kratky) is False


class TestUlozeni:
    def test_watch_survives_a_round_trip(self, store):
        cislo = store.watch_add(parse_watch(
            "/hlidat BCN 1.9.2099 31.10.2099 9 tam=pá@17-23"))
        radek = store.watch_list()[0]

        assert cislo == radek["id"]
        assert (radek["destination"], radek["out_day"], radek["out_after_h"]) \
            == ("BCN", 4, 17)

    def test_best_price_is_remembered(self, store):
        cislo = store.watch_add(parse_watch("/hlidat BCN 1.9.2099 31.10.2099 9"))
        store.watch_save_best(cislo, 1610.0, "klic")

        assert store.watch_list()[0]["best_czk"] == 1610.0

    def test_delete_reports_whether_it_existed(self, store):
        cislo = store.watch_add(parse_watch("/hlidat BCN 1.9.2099 31.10.2099 9"))

        assert store.watch_delete(cislo) is True
        assert store.watch_delete(cislo) is False


class TestPrehlaseni:
    """Přehlašuje se JEN zlepšení — jinak by hlídání psalo tutéž letenku
    každou hodinu."""

    class Odesilatel:
        def __init__(self):
            self.zpravy = []

        def send(self, text):
            self.zpravy.append(text)
            return True

    def _run(self, cfg, cena, odesilatel, monkeypatch):
        import src.main as main
        from src.watch import Trip, Vysledek

        trip = Trip(price_czk=cena,
                    out_dep=dt.datetime(2099, 9, 4, 20, 15),
                    out_arr=dt.datetime(2099, 9, 4, 22, 40),
                    back_dep=dt.datetime(2099, 9, 13, 12, 0),
                    back_arr=dt.datetime(2099, 9, 13, 14, 30), url="u")
        monkeypatch.setattr(main.WatchEngine, "best_trip",
                            lambda self, watch: Vysledek(vyhovujici=trip))
        monkeypatch.setattr(main, "build_http", lambda cfg: None)
        monkeypatch.setattr(main, "Telegram", lambda *a, **kw: odesilatel)
        monkeypatch.setattr(main, "_zpracuj_prikazy", lambda *a: 0)
        cfg.raw["watch"]["min_interval_min"] = 0
        return main.run_watch(cfg)

    @pytest.fixture
    def cfg(self, tmp_path):
        cfg = load_config()
        cfg.raw["db_path"] = str(tmp_path / "w.db")
        cfg.telegram_token, cfg.telegram_chat = "t", "1"
        s = Store(cfg.db_path)
        s.watch_add(parse_watch("/hlidat BCN 1.9.2099 31.10.2099 9"))
        s.close()
        return cfg

    def test_first_find_is_reported(self, cfg, monkeypatch):
        posta = self.Odesilatel()
        self._run(cfg, 1610.0, posta, monkeypatch)

        assert len(posta.zpravy) == 1
        assert "1\xa0610\xa0Kč" in posta.zpravy[0]

    def test_same_price_is_not_reported_again(self, cfg, monkeypatch):
        posta = self.Odesilatel()
        self._run(cfg, 1610.0, posta, monkeypatch)
        self._run(cfg, 1610.0, posta, monkeypatch)

        assert len(posta.zpravy) == 1

    def test_cheaper_find_is_reported_with_the_old_price(self, cfg, monkeypatch):
        posta = self.Odesilatel()
        self._run(cfg, 1610.0, posta, monkeypatch)
        self._run(cfg, 1200.0, posta, monkeypatch)

        assert len(posta.zpravy) == 2
        assert "místo 1\xa0610\xa0Kč" in posta.zpravy[1]

    def test_more_expensive_find_is_ignored(self, cfg, monkeypatch):
        posta = self.Odesilatel()
        self._run(cfg, 1200.0, posta, monkeypatch)
        self._run(cfg, 1610.0, posta, monkeypatch)

        assert len(posta.zpravy) == 1


class TestZprava:
    def _trip(self, cena=1610.0):
        from src.watch import Trip

        return Trip(price_czk=cena,
                    out_dep=dt.datetime(2099, 11, 6, 20, 15),
                    out_arr=dt.datetime(2099, 11, 6, 22, 40),
                    back_dep=dt.datetime(2099, 11, 15, 12, 0),
                    back_arr=dt.datetime(2099, 11, 15, 14, 30),
                    url="https://www.ryanair.com/x")

    def test_first_find_shows_the_term(self):
        from src.notify import format_watch

        text = format_watch("PRG→BCN · 9 nocí", self._trip())

        assert "pá 6. 11. 20:15" in text and "9 nocí" in text
        assert "ryanair.com" in text

    def test_overconstrained_watch_says_so(self):
        """Ticho, ze kterého se nepozná, jestli se nic nenašlo nebo je něco
        rozbité, je tady ta nejhorší odpověď."""
        from src.notify import format_watch

        text = format_watch("PRG→BCN", self._trip(), None, vyhovuje=False)

        assert "časy nesedí" in text
