"""SQLite: cenová historie, deduplikace upozornění a fronta denního souhrnu.

Dvě věci, které stojí za pozornost:

1. Do `price_log` se zapisuje jen ZMĚNA ceny, ne každý běh. Drtivá většina
   z 5000 sledovaných položek se mezi běhy nehne, takže databáze roste pomalu
   a vejde se na orphan větev v gitu.

2. Deduplikace je to, co dělá rozdíl mezi použitelným botem a spamem. Bez ní
   by ti stejný deal přišel 48x denně.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    source     TEXT NOT NULL,
    uid        TEXT NOT NULL,
    name       TEXT,
    url        TEXT,
    category   TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    min_ever   REAL,
    samples    INTEGER DEFAULT 0,
    PRIMARY KEY (source, uid)
);

CREATE TABLE IF NOT EXISTS price_log (
    source    TEXT NOT NULL,
    uid       TEXT NOT NULL,
    ts        TEXT NOT NULL,
    price_czk REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_log ON price_log (source, uid, ts);

CREATE TABLE IF NOT EXISTS alerts (
    source    TEXT NOT NULL,
    uid       TEXT NOT NULL,
    ts        TEXT NOT NULL,
    price_czk REAL NOT NULL,
    level     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts ON alerts (source, uid, ts);

-- Feedy: jednou viděné guid se už neřeší.
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    uid    TEXT NOT NULL,
    ts     TEXT NOT NULL,
    PRIMARY KEY (source, uid)
);

-- Co čeká na večerní souhrn.
CREATE TABLE IF NOT EXISTS digest_queue (
    source  TEXT NOT NULL,
    uid     TEXT NOT NULL,
    ts      TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (source, uid)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- IsThereAnyDeal: překlad názvu na ID hry. Trvalá cache, tituly se nemění.
-- Uloženo i "nenalezeno" (game_id NULL), ať se marné dotazy neopakují.
CREATE TABLE IF NOT EXISTS itad_titles (
    title      TEXT PRIMARY KEY,
    game_id    TEXT,
    resolved_at TEXT NOT NULL
);

-- Historická minima. Cache s TTL, protože minima se občas posunou.
CREATE TABLE IF NOT EXISTS itad_lows (
    game_id     TEXT PRIMARY KEY,
    low_czk     REAL,
    regular_czk REAL,
    shop        TEXT,
    cut         INTEGER,
    fetched_at  TEXT NOT NULL
);

-- Hodnocení hráčů a datum vydání. Slouží k tomu, aby souhrn nebyl samá stará
-- hra za pár korun. Mění se pomalu, takže dlouhé TTL.
CREATE TABLE IF NOT EXISTS itad_info (
    game_id       TEXT PRIMARY KEY,
    reviews_score INTEGER,
    reviews_count INTEGER,
    rank          INTEGER,
    released      TEXT,
    fetched_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Rychlý sken cestování běží na vlastním timeru a může se s hlavním
        # skenem potkat nad touhle databází. Radši počkat, než spadnout na
        # "database is locked".
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---------- meta ----------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        self.conn.commit()

    def bump_daily_counter(self, key: str) -> int:
        """Vrátí novou hodnotu denního čítače (pojistka na počet volání AI)."""
        today = dt.date.today().isoformat()
        full = f"counter:{key}:{today}"
        current = int(self.get_meta(full) or 0) + 1
        self.set_meta(full, str(current))
        return current

    def daily_counter(self, key: str) -> int:
        today = dt.date.today().isoformat()
        return int(self.get_meta(f"counter:{key}:{today}") or 0)

    # ---------- katalog: cenová historie ----------

    def record_price(self, source: str, uid: str, name: str, url: str,
                     category: str | None, price_czk: float) -> None:
        now = _now()
        row = self.conn.execute(
            "SELECT price_czk FROM price_log WHERE source = ? AND uid = ? "
            "ORDER BY ts DESC LIMIT 1",
            (source, uid),
        ).fetchone()

        # Zapisujeme jen změny — jinak by databáze narostla o 5000 řádků každý běh.
        if row is None or abs(row["price_czk"] - price_czk) > 0.01:
            self.conn.execute(
                "INSERT INTO price_log (source, uid, ts, price_czk) VALUES (?, ?, ?, ?)",
                (source, uid, now, price_czk),
            )

        self.conn.execute(
            """
            INSERT INTO products (source, uid, name, url, category,
                                  first_seen, last_seen, min_ever, samples)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(source, uid) DO UPDATE SET
                name      = excluded.name,
                url       = excluded.url,
                category  = excluded.category,
                last_seen = excluded.last_seen,
                min_ever  = MIN(products.min_ever, excluded.min_ever),
                samples   = products.samples + 1
            """,
            (source, uid, name, url, category, now, now, price_czk),
        )

    def history(self, source: str, uid: str, days: int = 30) -> list[float]:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT price_czk FROM price_log WHERE source = ? AND uid = ? AND ts >= ?",
            (source, uid, cutoff),
        ).fetchall()
        return [r["price_czk"] for r in rows]

    def median_price(self, source: str, uid: str, days: int = 30) -> float | None:
        prices = self.history(source, uid, days)
        return statistics.median(prices) if prices else None

    def price_profile(self, source: str, uid: str, days: int = 30) -> dict[str, Any] | None:
        """Časově vážený profil ceny za posledních `days` dní.

        Prostý medián z `price_log` by lhal, protože se logují jen ZMĚNY ceny.
        Kdyby položka stála 149 Kč tři týdny a pak spadla na 60 Kč, byly by
        v logu dva řádky a medián by vyšel 104 Kč — jenže běžná cena je 149 Kč.
        Proto se každá cena váží dobou, po kterou platila.
        """
        now = dt.datetime.now(dt.timezone.utc)
        window_start = now - dt.timedelta(days=days)

        rows = self.conn.execute(
            "SELECT ts, price_czk FROM price_log WHERE source = ? AND uid = ? ORDER BY ts",
            (source, uid),
        ).fetchall()
        if not rows:
            return None

        points: list[tuple[dt.datetime, float]] = []
        for row in rows:
            try:
                ts = dt.datetime.fromisoformat(row["ts"])
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            points.append((ts, row["price_czk"]))
        if not points:
            return None

        first_ts = points[0][0]

        # Cena platná na začátku okna je poslední zaznamenaná před ním.
        weighted: list[tuple[float, float]] = []  # (cena, váha v sekundách)
        for i, (ts, price) in enumerate(points):
            end = points[i + 1][0] if i + 1 < len(points) else now
            start = max(ts, window_start)
            if end <= window_start:
                continue
            seconds = (end - start).total_seconds()
            if seconds > 0:
                weighted.append((price, seconds))

        if not weighted:
            price = points[-1][1]
            weighted = [(price, 1.0)]

        weighted.sort(key=lambda p: p[0])
        total = sum(w for _, w in weighted)
        acc = 0.0
        median = weighted[-1][0]
        for price, w in weighted:
            acc += w
            if acc >= total / 2:
                median = price
                break

        prices = [p for p, _ in weighted]
        return {
            "median": median,
            "min": min(prices),
            "max": max(prices),
            "changes": len(points),
            "span_days": max(0.0, (now - first_ts).total_seconds() / 86400.0),
        }

    def product_stats(self, source: str, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT min_ever, samples, first_seen FROM products WHERE source = ? AND uid = ?",
            (source, uid),
        ).fetchone()
        return dict(row) if row else None

    # ---------- feedy: viděné položky ----------

    def is_seen(self, source: str, uid: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE source = ? AND uid = ?", (source, uid)
        ).fetchone()
        return row is not None

    def mark_seen(self, source: str, uid: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (source, uid, ts) VALUES (?, ?, ?)",
            (source, uid, _now()),
        )

    # ---------- deduplikace upozornění ----------

    def should_alert(self, source: str, uid: str, price_czk: float,
                     realert_drop: float, realert_days: int) -> bool:
        """Upozornit znovu jen při dalším výrazném poklesu, nebo po delší době."""
        row = self.conn.execute(
            "SELECT ts, price_czk FROM alerts WHERE source = ? AND uid = ? "
            "ORDER BY ts DESC LIMIT 1",
            (source, uid),
        ).fetchone()
        if row is None:
            return True

        if price_czk <= row["price_czk"] * (1 - realert_drop):
            return True

        try:
            last = dt.datetime.fromisoformat(row["ts"])
        except ValueError:
            return True
        age = dt.datetime.now(dt.timezone.utc) - last
        return age.days >= realert_days

    def mark_alerted(self, source: str, uid: str, price_czk: float, level: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts (source, uid, ts, price_czk, level) VALUES (?, ?, ?, ?, ?)",
            (source, uid, _now(), price_czk, level),
        )

    # ---------- fronta denního souhrnu ----------

    def queue_digest(self, source: str, uid: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO digest_queue (source, uid, ts, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, uid) DO UPDATE SET "
            "ts = excluded.ts, payload = excluded.payload",
            (source, uid, _now(), json.dumps(payload, ensure_ascii=False)),
        )

    def pop_digest(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload FROM digest_queue ORDER BY ts"
        ).fetchall()
        items = [json.loads(r["payload"]) for r in rows]
        self.conn.execute("DELETE FROM digest_queue")
        self.conn.commit()
        return items

    def digest_size(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM digest_queue").fetchone()
        return row["n"]

    # ---------- IsThereAnyDeal cache ----------

    def itad_known_titles(self, titles: list[str]) -> dict[str, str | None]:
        """Vrátí jen tituly, které už máme vyřešené (i ty nenalezené)."""
        if not titles:
            return {}
        out: dict[str, str | None] = {}
        for i in range(0, len(titles), 400):
            chunk = titles[i: i + 400]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT title, game_id FROM itad_titles WHERE title IN ({marks})",
                chunk,
            ).fetchall()
            out.update({r["title"]: r["game_id"] for r in rows})
        return out

    def itad_save_titles(self, mapping: dict[str, str | None]) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO itad_titles (title, game_id, resolved_at) VALUES (?, ?, ?) "
            "ON CONFLICT(title) DO UPDATE SET "
            "game_id = excluded.game_id, resolved_at = excluded.resolved_at",
            [(title, gid, now) for title, gid in mapping.items()],
        )

    def itad_get_low(self, game_id: str, ttl_days: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT low_czk, regular_czk, shop, cut, fetched_at FROM itad_lows "
            "WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            fetched = dt.datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if (dt.datetime.now(dt.timezone.utc) - fetched).days >= ttl_days:
            return None
        return dict(row)

    def itad_stale_game_ids(self, game_ids: list[str], ttl_days: int) -> list[str]:
        """ID, která je potřeba (znovu) načíst — chybí v cache nebo jsou stará."""
        return [gid for gid in game_ids if self.itad_get_low(gid, ttl_days) is None]

    def itad_save_low(self, game_id: str, low_czk: float | None,
                      regular_czk: float | None, shop: str | None,
                      cut: int | None) -> None:
        self.conn.execute(
            "INSERT INTO itad_lows (game_id, low_czk, regular_czk, shop, cut, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id) DO UPDATE SET "
            "low_czk = excluded.low_czk, regular_czk = excluded.regular_czk, "
            "shop = excluded.shop, cut = excluded.cut, fetched_at = excluded.fetched_at",
            (game_id, low_czk, regular_czk, shop, cut, _now()),
        )

    def itad_get_info(self, game_id: str, ttl_days: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT reviews_score, reviews_count, rank, released, fetched_at "
            "FROM itad_info WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            fetched = dt.datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if (dt.datetime.now(dt.timezone.utc) - fetched).days >= ttl_days:
            return None
        return dict(row)

    def itad_save_info(self, game_id: str, reviews_score: int | None,
                       reviews_count: int | None, rank: int | None,
                       released: str | None) -> None:
        self.conn.execute(
            "INSERT INTO itad_info "
            "(game_id, reviews_score, reviews_count, rank, released, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id) DO UPDATE SET "
            "reviews_score = excluded.reviews_score, "
            "reviews_count = excluded.reviews_count, "
            "rank = excluded.rank, released = excluded.released, "
            "fetched_at = excluded.fetched_at",
            (game_id, reviews_score, reviews_count, rank, released, _now()),
        )

    # ---------- údržba ----------

    def prune(self, keep_days: int = 60) -> None:
        """Zapomene, co jsme dlouho neviděli. Drží databázi malou napořád."""
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).isoformat()
        self.conn.execute("DELETE FROM products WHERE last_seen < ?", (cutoff,))
        self.conn.execute(
            "DELETE FROM price_log WHERE (source, uid) NOT IN "
            "(SELECT source, uid FROM products)"
        )
        self.conn.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
        # Staré denní čítače
        self.conn.execute(
            "DELETE FROM meta WHERE k LIKE 'counter:%' AND k < ?",
            (f"counter:zzz:{(dt.date.today() - dt.timedelta(days=7)).isoformat()}",),
        )
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
