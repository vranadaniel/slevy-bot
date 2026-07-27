"""Oracle nad ručním ceníkem letenek (`flights.yaml`).

Do téhle chvíle uměl letenku ocenit jedině AI soudce, a to odhadem z hlavy.
Fungovalo to, ale mělo dvě vady: stálo to peníze při každém dotazu a u téže
trasy mohl model odpovědět dnes jinak než zítra.

Ceník má na region dvě čísla:

* ``typical_czk`` — běžná cena zpáteční letenky v běžném období. Slouží jako
  hodnota, ze které se počítá `value_ratio`.
* ``great_czk``   — cena, o které cestovatelské weby píšou jako o výborné
  nabídce. Předává se dál jako `instant_below_czk`.

Proč to druhé číslo vůbec je: **jednotný poměr napříč světem nefunguje.**
Skvělá cena do Evropy leží na 36 % běžné ceny, do jihovýchodní Asie na 62 %.
Práh nastavený na Evropu by dálkové lety umlčel, práh nastavený na dálkové
lety by z Evropy posílal běžné ceny. Ceník proto říká rovnou částku.

Ceník platí jen pro **samotné letenky**. Zájezd (`category == "hotel"`) v sobě
má i ubytování a stravu, takže cena za letenku o něm nevypovídá — ten ať radši
ocení AI soudce, nebo zůstane neoceněný.
"""

from __future__ import annotations

import logging

from ..sources.base import Offer
from ..text import fold
from .base import Value

log = logging.getLogger(__name__)


class FlightOracle:
    name = "flights"

    def __init__(self, regions: list[dict]) -> None:
        # Nejdřív ty vzdálenější. Když se titulek trefí do dvou regionů,
        # vyhrává ten dražší — viz `_match`.
        self.regions = sorted(regions or [],
                              key=lambda r: r.get("typical_czk", 0),
                              reverse=True)
        # Výrazy se skládají jednou při startu, ne u každé nabídky znovu.
        self._terms = {
            r["name"]: [fold(t) for t in r.get("match", [])] for r in self.regions
        }

    def value_of(self, offer: Offer) -> Value | None:
        if offer.category != "flight":
            return None

        region = self._match(offer)
        if region is None:
            return None

        typical = float(region.get("typical_czk") or 0)
        if typical <= 0:
            return None

        great = region.get("great_czk")
        if great:
            # Skvělá cena je u každého regionu jinde, takže ji nese ceník,
            # ne jednotný práh ve `score.py`.
            offer.extra["instant_below_czk"] = float(great)
        offer.extra["flight_region"] = region["name"]

        return Value(
            real_value_czk=typical,
            origin="flights",
            note=f"ceník letenek: {region.get('label', region['name'])}, "
                 f"běžně ~{typical:.0f} Kč",
            # Ceník je poctivý odhad z reálných nabídek, ne měřená cena
            # konkrétního termínu. Nižší jistota než u vlastní historie.
            confidence=0.7,
        )

    def _match(self, offer: Offer) -> dict | None:
        """Region podle názvu nabídky.

        Zdroje píšou i výchozí město („from Milan to PHUKET"), takže se titulek
        běžně trefí do dvou regionů. Bere se ten vzdálenější: u těchhle webů je
        cíl skoro vždycky ta exotičtější půlka a evropské město bývá přestup.
        Regiony jsou seřazené od nejdražšího, takže stačí první shoda.

        Porovnává se bez diakritiky — viz `text.fold`. České skloňování totiž
        mění i souhlásku a „Boloňa" se v titulku objeví jako „do Boloně".
        """
        haystack = fold(" ".join([
            offer.name,
            " ".join(offer.extra.get("categories") or []),
        ]))
        for region in self.regions:
            if any(term in haystack for term in self._terms[region["name"]]):
                return region
        return None
