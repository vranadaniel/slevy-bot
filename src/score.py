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
        self.by_category: dict = cfg.get("thresholds.by_category", {}) or {}
        self.min_cred_instant = float(cfg.get("thresholds.min_credibility_instant", 0.5))
        self.min_cred_ai = float(cfg.get("thresholds.min_credibility_ai", 0.8))
        self.min_cred_ai_shipping_ok = float(
            cfg.get("thresholds.min_credibility_ai_shipping_ok", self.min_cred_ai))
        self.require_shipping = bool(cfg.get("thresholds.require_ships_to_cz_for_instant", True))
        # Hry: ITAD sám okamžité upozornění nespouští, viz komentář v _finalize.
        self.itad_digest = float(cfg.get("itad.digest_factor", 1.0))

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
            if self._history_only(offer):
                continue

            if offer.kind == CATALOG:
                claimed = offer.extra.get("claimed_discount") or 0
                # Jen špička žebříčku prodejnosti s deklarovanou extrémní slevou.
                if offer.credibility >= 0.7 and claimed >= 90:
                    candidates.append((offer.credibility * claimed, offer))
                elif verdict.all_time_low and offer.credibility >= 0.7:
                    candidates.append((offer.credibility * 100, offer))
            elif offer.credibility >= self._ai_bar(verdict):
                # U feedů rozhoduje důvěryhodnost, ať už vznikla z komunitní
                # teploty, nebo z redakčního výběru. Letenku ani hotel žádný
                # levný oracle ocenit neumí — bez téhle větve by cestování
                # mlčelo vždycky.
                candidates.append((offer.credibility * 100, offer))

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return [offer for _, offer in candidates[:limit]]

    def _ai_bar(self, verdict: Verdict) -> float:
        """Od jaké důvěryhodnosti stojí feedová položka za dotaz na AI.

        Nabídka, u které **víme, že se dá koupit do Česka**, si zaslouží nižší
        laťku než ta, u které to nevíme. Ověřeno na živých datech Pepperu:
        ze 107 nabídek jich 53 pochází z obchodů s potvrzeným doručením, ale
        jen 8 má v textu původní cenu. Zbytek nikdo neocení a při jednotném
        prahu 0,8 (což je 400° na Pepperu) se k soudci nedostane — proto z něj
        chodily dvě zprávy na sto nabídek.

        Cena je malá: feedy se deduplikují podle guid, takže každý příspěvek
        jde k AI **jednou za život**, ne při každém běhu.
        """
        if verdict.ships_to_cz is True:
            return self.min_cred_ai_shipping_ok
        return self.min_cred_ai

    def apply_ai(self, verdicts: list[Verdict], values: dict[str, Value]) -> None:
        for verdict in verdicts:
            value = values.get(verdict.offer.uid)
            if value is None:
                continue
            verdict.value = value
            verdict.ships_to_cz = self.shipping.ships_to_cz(verdict.offer)
            self._finalize(verdict)

    # ---------- rozhodnutí ----------

    def _policy_for(self, offer: Offer) -> dict:
        """Pravidla podle druhu zboží. Klíčem je `offer.category`, tedy povaha
        zboží — ne zdroj, ze kterého nabídka přišla."""
        category = (offer.category or "").lower()
        for key, limits in self.by_category.items():
            if key.lower() in category:
                return limits
        return {}

    def _thresholds_for(self, offer: Offer) -> tuple[float, float]:
        """Prahy podle druhu zboží.

        Letenka za 5 % běžné ceny neexistuje. I error fare bývá „jen" o 40–60 %
        pod cenou, a to je u letenek trhák, o kterém se mluví roky. Jednotný
        práh nastavený na digitální klíče by cestování umlčel vždycky.
        """
        limits = self._policy_for(offer)
        return (float(limits.get("instant_ratio", self.instant_ratio)),
                float(limits.get("digest_ratio", self.digest_ratio)))

    def _history_only(self, offer: Offer) -> bool:
        """Smí tuhle katalogovou položku ocenit jen její vlastní historie?

        U her je odhad zvenčí smysluplný — mají doporučenou cenu, kterou zná
        i AI. U letenky nic takového neexistuje: cena trasy závisí na termínu,
        sezóně a poptávce, takže „běžná cena" je vždycky jen dohad. A dohad
        proti ceníku dopravce je přesně ten kruh, kvůli kterému bot hlásil
        obyčejné letenky jako trháky.
        """
        return offer.kind == CATALOG and bool(self._policy_for(offer).get("catalog_history_only"))

    def _reference_needs_history(self, verdict) -> bool:
        """Musí ruční ceník u téhle položky počkat na vlastní historii?

        Ceník nese ceníkovou cenu výrobce, a ta u licencí a předplatného na
        šedém trhu nikdy neplatí. Změřeno na živých datech: Windows 10 Pro má
        v ceníku 4 500 Kč a na Kinguinu se prodává za 242 Kč, AVG Ultimate
        5 400 proti 774. Pravidlo pak pálí při KAŽDÉM pozorování a to není
        signál — u 22 pravidel ze 49 by položka prošla i za svou úplně běžnou
        cenu.

        Snížit sazby by znamenalo kalibrovat ceník podle trhu, který zrovna
        posuzujeme — tentýž kruh, kvůli kterému bot hlásil Krakov za 748 Kč
        jako trhák. Správná odpověď je stejná jako u her a ITAD: **jakmile má
        položka vlastní historii, rozhoduje ona.** Ceník zůstává na zobrazení
        hodnoty a na studený start.

        Podmínkou je historické minimum, ne práh v procentech. Je to
        bezparametrové, používá to `is_all_time_low`, které tu už je, a
        odpovídá to na otázku „je zrovna teď dobrá chvíle to koupit".
        """
        if verdict.value.origin != "references":
            return False
        if verdict.offer.kind != CATALOG:
            return False
        return self.history.has_history(verdict.offer)


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

        # --- rozhodnutí o úrovni ---
        itad_low = offer.extra.get("itad_low_czk")
        if verdict.value.origin == "itad":
            # Hry na šedém trhu neumí ITAD rozsoudit, a to ani jedním měřítkem.
            # Poměr k doporučené ceně je u nich vždycky extrémní (2–5 %), ale
            # levnější než historické minimum oficiálních obchodů je zhruba
            # třetina nabídek — Kinguin prodává regionální klíče pod cenami,
            # na které oficiální obchody nikdy nejdou. Obojí změřeno na živých
            # datech: jako spouštěč to dávalo 43, resp. 153 zpráv v jednom běhu.
            #
            # Okamžité upozornění u her proto spouští až vlastní cenová historie,
            # která šedý trh odráží. Do té doby slouží ITAD k zobrazení hodnoty
            # a k utišení nabídek, které ani oficiální minimum nepodlezou.
            qualifies_instant = False
            qualifies_digest = bool(itad_low) and \
                offer.price_czk <= itad_low * self.itad_digest
            if qualifies_digest:
                shop = offer.extra.get("itad_shop")
                verdict.reasons.append(
                    f"pod historickým minimem "
                    f"({itad_low:.0f} Kč{f' na {shop}' if shop else ''})"
                )
        else:
            instant_ratio, digest_ratio = self._thresholds_for(offer)
            qualifies_digest = ratio <= digest_ratio

            # Když ceník sám řekne, kde leží skvělá cena, věříme mu víc než
            # jednotnému poměru. U letenek se totiž ta hranice liší podle
            # světadílu: skvělá cena do Evropy je 36 % běžné, do jihovýchodní
            # Asie 62 %. Jedno číslo pro obojí musí jednu stranu odbýt.
            instant_below = offer.extra.get("instant_below_czk")
            if instant_below:
                qualifies_instant = offer.price_czk <= instant_below
                if qualifies_instant:
                    verdict.reasons.append(
                        f"pod hranicí skvělé ceny ({instant_below:.0f} Kč)")
            else:
                qualifies_instant = ratio <= instant_ratio

            # Vlastní historie je natolik důvěryhodná, že hluboký propad na
            # historické minimum stojí za okamžitou zprávu i bez extrémního poměru.
            if (not qualifies_instant and verdict.all_time_low
                    and verdict.value.origin == "history" and ratio <= 0.5):
                qualifies_instant = True
                verdict.reasons.append("propad na historické minimum")

            # Ceníková cena výrobce u licencí na šedém trhu nikdy neplatí,
            # takže by pravidlo pálilo pořád. Jakmile má položka vlastní
            # historii, musí ceníku dát za pravdu i ona.
            if self._reference_needs_history(verdict):
                if verdict.all_time_low:
                    verdict.reasons.append("a zároveň historicky nejnižší cena")
                else:
                    qualifies_instant = False
                    qualifies_digest = False
                    verdict.reasons.append(
                        "ceníková sleva, ale ne proti vlastní historii")

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

        verdict.level = DIGEST if qualifies_digest else NONE
