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

    def has_history(self, offer: Offer) -> bool:
        """Má položka dost dlouhou historii, aby o ní mohla rozhodovat?

        Používá to `score._reference_needs_history`: dokud historie není,
        rozhoduje ruční ceník (studený start), jakmile je, rozhoduje ona.
        """
        if offer.kind != CATALOG:
            return False
        profile = self.store.price_profile(offer.source, offer.uid, self.window_days)
        return profile is not None and profile["span_days"] >= self.min_span_days

    def is_all_time_low(self, offer: Offer) -> bool:
        """Je cena níž, než jsme kdy DŘÍV viděli?

        Porovnává se proti `prev_min`, ne proti `min_ever`. `record_price` totiž
        v `main.py` běží dřív než scoring, takže `min_ever` už dnešní cenu
        obsahuje — proti němu by „historicky nejnižší" platilo pro každou
        položku při prvním pozorování a pro každou nehybnou cenu napořád.

        Změřeno na Ryanairu: všech ~130 tras se takhle hned první běh označilo
        za historické minimum a šlo k AI soudci, který jim vymyslel běžnou cenu.
        Odtud ta záplava úplně obyčejných letenek.

        Porovnání je proto OSTŘE menší: stejná cena jako dosud není nález.
        """
        stats = self.store.product_stats(offer.source, offer.uid)
        if not stats or stats.get("prev_min") is None:
            return False  # první pozorování není historie
        return offer.price_czk < stats["prev_min"] - 0.01
