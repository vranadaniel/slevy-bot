"""Parsování cen z volného textu.

Pepper feedy míchají německý, britský, francouzský a polský zápis čísel v jednom
kódu, takže "1.259,29€" i "£1,259.29" znamenají totéž. Oddělovač desetin se
pozná tak, že je to ten poslední z dvojice tečka/čárka.
"""

from __future__ import annotations

import re

CURRENCY_SYMBOLS = {
    "€": "EUR",
    "£": "GBP",
    "zł": "PLN",
    "PLN": "PLN",
    "$": "USD",
    "Kč": "CZK",
    "CZK": "CZK",
}

# Číslo s volitelnými oddělovači tisíců a desetin, se symbolem měny před nebo za ním.
_PRICE_RE = re.compile(
    r"(?P<pre>[€£$]|zł|Kč)?\s*"
    r"(?P<num>\d{1,3}(?:[.\s ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s*(?P<post>[€£$]|zł|Kč|EUR|GBP|PLN|CZK|USD)?",
    re.IGNORECASE,
)

# Klíčová slova, za kterými následuje původní cena. Přesnější než "vezmi největší
# číslo v textu" — v titulcích se válí spousta čísel, která cenou nejsou.
_ORIGINAL_HINTS = re.compile(
    r"(statt|uvp|vorher|idealo|regulär|regulaer"          # DE
    r"|was|rrp|instead of|usually|normally"               # UK
    r"|au lieu de|prix conseill|initialement"             # FR
    r"|zamiast|cena regularna|regularnie"                 # PL
    r"|místo|běžně|původn)"                               # CZ
    r"\s*:?\s*",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(r"-?\s*(\d{1,2})\s*%\s*(?:off|rabatt|réduction|zniżka|sleva)?",
                         re.IGNORECASE)


def _to_float(num: str) -> float | None:
    """'1.259,29' -> 1259.29;  '1,259.29' -> 1259.29;  '1 259' -> 1259."""
    s = num.replace(" ", "").replace(" ", "")
    has_dot, has_comma = "." in s, "," in s

    if has_dot and has_comma:
        # Oddělovač desetin je ten, který je v řetězci později.
        dec = "," if s.rindex(",") > s.rindex(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif has_comma:
        # Čárka je desetinná jen když za ní zbývají nejvýš dvě číslice.
        s = s.replace(",", ".") if len(s.split(",")[-1]) <= 2 else s.replace(",", "")
    elif has_dot:
        s = s if len(s.split(".")[-1]) <= 2 else s.replace(".", "")

    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: str, default_currency: str = "EUR",
                require_symbol: bool = True) -> tuple[float, str] | None:
    """Vytáhne první cenu z textu. Vrací (částka, kód měny).

    Symbol měny je ve výchozím stavu povinný. Bez něj by regex bral jakékoliv
    číslo — z titulku "modern 4 hotel ... from €34" by vypadla cena 4 místo 34.
    """
    if not text:
        return None
    for m in _PRICE_RE.finditer(text):
        symbol = m.group("pre") or m.group("post")
        if require_symbol and not symbol:
            continue
        value = _to_float(m.group("num"))
        if value is None:
            continue
        currency = CURRENCY_SYMBOLS.get(
            symbol, symbol.upper() if symbol else default_currency
        )
        return value, currency
    return None


def find_all_prices(text: str, default_currency: str = "EUR") -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for m in _PRICE_RE.finditer(text or ""):
        value = _to_float(m.group("num"))
        if value is None:
            continue
        symbol = m.group("pre") or m.group("post")
        if not symbol:
            continue  # holé číslo bez měny cenou být nemusí
        out.append((value, CURRENCY_SYMBOLS.get(symbol, symbol.upper())))
    return out


def find_original_price(text: str, default_currency: str = "EUR") -> tuple[float, str] | None:
    """Hledá původní cenu podle klíčových slov typu 'statt', 'RRP', 'zamiast'."""
    if not text:
        return None
    for hint in _ORIGINAL_HINTS.finditer(text):
        window = text[hint.end(): hint.end() + 30]
        found = parse_price(window, default_currency)
        if found and found[0] > 0:
            return found
    return None


def find_discount_percent(text: str) -> int | None:
    """Nejvyšší procento slevy zmíněné v textu, pokud dává smysl."""
    best: int | None = None
    for m in _PERCENT_RE.finditer(text or ""):
        pct = int(m.group(1))
        if 5 <= pct <= 99 and (best is None or pct > best):
            best = pct
    return best
