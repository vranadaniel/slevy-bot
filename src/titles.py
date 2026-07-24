"""Převod názvů produktů na tituly her.

IsThereAnyDeal páruje tituly **přesnou shodou** po vlastním preprocessingu, takže
„Gothic 1 Remake PC Steam CD Key" se sám od sebe netrefí. Tenhle modul z názvu
odstraní obchodní balast a vygeneruje pár kandidátů, ze kterých se zkusí první,
který ITAD zná.

Změřeno na živých datech z Kinguinu: **90 % her se spáruje.** Zbytek jsou hlavně
edice a expanze, které ITAD jako samostatný titul nevede.

Stripování označení edice („Ultimate Edition" → základní hra) je záměrně
konzervativní: základní hra je levnější, takže se nabídka spíš podhodnotí než
aby vyrobila falešný trhák.
"""

from __future__ import annotations

import re

# Platformy, obchody, typy klíčů a regiony — vše, co s názvem hry nesouvisí.
_NOISE = re.compile(
    r"\b("
    r"pc|mac|linux|windows(\s*1[01])?|"
    r"steam|epic games?|epic|uplay|ubisoft connect|origin|ea app|ea play|gog|"
    r"rockstar|battle\.?net|microsoft store|"
    r"xbox(\s*live)?(\s*one)?(\s*series\s*x\|?s?)?(\s*360)?|"
    r"playstation(\s*[45])?|psn|ps[45]|nintendo|switch|"
    r"cd\s*key|key|account|gift|code|voucher|activation|online|oem|retail|"
    r"global|europe|region\s*free|worldwide|digital(\s*download)?|download|"
    r"pre-?order|preorder"
    r")\b",
    re.IGNORECASE,
)
# Regionální zkratky. Schválně BEZ IGNORECASE: Kinguin je píše verzálkami
# ("... EU PC Steam CD Key"), zatímco "Us" v "The Last of Us" je běžné slovo.
# S IGNORECASE tenhle regex ukusoval z názvu her — chyba odhalená na živých datech.
_REGION = re.compile(r"(?:^|\s)(EU|NA|ROW|RoW|RoV|US|UK|LATAM|EMEA)(?=\s|$)")
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_EDITION = re.compile(
    r"\b(deluxe|ultimate|gold|premium|complete|definitive|goty|game of the year|"
    r"standard|enhanced|legendary|collector'?s?|anniversary|special|early access)\b.*$",
    re.IGNORECASE,
)
# Čárka se musí zachovat kvůli titulům jako "Warhammer 40,000".
_KEEP = re.compile(r"[^\w\s:,'&!?.\-–]")
_DANGLING = re.compile(r"\s+(for|the|a|of|and|with|in|on)$", re.IGNORECASE)

_ROMAN = {"2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI", "7": "VII",
          "8": "VIII", "9": "IX", "10": "X"}

# Přídavky k hrám. Pro ty se NIKDY nesmí použít název základní hry — bonus za
# předobjednávku stojí pár korun, ale základní hra tisíce, takže by se z bezcenné
# položky stal zdánlivý trhák za 0,4 % ceny. Chyba odhalená až na ostrých datech.
_ADDON = re.compile(
    r"\b(dlc|pre-?order bonus|preorder bonus|season pass|expansion pass|"
    r"bonus content|booster|xp boost|double xp|battle pass|"
    r"skin|weapon pack|character pack|currency|coins?|points?|credits?|"
    r"gift card|top-?up|membership|subscription)\b",
    re.IGNORECASE,
)

# Náhodné klíče a "mystery" balíčky nemají definovaný obsah, takže je nelze ocenit.
_UNVALUABLE = re.compile(r"\b(mystery|random|lucky|surprise)\b", re.IGNORECASE)


def is_addon(name: str) -> bool:
    """Je to přídavek k hře, ne samostatná hra?"""
    return bool(_ADDON.search(name or ""))


def is_unvaluable(name: str) -> bool:
    """Náhodný klíč nebo mystery balíček — obsah není znám, cena nedává smysl."""
    return bool(_UNVALUABLE.search(name or ""))


def _tidy(text: str) -> str:
    text = _KEEP.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–:,")
    # Předložka osiřelá po odstranění balastu: "Minecraft ... Edition for" → "... Edition"
    while True:
        stripped = _DANGLING.sub("", text).strip(" -–:,")
        if stripped == text:
            return stripped
        text = stripped


def candidates(name: str) -> list[str]:
    """Kandidátní tituly seřazené od nejkonkrétnějšího. Zkouší se v tomhle pořadí."""
    if not name or is_unvaluable(name):
        return []

    base = _BRACKETS.sub(" ", name)

    # Pozor na pořadí: "+" znamená balíček, kde hlavní produkt je ta hra vepředu
    # ("DOOM: The Dark Ages + Pre-Order Bonus DLC"), zatímco u "-" bývá produktem
    # sám přídavek ("EA Sports FC 24 - Pre-order Bonus DLC"). Balíček se proto
    # musí rozdělit dřív, než se ptáme, jestli jde o přídavek.
    base = base.split(" + ")[0]

    # U přídavků se hledá POUZE jejich plný název. Žádné zkracování na základní
    # hru, žádné odstraňování edice — když ITAD zrovna tenhle přídavek nezná,
    # je správná odpověď "neumím ocenit", ne cena celé hry.
    if is_addon(base):
        cleaned = _tidy(_NOISE.sub(" ", _REGION.sub(" ", base)))
        return [cleaned] if len(cleaned) > 2 else []

    base = base.split(" - ")[0]          # "Hra - Sezónní vstupenka" → "Hra"
    base = _REGION.sub(" ", base)
    cleaned = _tidy(_NOISE.sub(" ", base))

    out = [cleaned]

    # Nejsilnější heuristika: název hry stojí vždy vepředu, obchodní balast až za ním.
    # Uříznutí u prvního balastního slova zachrání případy, kde uprostřed zbyde
    # smetí — "Red Dead Redemption 2 Epic Games Green Gift Redemption Code"
    # by jinak skončilo jako "Red Dead Redemption 2 Green Redemption".
    first_noise = _NOISE.search(base)
    if first_noise:
        prefix = _tidy(base[: first_noise.start()])
        if len(prefix) > 2:
            out.insert(0, prefix)

    # Varianta bez označení edice — základní hra bývá v ITAD vedená vždy.
    # Tenhle směr je bezpečný: základní hra je levnější, takže se nabídka
    # spíš podhodnotí, než aby vyrobila falešný trhák.
    without_edition = _tidy(_EDITION.sub("", cleaned))
    if without_edition and without_edition != cleaned:
        out.append(without_edition)

    # "Part 2" ↔ "Part II": ITAD u některých sérií drží římské číslice.
    for variant in list(out):
        roman = _to_roman(variant)
        if roman and roman != variant:
            out.append(roman)

    return [c for c in dict.fromkeys(out) if len(c) > 2]


def _to_roman(title: str) -> str | None:
    def repl(match: re.Match) -> str:
        return match.group(1) + _ROMAN[match.group(2)]

    converted = re.sub(r"\b(Part |Episode |Chapter )(\d{1,2})\b",
                       lambda m: repl(m) if m.group(2) in _ROMAN else m.group(0),
                       title, flags=re.IGNORECASE)
    return converted if converted != title else None
