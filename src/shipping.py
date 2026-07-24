"""Doručuje obchod do ČR?

Pepper feedy jsou německé, britské, francouzské a polské. Fyzické zboží má smysl
jen tehdy, když e-shop pošle do Česka. Odpověď je záměrně trojstavová:

* `True`  — víme, že posílá
* `False` — víme, že neposílá (položka se zahodí)
* `None`  — nevíme; položka projde, ale nikdy jako okamžité upozornění

Trojstavovost je tu proto, že mlčet o možném trháku je horší než poslat ho
s poznámkou „doručení neověřeno".
"""

from __future__ import annotations


class ShippingPolicy:
    def __init__(self, merchants_cfg: dict) -> None:
        self.digital_merchants = [m.lower() for m in merchants_cfg.get("digital_merchants", [])]
        self.digital_categories = [c.lower() for c in merchants_cfg.get("digital_categories", [])]
        self.ships = {k.lower(): bool(v) for k, v in (merchants_cfg.get("ships_to_cz") or {}).items()}

    def is_digital(self, offer) -> bool:
        merchant = (offer.merchant or "").lower()
        category = (offer.category or "").lower()
        if any(d in merchant for d in self.digital_merchants):
            return True
        return any(d in category for d in self.digital_categories)

    def ships_to_cz(self, offer) -> bool | None:
        # Kinguin i fly4free jsou svou povahou bez dopravy.
        if offer.source in ("kinguin", "fly4free"):
            return True
        if self.is_digital(offer):
            return True

        merchant = (offer.merchant or "").lower()
        if not merchant:
            return offer.extra.get("ai_ships_to_cz")

        # Nejdelší shoda vyhrává, ať "media expert" nepřebije "mediamarkt" a naopak.
        best: tuple[int, bool] | None = None
        for key, value in self.ships.items():
            if key in merchant and (best is None or len(key) > best[0]):
                best = (len(key), value)
        if best is not None:
            return best[1]

        return offer.extra.get("ai_ships_to_cz")
