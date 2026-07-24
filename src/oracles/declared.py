"""Oracle nad cenou deklarovanou v příspěvku.

Platí jen pro **feedy**. Když Pepper uvede "statt 1.599€" nebo "RRP £89", je to
obvykle poctivý údaj — píše ho komunita, ne prodejce, a ostatní ho vidí.

Pro katalog se schválně nepoužívá. Kinguin má pole `price.market`, jenže to si
nastavuje prodejce sám: šunta "Tanks Battle Steam CD Key" se tváří na 97,50 €
a 99 % slevu. Kdyby z toho oracle vyráběl hodnotu, bot by posílal samý brak.
Katalogová `ref_price_czk` proto slouží nanejvýš jako doplňková poznámka.
"""

from __future__ import annotations

from ..sources.base import FEED, Offer
from .base import Value


class DeclaredOracle:
    name = "feed"

    def value_of(self, offer: Offer) -> Value | None:
        if offer.kind != FEED:
            return None
        if not offer.ref_price_czk or offer.ref_price_czk <= offer.price_czk:
            return None
        return Value(
            real_value_czk=offer.ref_price_czk,
            origin="feed",
            note=f"původní cena z příspěvku {offer.ref_price_czk:.0f} Kč",
            confidence=0.6,
        )
