"""Převod měn na koruny podle denního kurzu ČNB.

ČNB publikuje kurzy jako prostý textový soubor bez klíče a bez limitů:

    23.07.2026 #141
    země|měna|množství|kód|kurz
    EMU|euro|1|EUR|24,850

Kurz se stahuje jednou denně, mezitím se drží v tabulce `meta`. Když ČNB
nedojede, použije se poslední známý kurz, a teprve když není ani ten, tak
nouzová konstanta.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

log = logging.getLogger(__name__)

CNB_URL = (
    "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
    "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
)

# Nouzové kurzy, kdyby ČNB nejela a v databázi ještě nic nebylo.
FALLBACK = {"EUR": 24.9, "GBP": 28.8, "PLN": 5.8, "USD": 21.5, "CZK": 1.0}

_META_KEY = "fx_rates"


def _parse(text: str) -> dict[str, float]:
    rates: dict[str, float] = {"CZK": 1.0}
    for line in text.splitlines()[2:]:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        try:
            amount = float(parts[2].replace(",", "."))
            code = parts[3].strip().upper()
            rate = float(parts[4].replace(",", "."))
        except ValueError:
            continue
        if amount:
            rates[code] = rate / amount
    return rates


class Fx:
    """Držák kurzů. `to_czk` je jediné, co zbytek kódu potřebuje."""

    def __init__(self, rates: dict[str, float], day: str) -> None:
        self.rates = rates
        self.day = day

    def to_czk(self, amount: float, currency: str) -> float:
        code = (currency or "CZK").upper()
        rate = self.rates.get(code)
        if rate is None:
            rate = FALLBACK.get(code)
        if rate is None:
            raise ValueError(f"Neznámá měna {currency}")
        return amount * rate


def load_fx(http, store) -> Fx:
    today = dt.date.today().isoformat()

    cached = store.get_meta(_META_KEY)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("day") == today:
                return Fx(data["rates"], today)
        except (ValueError, KeyError):
            data = None

    try:
        text = http.get(CNB_URL).text
        rates = _parse(text)
        if "EUR" not in rates:
            raise ValueError("odpověď ČNB neobsahuje EUR")
        store.set_meta(_META_KEY, json.dumps({"day": today, "rates": rates}))
        log.info("Kurzy ČNB načteny (EUR %.3f Kč)", rates["EUR"])
        return Fx(rates, today)
    except Exception as exc:  # noqa: BLE001
        log.warning("Kurzy ČNB se nepodařilo načíst (%s), beru poslední známé", exc)

    if cached:
        try:
            data = json.loads(cached)
            return Fx(data["rates"], data.get("day", "?"))
        except (ValueError, KeyError):
            pass

    log.warning("Používám nouzové kurzy")
    return Fx(dict(FALLBACK), "fallback")
