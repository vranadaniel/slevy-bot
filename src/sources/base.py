"""Rozhraní zdrojů nabídek.

Zdroje jsou dvojího druhu a nejsou zaměnitelné:

* **catalog** — tutéž položku vidíme opakovaně, takže si stavíme vlastní cenovou
  historii a poznáme historické minimum. Deduplikuje se podle `uid` a poklesu ceny.
* **feed**    — proud jedinečných příspěvků, historie neexistuje. Deduplikuje se
  podle `uid` (guid); jednou viděné se už neřeší.

`credibility` je normalizovaná obrana proti braku. Každý zdroj ji odvozuje z toho,
co jde těžko zfalšovat (prodejnost, komunitní teplota, redakční výběr). Nízká
hodnota položku nezahodí, jen jí zakáže cestu do okamžitého upozornění.

Nic pod touhle vrstvou už nesmí vědět, ze kterého webu data pocházejí.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

CATALOG = "catalog"
FEED = "feed"


@dataclass
class Offer:
    source: str                      # "kinguin", "mydealz", "fly4free", ...
    kind: str                        # CATALOG | FEED
    uid: str                         # product_id nebo guid
    name: str
    price_czk: float
    url: str
    credibility: float = 0.5         # 0–1
    ref_price_czk: float | None = None   # deklarovaná původní cena — SLABÝ signál
    category: str | None = None
    merchant: str | None = None
    currency: str = "CZK"            # původní měna před přepočtem
    price_orig: float | None = None  # částka v původní měně
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.uid)


class Source(Protocol):
    name: str
    kind: str

    def fetch(self) -> list[Offer]:
        """Stáhne aktuální nabídky. Výjimka znamená přeskočení zdroje, ne pád běhu."""
        ...
