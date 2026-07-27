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

import unicodedata


def fold(text: str) -> str:
    """Malá písmena bez diakritiky: 'Do Boloně' -> 'do bolone'.

    Skládá se přes NFKD a zahazují se kombinující znaky. Zdroje občas posílají
    rozloženou diakritiku, takže normalizace musí proběhnout tak jako tak.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
