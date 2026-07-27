"""Porovnávání textu napříč češtinou a angličtinou.

Ceníky se hledají jako podřetězec v názvu nabídky, což u češtiny naráží na dvě
věci najednou:

* **Skloňování mění i souhlásku.** „Boloňa" se skloňuje na „v Boloni", „do
  Boloně" — `ň` se před měkkým `ě`/`i` mění na `n`. Výraz `boloň` tedy
  v titulku „Do Boloně" není, i když jde o totéž město.
* **Zdroje píšou diakritiku různě.** „Vídeň" i „Viden", „Řím" i „Rim".

Obojí spolehlivě řeší srovnání bez diakritiky: `boloň` i `boloně` se složí na
`bolon…` a shoda vyjde. Zbývá jen psát v ceníku kmeny, ne první pády — `menork`
místo `menorka`, ať sedí i „na Menorku".
"""

from __future__ import annotations

import re
import unicodedata

_NEALFA = re.compile(r"[^0-9a-z]+")


def fold(text: str) -> str:
    """Malá písmena bez diakritiky a bez interpunkce: 'Do Boloně!' -> 'do bolone'.

    Skládá se přes NFKD a zahazují se kombinující znaky. Zdroje občas posílají
    rozloženou diakritiku, takže normalizace musí proběhnout tak jako tak.

    Interpunkce se nahrazuje mezerou, aby šlo v ceníku psát výrazy s hranicí
    slova. Bez toho by `"rim "` nesedělo na „Praha – Řím." a ochrana proti
    slovu „přímý" by v půlce případů selhala.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NEALFA.sub(" ", stripped).strip()


def fold_term(term: str) -> str:
    """Výraz z ceníku. Na rozdíl od `fold` zachová vodicí mezery.

    Právě jimi se v ceníku vyznačuje hranice slova — `"rim "` je Řím, kdežto
    bez mezery by to byl i kus slova „Rimini". `fold` mezery ořezává, takže
    ochrana musí projít tudy.
    """
    core = fold(term)
    lead = " " if term[:1].isspace() else ""
    trail = " " if term[-1:].isspace() else ""
    return f"{lead}{core}{trail}"


def haystack(text: str) -> str:
    """Text připravený k hledání výrazů, obalený mezerami.

    Obalení je to, co dělá hranici slova použitelnou i na začátku a na konci:
    výraz `"rim "` pak sedne i na titulek končící „…Řím"."""
    return f" {fold(text)} "
