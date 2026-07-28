"""Wizz Air — druhý katalogový zdroj u cestování.

Stejný princip jako u Ryanairu: tutéž trasu vidíme opakovaně, takže se dá stavět
vlastní cenová historie. Ceník se dá zpochybnit, vlastní měření ne.

Ověřeno živě 27. 7. 2026:

* `GET  /Api/asset/map` — mapa linek. Z našich letišť jede **20 tras z Prahy
  a 38 z Bratislavy**. Z Vídně Wizz Air podle mapy nelétá; Ryanair to naopak
  pokrývá, takže se ty dva zdroje doplňují.
* `POST /Api/asset/farechart` — ceny po dnech, **v eurech** (`currencyCode`
  v odpovědi je `EUR`, ne CZK; převod řeší `fx`). `dayInterval` musí být
  v rozmezí **3 až 10** — mimo něj vrátí `DayIntervalMustBeGreaterOrEqualTo3`,
  resp. `DayIntervalMustBeLessOrEqualTo10`. Vrací `2 × dayInterval + 1` dní
  kolem zadaného data, tedy 7 při trojce a **21 při desítce**.

  Bereme maximum. Změřeno 28. 7. 2026 na dvanácti trasách: širší okno našlo
  nižší cenu u **tří z devíti**, jednou o 56 % (Bratislava–Málaga z 90 na 40 €).
  Stojí to tentýž jeden požadavek, jen se přestaneme dívat jen na jeden týden
  ze sto osmdesátidenního horizontu.

  Dny bez spoje vracejí `amount: 0.0` a `priceType: "noData"` — s širším oknem
  jich je víc, takže se filtrují výslovně.

Co Wizz Air **neumí**, ověřeno: `Api/search/CheapFlights` vrací 500,
`Api/search/timetable` odmítá delší rozsahy (`InvalidTimeDateRange`)
a farechart s prázdnou cílovou stanicí vrátí `InvalidArrivalStationCode`.
Obdoba Ryanairova „kamkoliv" tedy neexistuje; seznam tras dává jen mapa linek.

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

# Meze, které API vynucuje validací. Okno je 2 × dayInterval + 1 dní,
# takže desítka znamená 21 dní na jeden požadavek.
MIN_DAY_INTERVAL = 3
MAX_DAY_INTERVAL = 10


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
        # Mimo meze API dotaz odmítne, takže hodnotu z konfigurace ořízneme
        # radši tady, než abychom celý zdroj shodili na validaci.
        self.day_interval = max(MIN_DAY_INTERVAL, min(
            MAX_DAY_INTERVAL, int(cfg.get("sources.wizzair.day_interval", MAX_DAY_INTERVAL))))
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
                        "dayInterval": self.day_interval,
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
            # Dny bez spoje mají `amount: 0` a `priceType: "noData"`. Se širším
            # oknem jich přibylo, takže se zahazují výslovně — nula by jinak
            # vypadala jako letenka zdarma.
            if den.get("priceType") == "noData":
                continue
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
