"""Odhad reálné hodnoty nabídky.

Druhý šev projektu. `Source` říká, co se prodává a za kolik; `ValueOracle` říká,
co to doopravdy stojí. Bez toho druhého čísla nejde spočítat procento slevy, což
je jediné kritérium, podle kterého se rozhoduje.

Oracles se zkoušejí v pořadí od nejlevnějšího a nejdůvěryhodnějšího:

1. vlastní cenová historie (jen katalog, nejde zfalšovat, ale potřebuje čas)
2. `references.yaml` (ruční ceník, překlenuje studený start)
3. původní cena uvedená v příspěvku (u feedů, slabší)
4. AI soudce (poslední instance, běží dávkově a jen na užší výběr)

Sem později zapadne IsThereAnyDeal — je to zdroj referenčních cen her, ne zdroj
nabídek, takže patří právě do téhle vrstvy, ne mezi `sources`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..sources.base import Offer


@dataclass
class Value:
    """Odhad reálné hodnoty položky v korunách."""

    real_value_czk: float
    origin: str                # "history" | "references" | "feed" | "ai"
    note: str | None = None
    confidence: float = 1.0    # 0–1, jak moc odhadu věřit


class ValueOracle(Protocol):
    name: str

    def value_of(self, offer: Offer) -> Value | None:
        """Vrátí odhad, nebo None když položku neumí ocenit."""
        ...
