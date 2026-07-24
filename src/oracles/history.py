"""Oracle nad vlastní cenovou historií.

Nejsilnější zdroj reference, protože ho nikdo nemůže ovlivnit. Když si každou
půlhodinu zapíšeme cenu, po pár dnech víme, že položka běžně stojí 149 Kč — a
když spadne na 60 Kč, poznáme to bez důvěry prodejci a jeho vymyšlené MSRP.

Cenou za tu sílu je čas: první dny historie neexistuje a oracle vrací None.
Tehdy nastupuje ceník a AI soudce.
"""

from __future__ import annotations

from ..sources.base import CATALOG, Offer
from .base import Value


class HistoryOracle:
    name = "history"

    def __init__(self, store, min_span_days: float = 2.0, window_days: int = 30) -> None:
        self.store = store
        self.min_span_days = min_span_days
        self.window_days = window_days

    def value_of(self, offer: Offer) -> Value | None:
        if offer.kind != CATALOG:
            return None  # feed vidí každou položku jen jednou, historie nedává smysl

        profile = self.store.price_profile(offer.source, offer.uid, self.window_days)
        if profile is None or profile["span_days"] < self.min_span_days:
            return None

        median = profile["median"]
        if median <= 0 or median <= offer.price_czk:
            return None  # není to sleva proti vlastnímu záznamu

        # Čím delší historie, tím větší důvěra; nad dva týdny je to plná jistota.
        confidence = min(1.0, profile["span_days"] / 14.0)

        return Value(
            real_value_czk=median,
            origin="history",
            note=f"vlastní historie {profile['span_days']:.0f} dní, běžně {median:.0f} Kč",
            confidence=confidence,
        )

    def is_all_time_low(self, offer: Offer) -> bool:
        stats = self.store.product_stats(offer.source, offer.uid)
        if not stats or stats.get("min_ever") is None:
            return False
        return offer.price_czk <= stats["min_ever"] + 0.01
