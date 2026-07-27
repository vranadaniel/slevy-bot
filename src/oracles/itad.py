"""Oracle nad IsThereAnyDeal — referenční ceny her.

ITAD sleduje ceny her napříč 50+ oficiálními obchody a hlavně si drží **historii,
kdy byla hra kdy nejlevnější**. To je přesně to, co katalogu Kinguinu chybělo:
herních klíčů jsou tam tisíce a ceník v `references.yaml` je pokrýt nemůže.

Proč je historické minimum lepší laťka než doporučená cena: hry se slevují
neustále. Hra s doporučenou cenou 1 500 Kč, která na každém výprodeji spadne na
120 Kč, **není trhák za 300 Kč**, i když to vypadá jako 80% sleva. Právě tenhle
typ falešného poplachu by jinak chodil.

Oracle proto dělá dvě věci:

* jako **hodnotu** vrací skutečnou doporučenou cenu z ITAD (na rozdíl od
  `price.market` na Kinguinu si ji neurčuje prodejce)
* do `offer.extra` uloží **historické minimum**, které ve `score.py` funguje
  jako brána: co už někdy bylo levnější, nemá cenu hlásit jako trhák

Provoz je postavený na dvou dávkových endpointech, takže tisíce her stojí
jednotky požadavků:

* ``POST /lookup/id/title/v1``   — názvy → ID (funguje i bez klíče)
* ``POST /games/historylow/v1``  — až 200 ID naráz, klíč vyžaduje

Obojí se cachuje v SQLite: překlad názvů natrvalo (tituly se nemění), minima
s TTL. V ustáleném stavu tedy oracle skoro nic nevolá.
"""

from __future__ import annotations

import logging
import math
import time

from .. import titles as title_utils
from ..sources.base import CATALOG, Offer
from .base import Value

log = logging.getLogger(__name__)

BASE_URL = "https://api.isthereanydeal.com"
LOOKUP_CHUNK = 100     # názvů na jeden dotaz
HISTORYLOW_CHUNK = 200  # tvrdý limit API
INFO_DELAY_S = 0.15     # /games/info/v2 bere jednu hru na dotaz, nespěchejme

# Typy produktů z Kinguinu, u kterých má smysl ptát se na hru.
# RANDOM_KEY schválně chybí: náhodný klíč nemá známý obsah, takže se nedá ocenit.
GAME_TYPES = {"GAME", "GAME_ACCOUNT", "DLC", "GAME_GIFT"}


class ItadOracle:
    name = "itad"

    def __init__(self, http, store, fx, cfg) -> None:
        self.http = http
        self.store = store
        self.fx = fx
        self.api_key = cfg.itad_key
        self.country = cfg.get("itad.country", "CZ")
        self.ttl_days = int(cfg.get("itad.low_ttl_days", 7))
        self.max_lookup = int(cfg.get("itad.max_lookup_per_run", 600))
        self.max_lows = int(cfg.get("itad.max_lows_per_run", 600))
        self.info_ttl_days = int(cfg.get("itad.info_ttl_days", 30))
        self.max_info = int(cfg.get("itad.max_info_per_run", 120))
        self._resolved: dict[str, str] = {}  # uid nabídky -> game_id

    # ---------- příprava dávky ----------

    def prepare(self, offers: list[Offer]) -> None:
        """Doplní cache pro dávku nabídek. Volá se jednou za běh, před scoringem."""
        if not self.api_key:
            log.info("ITAD vypnutý (chybí klíč), hry zůstanou bez ocenění")
            return

        games = [o for o in offers if _is_game(o)]
        if not games:
            return

        try:
            self._resolve_titles(games)
            self._fetch_lows()
        except Exception as exc:  # noqa: BLE001 — výpadek ITAD nesmí shodit běh
            log.warning("ITAD selhal a přeskakuji ho: %s", exc)

    def _resolve_titles(self, games: list[Offer]) -> None:
        # Kandidátní tituly pro každou nabídku, ať víme, na co se ptát.
        per_offer: dict[str, list[str]] = {
            o.uid: title_utils.candidates(o.name) for o in games
        }
        wanted = list(dict.fromkeys(t for cands in per_offer.values() for t in cands))

        known = self.store.itad_known_titles(wanted)
        missing = [t for t in wanted if t not in known][: self.max_lookup]

        if missing:
            fetched: dict[str, str | None] = {}
            for i in range(0, len(missing), LOOKUP_CHUNK):
                chunk = missing[i: i + LOOKUP_CHUNK]
                data = self.http.post_json(
                    f"{BASE_URL}/lookup/id/title/v1", chunk, self._headers()
                )
                # Odpověď mapuje titul na UUID, nebo na null když ITAD hru nezná.
                fetched.update({t: data.get(t) for t in chunk})
            # Ukládáme i nenalezené, ať se marné dotazy neopakují každý běh.
            self.store.itad_save_titles(fetched)
            known.update(fetched)
            log.info("ITAD: přeloženo %s nových názvů", len(fetched))

        for uid, cands in per_offer.items():
            game_id = next((known[c] for c in cands if known.get(c)), None)
            if game_id:
                self._resolved[uid] = game_id

    def _fetch_lows(self) -> None:
        game_ids = list(dict.fromkeys(self._resolved.values()))
        stale = self.store.itad_stale_game_ids(game_ids, self.ttl_days)[: self.max_lows]
        if not stale:
            return

        for i in range(0, len(stale), HISTORYLOW_CHUNK):
            chunk = stale[i: i + HISTORYLOW_CHUNK]
            data = self.http.post_json(
                f"{BASE_URL}/games/historylow/v1?country={self.country}",
                chunk,
                self._headers(),
            )
            returned = set()
            for row in data or []:
                game_id = row.get("id")
                if not game_id:
                    continue
                returned.add(game_id)
                self.store.itad_save_low(game_id, *self._prices(row.get("low")))
            # Hry bez zaznamenaného minima zapíšeme prázdné, ať se nedotazujeme pořád.
            for game_id in chunk:
                if game_id not in returned:
                    self.store.itad_save_low(game_id, None, None, None, None)

        self.store.commit()
        log.info("ITAD: načteno %s historických minim", len(stale))

    def _prices(self, low: dict | None):
        if not low:
            return None, None, None, None
        price = low.get("price") or {}
        regular = low.get("regular") or {}
        shop = (low.get("shop") or {}).get("name")

        low_czk = self._to_czk(price)
        regular_czk = self._to_czk(regular)
        return low_czk, regular_czk, shop, low.get("cut")

    def _to_czk(self, amount: dict) -> float | None:
        value, currency = amount.get("amount"), amount.get("currency")
        if value is None or not currency:
            return None
        try:
            return self.fx.to_czk(float(value), currency)
        except (ValueError, KeyError):
            return None

    def _headers(self) -> dict:
        return {"ITAD-API-Key": self.api_key, "Content-Type": "application/json"}

    # ---------- popularita her ----------

    def enrich_popularity(self, offers: list[Offer]) -> None:
        """Doplní `extra["popularity"]` u her z dané hrstky nabídek.

        Volá se **až na tom, co míří do souhrnu**, ne na celém katalogu:
        endpoint bere jednu hru na dotaz, takže 5 000 položek by znamenalo
        5 000 požadavků. Po prvních dnech je práce skoro nulová — hodnocení
        se v cache drží měsíc.
        """
        if not self.api_key:
            return

        wanted: dict[str, list[Offer]] = {}
        for offer in offers:
            game_id = self._resolved.get(offer.uid)
            if game_id:
                wanted.setdefault(game_id, []).append(offer)
        if not wanted:
            return

        stale = [gid for gid in wanted
                 if self.store.itad_get_info(gid, self.info_ttl_days) is None]
        fetched = 0
        for game_id in stale[: self.max_info]:
            try:
                data = self.http.get_json(
                    f"{BASE_URL}/games/info/v2",
                    params={"id": game_id}, headers=self._headers(),
                )
            except Exception as exc:  # noqa: BLE001 — výpadek nesmí shodit běh
                log.warning("ITAD info pro %s selhalo: %s", game_id, exc)
                continue
            self.store.itad_save_info(game_id, *_review_stats(data))
            fetched += 1
            if INFO_DELAY_S:
                time.sleep(INFO_DELAY_S)

        if fetched:
            self.store.commit()
            log.info("ITAD: načteno %s hodnocení her", fetched)

        for game_id, group in wanted.items():
            info = self.store.itad_get_info(game_id, self.info_ttl_days)
            if not info:
                continue
            for offer in group:
                offer.extra["popularity"] = popularity(info, offer.credibility)
                offer.extra["reviews_score"] = info["reviews_score"]
                offer.extra["reviews_count"] = info["reviews_count"]
                offer.extra["released"] = info["released"]
                offer.extra["itad_rank"] = info["rank"]

    # ---------- rozhraní oracle ----------

    def value_of(self, offer: Offer) -> Value | None:
        game_id = self._resolved.get(offer.uid)
        if not game_id:
            return None

        low = self.store.itad_get_low(game_id, self.ttl_days)
        if not low:
            return None

        # Historické minimum si nese nabídka dál — ve scoringu funguje jako brána.
        if low["low_czk"]:
            offer.extra["itad_low_czk"] = low["low_czk"]
            offer.extra["itad_shop"] = low["shop"]

        regular = low["regular_czk"]
        if not regular or regular <= 0:
            return None

        note = f"ITAD: běžně {regular:.0f} Kč"
        if low["low_czk"]:
            note += f", historicky nejníž {low['low_czk']:.0f} Kč"
            if low["shop"]:
                note += f" ({low['shop']})"

        return Value(
            real_value_czk=regular,
            origin="itad",
            note=note,
            confidence=0.9,
        )


def _review_stats(data: dict):
    """Z odpovědi /games/info/v2 vytáhne, co potřebujeme: (skóre, počet, rank, vydání).

    `reviews` je pole přes víc zdrojů. Bereme to s nejvíc hlasy — u her na
    Steamu je to Steam, u ostatních aspoň něco.
    """
    reviews = [r for r in (data.get("reviews") or []) if r.get("count")]
    best = max(reviews, key=lambda r: r["count"], default=None)
    stats = data.get("stats") or {}
    return (
        (best or {}).get("score"),
        (best or {}).get("count"),
        stats.get("rank"),
        data.get("releaseDate"),
    )


def popularity(info: dict, fallback: float | None = None) -> float:
    """0–1: jak moc o tu hru lidi stojí.

    Proč to vůbec počítáme: sleva sama o sobě vytahuje nahoru tituly, o které
    nikdo nestojí — čím míň lidí hru chce, tím hlouběji jde cena. Souhrn se pak
    plní starými hrami za pár korun a trhák zapadne.

    Objem hodnocení říká, jestli hru vůbec někdo hrál, skóre jestli za to
    stála. Ani jedno samo nestačí: 98 % ze šesti hlasů nic neznamená a stovky
    tisíc hlasů má i propadák. Objem se bere logaritmicky, protože rozdíl mezi
    tisícem a deseti tisíci hodnocení je podstatný, mezi 300 a 310 tisíci ne.

    Hry bez hodnocení (Battle.net, EA App) padají na `fallback` — pozici
    v žebříčku prodejnosti Kinguinu. Vlastní `stats.rank` z ITAD schválně
    nepoužíváme: neměřili jsme, jak je rozložený, kdežto prodejnosti rozumíme.
    """
    count = info.get("reviews_count") or 0
    if count <= 0:
        return fallback if fallback is not None else 0.0

    volume = min(1.0, math.log10(count + 1) / 5.0)   # 100 000 hodnocení -> 1,0
    score = info.get("reviews_score")
    if score is None:
        return volume
    return 0.65 * volume + 0.35 * (max(0.0, min(100.0, float(score))) / 100.0)


def _is_game(offer: Offer) -> bool:
    if offer.kind != CATALOG:
        return False
    return (offer.extra.get("product_type") or "").upper() in GAME_TYPES
