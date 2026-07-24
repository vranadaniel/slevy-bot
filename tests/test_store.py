"""Cenová historie a deduplikace."""

import datetime as dt

from src.store import Store


def _ts(days_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()


class TestTimeWeightedMedian:
    def test_median_weights_by_duration_not_by_row_count(self, tmp_path):
        """Do logu se zapisují jen ZMĚNY ceny, takže prostý medián by lhal.

        Položka stála 149 Kč tři týdny a včera spadla na 60 Kč. V logu jsou dva
        řádky — prostý medián by dal 104 Kč, jenže běžná cena je 149 Kč.
        """
        store = Store(tmp_path / "t.db")
        store.conn.execute(
            "INSERT INTO price_log (source, uid, ts, price_czk) VALUES (?,?,?,?)",
            ("kinguin", "x", _ts(21), 149.0),
        )
        store.conn.execute(
            "INSERT INTO price_log (source, uid, ts, price_czk) VALUES (?,?,?,?)",
            ("kinguin", "x", _ts(1), 60.0),
        )
        store.commit()

        profile = store.price_profile("kinguin", "x", days=30)
        assert profile["median"] == 149.0
        assert profile["min"] == 60.0
        assert profile["span_days"] > 20
        store.close()


class TestPriceLogging:
    def test_only_changes_are_logged(self, tmp_path):
        store = Store(tmp_path / "t.db")
        for _ in range(5):
            store.record_price("kinguin", "x", "Věc", "http://x", "GAME", 100.0)
        store.record_price("kinguin", "x", "Věc", "http://x", "GAME", 80.0)
        store.commit()

        rows = store.conn.execute("SELECT COUNT(*) AS n FROM price_log").fetchone()
        assert rows["n"] == 2, "pět stejných cen má být jeden řádek, ne pět"
        store.close()

    def test_min_ever_tracks_lowest(self, tmp_path):
        store = Store(tmp_path / "t.db")
        for price in (100.0, 60.0, 90.0):
            store.record_price("kinguin", "x", "Věc", "http://x", "GAME", price)
        store.commit()
        assert store.product_stats("kinguin", "x")["min_ever"] == 60.0
        store.close()


class TestDedup:
    def test_first_alert_always_passes(self, tmp_path):
        store = Store(tmp_path / "t.db")
        assert store.should_alert("kinguin", "x", 100.0, 0.15, 30) is True
        store.close()

    def test_same_price_is_not_repeated(self, tmp_path):
        """Bez tohohle by ti stejný deal přišel 48x denně."""
        store = Store(tmp_path / "t.db")
        store.mark_alerted("kinguin", "x", 100.0, "instant")
        store.commit()
        assert store.should_alert("kinguin", "x", 100.0, 0.15, 30) is False
        assert store.should_alert("kinguin", "x", 95.0, 0.15, 30) is False
        store.close()

    def test_further_drop_alerts_again(self, tmp_path):
        store = Store(tmp_path / "t.db")
        store.mark_alerted("kinguin", "x", 100.0, "instant")
        store.commit()
        assert store.should_alert("kinguin", "x", 80.0, 0.15, 30) is True
        store.close()


class TestDigestQueue:
    def test_queueing_records_an_alert(self, tmp_path):
        """Regrese: bez zápisu do `alerts` se stejné položky vracely do souhrnu
        každý večer znovu — katalog totiž vidíme pořád dokola."""
        from src.main import _queue
        from src.score import Verdict
        from src.sources.base import CATALOG, Offer

        store = Store(tmp_path / "t.db")
        offer = Offer(source="kinguin", kind=CATALOG, uid="w11",
                      name="Windows 11 Pro Key", price_czk=568.0, url="http://x")
        verdict = Verdict(offer=offer, value_ratio=0.126)

        _queue(store, verdict)
        store.commit()

        assert store.digest_size() == 1
        assert store.should_alert("kinguin", "w11", 568.0, 0.15, 30) is False
        store.close()

    def test_queue_is_idempotent_within_a_day(self, tmp_path):
        from src.main import _queue
        from src.score import Verdict
        from src.sources.base import CATALOG, Offer

        store = Store(tmp_path / "t.db")
        offer = Offer(source="kinguin", kind=CATALOG, uid="w11",
                      name="Windows 11 Pro Key", price_czk=568.0, url="http://x")
        for _ in range(3):
            _queue(store, Verdict(offer=offer, value_ratio=0.126))
        store.commit()

        assert store.digest_size() == 1, "táž položka nesmí být v souhrnu třikrát"
        store.close()


class TestFeedSeen:
    def test_seen_guid_is_remembered(self, tmp_path):
        store = Store(tmp_path / "t.db")
        assert store.is_seen("mydealz", "g1") is False
        store.mark_seen("mydealz", "g1")
        store.commit()
        assert store.is_seen("mydealz", "g1") is True
        store.close()


class TestDailyCounter:
    def test_counter_increments(self, tmp_path):
        store = Store(tmp_path / "t.db")
        assert store.daily_counter("judge") == 0
        store.bump_daily_counter("judge")
        store.bump_daily_counter("judge")
        assert store.daily_counter("judge") == 2
        store.close()
