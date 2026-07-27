"""Wizz Air — druhý katalogový zdroj u cestování.

Stejný princip jako u Ryanairu: tutéž trasu vidíme opakovaně, takže se dá stavět
vlastní cenová historie. Ceník se dá zpochybnit, vlastní měření ne.

Ověřeno živě 27. 7. 2026:

* `GET  /Api/asset/map` — mapa linek. Z našich letišť jede **20 tras z Prahy
  a 38 z Bratislavy**. Z Vídně Wizz Air podle mapy nelétá; Ryanair to naopak
  pokrývá, takže se ty dva zdroje doplňují.
* `POST /Api/asset/farechart` — ceny po dnech, rovnou v korunách. Vyžaduje
  `dayInterval` **aspoň 3**, jinak vrátí `DayIntervalMustBeGreaterOrEqualTo3`.
  Při `dayInterval: 3` odpoví sedmi dny kolem zadaného data.

Verze API je v cestě (`be.wizzair.com/29.8.0/…`) a čas od času se zvedne.
Zjišťuje se proto z jejich webu, ne natvrdo z konfigurace — viz `_verze`.

**Trasy se střídají mezi běhy.** Zeptat se na všech 58 při každém běhu by
znamenalo 58 požadavků každých deset minut, tedy přes osm tisíc denně. To je
zbytečné zatížení a slušnost velí jinak. Cena letenky se navíc mezi dvěma běhy
skoro nikdy nezmění, takže stačí projít celý seznam za pár hodin.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

from ..sources.base import CATALOG, Offer

log = logging.getLogger(__name__)

BASE = "https://be.wizzair.com"
WEB = "https://wizzair.com/"
_VERZE_RE = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)")
FALLBACK_VERZE = "29.8.0"

# Minimum, které API vyžaduje; menší hodnotu odmítne validací.
MIN_DAY_INTERVAL = 3


class WizzAirSource:
    name = "wizzair"
    kind = CATALOG

    def __init__(self, http, fx, cfg, store=None) -> None:
        self.http = http
        self.fx = fx
        self.store = store
        self.airports: list[str] = cfg.get("sources.wizzair.airports", []) or []
        self.days_ahead = int(cfg.get("sources.wizzair.days_ahead", 45))
        self.routes_per_run = int(cfg.get("sources.wizzair.routes_per_run", 12))
        self.delay_s = float(cfg.get("sources.wizzair.delay_s", 0.6))
        self._verze: str | None = None

    # ---------- pomocné ----------

    def _headers(self) -> dict:
        # Bez Origin a Referer API odpovídá chybou protokolu.
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Origin": "https://wizzair.com", "Referer": WEB}

    def _zahod_cookies(self) -> None:
        """Před každým dotazem na ceny zahodí cookies.

        Server k první odpovědi přiloží `ASP.NET_SessionId` a
        `RequestVerificationToken` a u dalšího dotazu pak ten token vyžaduje
        zpátky v hlavičce — jinak vrátí `InvalidProtocol`. Bez tohohle projde
        z celé dávky jen první trasa a zbytek tiše propadne.

        Sdílená session je jinak správně: drží spojení a retry logiku.
        """
        session = getattr(self.http, "session", None)
        if session is not None:
            session.cookies.clear()

    def verze(self) -> str:
        """Číslo verze z jejich webu. Zvedá se a natvrdo zapsané by jednou přestalo."""
        if self._verze:
            return self._verze
        try:
            nalezene = _VERZE_RE.findall(self.http.get(WEB).text)
            self._verze = max(nalezene) if nalezene else FALLBACK_VERZE
        except Exception as exc:  # noqa: BLE001
            log.warning("Wizz Air: verzi se nepodařilo zjistit (%s), beru %s",
                        exc, FALLBACK_VERZE)
            self._verze = FALLBACK_VERZE
        return self._verze

    def routes(self) -> list[tuple[str, str]]:
        """Trasy z našich letišť podle mapy linek."""
        data = self.http.get_json(f"{BASE}/{self.verze()}/Api/asset/map",
                                  headers=self._headers())
        chtene = set(self.airports)
        out: list[tuple[str, str]] = []
        for city in data.get("cities") or []:
            odkud = city.get("iata")
            if odkud not in chtene:
                continue
            for spoj in city.get("connections") or []:
                kam = spoj.get("iata")
                if kam:
                    out.append((odkud, kam))
        return sorted(set(out))

    def _davka(self, vsechny: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Výsek tras na tenhle běh. Ukazatel se drží v databázi."""
        if not vsechny or self.routes_per_run <= 0:
            return vsechny
        start = int(self.store.get_meta("wizzair:offset") or 0) if self.store else 0
        start %= len(vsechny)
        vyber = [vsechny[(start + i) % len(vsechny)] for i in range(
            min(self.routes_per_run, len(vsechny)))]
        if self.store:
            self.store.set_meta("wizzair:offset",
                                str((start + len(vyber)) % len(vsechny)))
        return vyber

    # ---------- sběr ----------

    def fetch(self) -> list[Offer]:
        try:
            vsechny = self.routes()
        except Exception as exc:  # noqa: BLE001 — výpadek nesmí shodit běh
            log.warning("Wizz Air: mapa linek selhala: %s", exc)
            return []

        datum = (dt.date.today() + dt.timedelta(days=self.days_ahead)).isoformat()
        offers: dict[str, Offer] = {}

        for odkud, kam in self._davka(vsechny):
            try:
                self._zahod_cookies()
                data = self.http.post_json(
                    f"{BASE}/{self.verze()}/Api/asset/farechart",
                    {
                        "flightList": [{"departureStation": odkud,
                                        "arrivalStation": kam, "date": datum}],
                        "priceType": "regular", "adultCount": 1,
                        "childCount": 0, "infantCount": 0,
                        "dayInterval": MIN_DAY_INTERVAL,
                        "wdc": False, "isRescueFare": False, "isFlightChange": False,
                    },
                    self._headers(),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("Wizz Air: %s-%s selhalo: %s", odkud, kam, exc)
                continue

            offer = self._to_offer(odkud, kam, data)
            if offer is not None:
                offers[offer.uid] = offer

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("Wizz Air: %s tras z %s", len(offers), len(vsechny))
        return list(offers.values())

    def _to_offer(self, odkud: str, kam: str, data: dict) -> Offer | None:
        """Z okna dnů bereme tu NEJLEVNĚJŠÍ cenu.

        Historie pak sleduje, jak se hýbe dosažitelné minimum na té trase —
        což je přesně to, co člověk hledá, když se dívá po levné letence.
        """
        ceny = []
        for den in data.get("outboundFlights") or []:
            cena = (den.get("price") or {}).get("amount")
            mena = (den.get("price") or {}).get("currencyCode")
            if cena and mena:
                ceny.append((float(cena), mena, str(den.get("date") or "")[:10]))
        if not ceny:
            return None

        castka, mena, den = min(ceny)

        return Offer(
            source=self.name,
            kind=CATALOG,
            # Uid je trasa, ne termín — jinak by se historie nenasbírala.
            uid=f"{odkud}-{kam}",
            name=f"Letenka z {_MESTA.get(odkud, odkud)} do {kam} (Wizz Air)",
            price_czk=self.fx.to_czk(castka, mena),
            price_orig=castka,
            currency=mena,
            ref_price_czk=None,
            url=f"https://wizzair.com/cs-cz/booking/select-flight/{odkud}/{kam}/{den}",
            category="flight",
            merchant="wizzair",
            # Ceníková cena přímo od dopravce, nikdo si ji nevymýšlí.
            credibility=1.0,
            extra={"airport": odkud, "destination": kam, "outbound": den,
                   "one_way": True},
        )


_MESTA = {"PRG": "Prahy", "BRQ": "Brna", "OSR": "Ostravy",
          "PED": "Pardubic", "VIE": "Vídně", "BTS": "Bratislavy"}
