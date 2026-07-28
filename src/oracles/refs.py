"""Oracle nad ručním ceníkem `references.yaml`.

Řeší dvě věci, které jinak nikdo neumí:

* **Studený start.** První týden nemá katalog žádnou cenovou historii, takže
  bez ceníku by bot neuměl ocenit vůbec nic.
* **Předplatné.** U položky typu "AI Pro > 18 Months" nedává smysl porovnávat
  cenu s cenou; smysl dává vynásobit měsíční hodnotu počtem měsíců. Právě tenhle
  přepočet dělá z Gemini za 65 Kč trhák — 18 měsíců po 490 Kč je 8 820 Kč.
"""

from __future__ import annotations

import re

from ..sources.base import Offer
from .base import Value

# "18 Months", "18-Month", "18 měsíců", "12 Monate", "18 miesięcy"
_MONTHS_RE = re.compile(
    r"(\d{1,3})\s*[-–]?\s*(month|months|měsíc|mesic|měsíců|mesicu|monat|monate|mois|miesi)",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*[-–]?\s*(year|years|rok|roky|let|jahr|jahre|an|ans)\b",
    re.IGNORECASE,
)


def parse_months(name: str) -> int | None:
    """Vytáhne délku předplatného v měsících z názvu položky."""
    if not name:
        return None
    m = _MONTHS_RE.search(name)
    if m:
        months = int(m.group(1))
        return months if 1 <= months <= 120 else None
    y = _YEARS_RE.search(name)
    if y:
        years = int(y.group(1))
        if 1 <= years <= 10:
            return years * 12
    if re.search(r"\b(annual|yearly|ročn|roczn|jährlich)", name, re.IGNORECASE):
        return 12
    return None


# Zkušební verze nemá cenu plného předplatného — dostane ji každý zadarmo.
_TRIAL_RE = re.compile(r"\btrial\b|\bzku[šs]ebn", re.IGNORECASE)


class ReferenceOracle:
    name = "references"

    def __init__(self, rules: list[dict]) -> None:
        self.rules = rules or []

    def value_of(self, offer: Offer) -> Value | None:
        # Ceník obchází práh důvěryhodnosti, takže jde rovnou na mobil. Zkušební
        # verze by tím dostala hodnotu plného předplatného: „Discord Nitro –
        # 3 Months Trial (ONLY FOR NEW ACCOUNTS)" za 12 Kč vyšlo na 1,6 % ze
        # 747 Kč a pinglo by jako trhák. Přitom je to trial, který je zdarma.
        # Ať to ocení AI soudce, nebo ať to zůstane neoceněné.
        if _TRIAL_RE.search(offer.name or ""):
            return None

        haystack = (offer.name or "").lower()
        for rule in self.rules:
            terms = [t.lower() for t in rule.get("match", [])]
            if not terms or not all(t in haystack for t in terms):
                continue

            if "value_czk" in rule:
                return Value(
                    real_value_czk=float(rule["value_czk"]),
                    origin="references",
                    note=f"ceník: {' + '.join(terms)}",
                )

            per_month = rule.get("value_czk_per_month")
            if per_month is None:
                continue

            months = parse_months(offer.name)
            if months is None:
                # Bez délky předplatného bereme jeden měsíc — konzervativní odhad,
                # který spíš podstřelí hodnotu, než aby vyrobil falešný trhák.
                months = 1
                note = f"ceník: {' + '.join(terms)}, délka neurčena → 1 měsíc"
                confidence = 0.6
            else:
                note = f"ceník: {' + '.join(terms)} × {months} měsíců"
                confidence = 1.0

            return Value(
                real_value_czk=float(per_month) * months,
                origin="references",
                note=note,
                confidence=confidence,
            )
        return None
