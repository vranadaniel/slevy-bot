"""Travelpayouts (data Aviasales) — třetí katalogový zdroj u cestování.

Proč stojí za to i vedle Ryanairu a Wizz Airu: ti dva umějí jen svoje vlastní
linky, tedy nízkonákladovou Evropu. Tenhle zdroj vidí napříč dopravci včetně
přestupů, takže dosáhne i na dálkové trasy — a právě tam ceník `flights.yaml`
odhaduje nejhůř a rozptyl cen je největší.

Hlavní věc je dotaz **bez cílové stanice**: „nejlevnější letenky z Prahy
kamkoliv". Přesně tuhle otázku si klade člověk hledající levnou dovolenou
a žádný jiný náš zdroj na ni neodpovídá — u Wizz Airu bylo ověřeno, že obdobu
nemá, a Ryanair ji zvládne jen v rámci vlastní sítě.

**Token se předává v hlavičce `X-Access-Token`, ne v URL.** Travelpayouts umí
obojí, ale klíč v query stringu skončí v logu proxy i v historii serveru.
Zdroj bez `TRAVELPAYOUTS_TOKEN` tiše mlčí, stejně jako ITAD bez klíče.

**Odpověď nebyla ověřena živě**, protože bez tokenu vrací 401. Mapování polí
vychází z dokumentace, ne z měření — v tomhle projektu je to výjimka. Parser
je proto psaný tolerantně: co nesedí, přeskočí se, a nikdy to neshodí běh.
První běh s ostrým tokenem projeď přes `--check-travelpayouts`, který vypíše
syrová jména polí vedle toho, co z nich zdroj složil.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from ..sources.base import CATALOG, Offer

log = logging.getLogger(__name__)

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES = "https://www.aviasales.com"


class TravelpayoutsSource:
    name = "travelpayouts"
    kind = CATALOG

    def __init__(self, http, fx, cfg, store=None) -> None:
        self.http = http
        self.fx = fx
        self.store = store
        self.token = cfg.travelpayouts_token
        self.airports: list[str] = cfg.get("sources.travelpayouts.airports", []) or []
        # Určuje MĚSÍC odletu, na který se ptáme. Horní mez tu být nemůže —
        # API nebere okno, ale konkrétní termín, viz `_params`.
        self.days_from = int(cfg.get("sources.travelpayouts.days_from", 21))
        self.limit = int(cfg.get("sources.travelpayouts.limit", 100))
        self.max_transfers = int(cfg.get("sources.travelpayouts.max_transfers", 1))
        self.delay_s = float(cfg.get("sources.travelpayouts.delay_s", 1.0))

    # ---------- dotaz ----------

    def _params(self, origin: str) -> dict:
        """Dotaz na nejlevnější cíle z jednoho letiště.

        `departure_at` je **měsíc** (`2026-08`), ne konkrétní datum. Původně tu
        byl rozsah `departure_at` + `return_at` vzdálený půl roku, na což API
        odpovědělo `400` — nejsou to meze okna, ale skutečné termíny letu.

        Bez `destination` vrátí nejlevnější cíle, což je celý smysl zdroje.
        """
        mesic = (dt.date.today() + dt.timedelta(days=self.days_from)).strftime("%Y-%m")
        return {
            "origin": origin,
            "departure_at": mesic,
            "currency": "czk",
            "sorting": "price",
            "one_way": "true",
            "limit": self.limit,
            "page": 1,
        }

    def fetch(self) -> list[Offer]:
        if not self.token:
            log.info("Travelpayouts: chybí TRAVELPAYOUTS_TOKEN, zdroj přeskakuji")
            return []

        # Token do hlavičky, ne do URL — v query stringu by skončil v logách.
        headers = {"X-Access-Token": self.token, "Accept": "application/json"}
        offers: dict[str, Offer] = {}

        for origin in self.airports:
            try:
                data = self.http.get_json(API, params=self._params(origin),
                                          headers=headers)
            except Exception as exc:  # noqa: BLE001 — jedno letiště neshodí zdroj
                log.warning("Travelpayouts: letiště %s selhalo: %s", origin, exc)
                continue

            for row in (data or {}).get("data") or []:
                offer = self._to_offer(origin, row)
                if offer is None:
                    continue
                # Tutéž trasu vrátí API v několika termínech; bereme nejlevnější,
                # protože o to při lovu levné letenky jde.
                stavajici = offers.get(offer.uid)
                if stavajici is None or offer.price_czk < stavajici.price_czk:
                    offers[offer.uid] = offer

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("Travelpayouts: %s tras", len(offers))
        return list(offers.values())

    # ---------- převod ----------

    def _to_offer(self, origin: str, row: dict) -> Offer | None:
        """Jedna nabídka z odpovědi.

        Tolerantně: co nesedí, to se přeskočí. Odpověď nebyla ověřena živě,
        takže výjimka kvůli přejmenovanému poli by uzemnila celý zdroj.
        """
        try:
            dest = str(row.get("destination") or "").strip().upper()
            cena = row.get("price")
            if not dest or cena in (None, "", 0):
                return None
            cena = float(cena)
            if cena <= 0:
                return None
        except (TypeError, ValueError):
            return None

        # Přestupy: dvojka a víc bývá levná na papíře a nepoužitelná v praxi.
        try:
            prestupy = int(row.get("transfers") or 0)
        except (TypeError, ValueError):
            prestupy = 0
        if prestupy > self.max_transfers:
            return None

        odkud = str(row.get("origin") or origin).strip().upper()
        odkaz = str(row.get("link") or "").strip()

        return Offer(
            source=self.name,
            kind=CATALOG,
            # Uid je TRASA, ne termín — jinak by se cenová historie nenasbírala.
            # Tentýž důvod jako u Ryanairu a Wizz Airu.
            uid=f"{odkud}-{dest}",
            name=f"Letenky z {_MESTA.get(odkud, odkud)} do {dest} (jednosměrné"
                 + (f", {prestupy} přestup)" if prestupy else ", přímý let)"),
            price_czk=self.fx.to_czk(cena, "CZK"),
            price_orig=cena,
            currency="CZK",
            # Agregátor „původní cenu" neuvádí a je to tak dobře — tuhle roli
            # zastane vlastní historie, viz `catalog_history_only` v config.yaml.
            ref_price_czk=None,
            url=f"{AVIASALES}{odkaz}" if odkaz.startswith("/") else (odkaz or AVIASALES),
            category="flight",
            merchant="aviasales",
            # Cena od agregátora, na které staví vlastní prodej. Nikdo si ji
            # nevymýšlí, ale je to zprostředkovatel, ne dopravce — proto o kousek
            # níž než Ryanair a Wizz Air s jedničkou.
            credibility=0.95,
            extra={
                "airport": odkud,
                "destination": dest,
                "outbound": row.get("departure_at"),
                "inbound": row.get("return_at"),
                "transfers": prestupy,
                "airline": row.get("airline"),
            },
        )


_MESTA = {
    "PRG": "Prahy", "BRQ": "Brna", "OSR": "Ostravy",
    "PED": "Pardubic", "VIE": "Vídně", "BTS": "Bratislavy",
}
