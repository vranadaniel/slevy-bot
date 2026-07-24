"""Jádro projektu: rozhodnutí, jestli je nabídka trhák.

Vše se sbíhá do jednoho čísla — **`value_ratio` = zaplatíš / reálná hodnota.**
Gemini AI Pro na 18 měsíců za 65 Kč má poměr 0,007, tedy necelé procento
skutečné ceny. Přesně tohle má bot hledat.

Past, kterou to musí ustát: procento samo o sobě nestačí. „95 % sleva" na
bezcennou věc je pořád bezcenná věc, a marketplace jsou plné šuntu s vymyšlenou
původní cenou. Proto každá položka nese `credibility` — signál, který prodejce
neovlivní (prodejnost na Kinguinu, komunitní teplota na Pepperu, redakční výběr
na fly4free). Nízká důvěryhodnost položku nezahodí, jen jí zavře cestu
k okamžitému upozornění.

Trychtýř je záměrný: drahé kroky běží až na hrstce položek, které přežily levné.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .oracles.base import Value
from .oracles.refs import parse_months
from .sources.base import CATALOG, Offer

log = logging.getLogger(__name__)

INSTANT = "instant"
DIGEST = "digest"
NONE = "none"


@dataclass
class Verdict:
    offer: Offer
    level: str = NONE
    value: Value | None = None
    value_ratio: float | None = None
    ships_to_cz: bool | None = None
    all_time_low: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def discount_pct(self) -> float | None:
        if self.value_ratio is None:
            return None
        return (1 - self.value_ratio) * 100


class Scorer:
    def __init__(self, cfg, store, oracles: list, history_oracle, shipping) -> None:
        self.cfg = cfg
        self.store = store
        self.oracles = oracles
        self.history = history_oracle
        self.shipping = shipping

        self.instant_ratio = float(cfg.get("thresholds.instant_ratio", 0.05))
        self.digest_ratio = float(cfg.get("thresholds.digest_ratio", 0.20))
        self.min_cred_instant = float(cfg.get("thresholds.min_credibility_instant", 0.5))
        self.require_shipping = bool(cfg.get("thresholds.require_ships_to_cz_for_instant", True))

    # ---------- první průchod, bez AI ----------

    def prescore(self, offers: list[Offer]) -> list[Verdict]:
        verdicts = []
        for offer in offers:
            verdict = Verdict(offer=offer)
            verdict.ships_to_cz = self.shipping.ships_to_cz(offer)

            if verdict.ships_to_cz is False:
                verdict.reasons.append("obchod neposílá do ČR")
                verdicts.append(verdict)
                continue

            if offer.kind == CATALOG:
                verdict.all_time_low = self.history.is_all_time_low(offer)

            for oracle in self.oracles:
                value = oracle.value_of(offer)
                if value is not None:
                    verdict.value = value
                    break

            self._finalize(verdict)
            verdicts.append(verdict)
        return verdicts

    # ---------- výběr pro AI soudce ----------

    def ai_candidates(self, verdicts: list[Verdict], limit: int) -> list[Offer]:
        """Položky, které levné oracles neocenily, ale vypadají slibně.

        Záměrně úzké síto — AI stojí peníze a většina katalogu je nezajímavá.
        Propouští se jen to, co má silnou důvěryhodnost a náznak výrazné slevy.
        """
        candidates: list[tuple[float, Offer]] = []

        for verdict in verdicts:
            if verdict.value is not None or verdict.ships_to_cz is False:
                continue
            offer = verdict.offer

            if offer.kind == CATALOG:
                claimed = offer.extra.get("claimed_discount") or 0
                # Jen špička žebříčku prodejnosti s deklarovanou extrémní slevou.
                if offer.credibility >= 0.7 and claimed >= 90:
                    candidates.append((offer.credibility * claimed, offer))
                elif verdict.all_time_low and offer.credibility >= 0.7:
                    candidates.append((offer.credibility * 100, offer))
            else:
                # U feedů je teplota to jediné, co o kvalitě něco říká.
                temperature = offer.extra.get("temperature") or 0
                if temperature >= 400 or offer.extra.get("error_fare"):
                    candidates.append((float(temperature or 500), offer))

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return [offer for _, offer in candidates[:limit]]

    def apply_ai(self, verdicts: list[Verdict], values: dict[str, Value]) -> None:
        for verdict in verdicts:
            value = values.get(verdict.offer.uid)
            if value is None:
                continue
            verdict.value = value
            verdict.ships_to_cz = self.shipping.ships_to_cz(verdict.offer)
            self._finalize(verdict)

    # ---------- rozhodnutí ----------

    def _finalize(self, verdict: Verdict) -> None:
        offer = verdict.offer
        verdict.reasons = []

        if verdict.value is None or verdict.value.real_value_czk <= 0:
            verdict.level = NONE
            verdict.reasons.append("nepodařilo se určit reálnou hodnotu")
            return

        ratio = offer.price_czk / verdict.value.real_value_czk
        verdict.value_ratio = ratio

        if verdict.value.note:
            verdict.reasons.append(verdict.value.note)
        if verdict.all_time_low:
            verdict.reasons.append("historicky nejnižší cena")

        # U předplatného je cena za měsíc to, co dělá z nabídky trhák.
        months = parse_months(offer.name)
        if months and months > 1:
            verdict.reasons.append(f"{offer.price_czk / months:.0f} Kč za měsíc")

        # --- okamžité upozornění ---
        qualifies_instant = ratio <= self.instant_ratio

        # Vlastní historie je natolik důvěryhodná, že hluboký propad na historické
        # minimum stojí za okamžitou zprávu i bez extrémního poměru.
        if (not qualifies_instant and verdict.all_time_low
                and verdict.value.origin == "history" and ratio <= 0.5):
            qualifies_instant = True
            verdict.reasons.append("propad na historické minimum")

        if qualifies_instant:
            # Práh důvěryhodnosti hlídá nedůvěryhodné OCENĚNÍ, ne položku samotnou.
            # Když hodnota přišla z ručního ceníku nebo z vlastní historie, víme,
            # co ta věc stojí — a je jedno, kolikátá je v žebříčku prodejnosti.
            trusted_valuation = (
                verdict.value.origin in ("references", "history")
                and verdict.value.confidence >= 0.9
            )
            if not trusted_valuation and offer.credibility < self.min_cred_instant:
                verdict.level = DIGEST
                verdict.reasons.append("nízká důvěryhodnost položky → jen do souhrnu")
                return
            if self.require_shipping and verdict.ships_to_cz is None:
                verdict.level = DIGEST
                verdict.reasons.append("doručení do ČR neověřeno → jen do souhrnu")
                return
            verdict.level = INSTANT
            return

        verdict.level = DIGEST if ratio <= self.digest_ratio else NONE
