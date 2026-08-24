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

**Verze API je v cestě** (`be.wizzair.com/29.13.0/…`) a čas od času se zvedne.
Zastaralá verze znamená `404` na všechno a zdroj mlčí úplně — přesně tak Wizz
Air 24. 8. 2026 vypadl: v konstantě bylo 29.8.0 a ta cesta už neexistuje.

Zjišťuje se proto ve třech krocích (`verze`, `_preladit`): zapamatovaná hodnota
z databáze, pak číslo vyčtené z jejich webu a nakonec **oťukání
`be.wizzair.com`**. Ten třetí krok je tam proto, že web se z ostrého serveru
načíst nemusí — `www.wizzair.com` odtamtud vrací `405` — kdežto `be.wizzair.com`
odpovídá normálně. Oťukává se přes `Api/asset/farechart`: GET na živou verzi
vrátí `405` a 82 bajtů, na mrtvou `404`. Mapa by stála 666 kB na pokus.

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
FALLBACK_VERZE = "29.13.0"      # ověřeno živě 24. 8. 2026

# Kolik vyšších verzí se při oťukávání zkusí, než to vzdáme.
PROBE_MINORU = 12
# Oťukávat při každém běhu by z výpadku udělalo palbu na jejich server.
PROBE_INTERVAL_H = 6

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
        """Platná verze v cestě k API.

        Zapamatovaná hodnota má přednost před stahováním webu — ten má dva
        megabajty a při běhu každých deset minut by to bylo čtvrt gigabajtu
        denně jen kvůli jednomu číslu. Že hodnota zestárla, se pozná spolehlivě
        až tím, že API vrátí 404; tehdy zasáhne `_preladit`.
        """
        if self._verze:
            return self._verze
        ulozena = self.store.get_meta("wizzair:verze") if self.store else None
        self._verze = ulozena or self._z_webu() or FALLBACK_VERZE
        self._zapamatuj(self._verze)
        return self._verze

    def _z_webu(self) -> str | None:
        try:
            nalezene = _VERZE_RE.findall(self.http.get(WEB).text)
        except Exception as exc:  # noqa: BLE001 — web může být zablokovaný
            log.warning("Wizz Air: verzi se z webu nepodařilo zjistit (%s)", exc)
            return None
        # Řadit se MUSÍ číselně: textově je "29.9.0" větší než "29.13.0".
        return max(nalezene, key=_cislo_verze) if nalezene else None

    def _zapamatuj(self, verze: str) -> None:
        if self.store is not None:
            self.store.set_meta("wizzair:verze", verze)

    def _smi_ladit(self) -> bool:
        """Oťukávání se pouští nejvýš jednou za `PROBE_INTERVAL_H` hodin."""
        if self.store is None:
            return True
        posledni = self.store.get_meta("wizzair:verze:zkouseno")
        if posledni:
            try:
                kdy = dt.datetime.fromisoformat(posledni)
            except ValueError:
                kdy = None
            if kdy and dt.datetime.now(dt.timezone.utc) - kdy < dt.timedelta(
                    hours=PROBE_INTERVAL_H):
                return False
        self.store.set_meta("wizzair:verze:zkouseno",
                            dt.datetime.now(dt.timezone.utc).isoformat())
        return True

    def _kandidati(self, stara: str) -> list[str]:
        """Co zkusit: nejdřív číslo z webu, pak nejbližší vyšší verze."""
        out: list[str] = []
        z_webu = self._z_webu()
        if z_webu:
            out.append(z_webu)
        major, minor = _cislo_verze(stara)[:2]
        out += [f"{major}.{minor + i}.0" for i in range(1, PROBE_MINORU + 1)]
        out += [f"{major + 1}.{i}.0" for i in range(3)]
        return [v for v in dict.fromkeys(out) if v != stara]

    def _preladit(self) -> bool:
        """Najít platnou verzi, když ta zapamatovaná přestala odpovídat."""
        if not self._smi_ladit():
            return False
        stara = self.verze()
        for kandidat in self._kandidati(stara):
            # 405 = cesta žije, jen GET není správná metoda. 404 = mrtvá verze.
            if self.http.probe(f"{BASE}/{kandidat}/Api/asset/farechart",
                               headers=self._headers()) != 405:
                continue
            log.info("Wizz Air: verze API se posunula z %s na %s", stara, kandidat)
            self._verze = kandidat
            self._zapamatuj(kandidat)
            return True
        log.warning("Wizz Air: platnou verzi API se najít nepodařilo "
                    "(poslední známá %s)", stara)
        return False

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

    def _trasy(self) -> list[tuple[str, str]]:
        """Seznam tras, a když selže, jeden pokus o přeladění verze.

        Bez toho vypadá zastaralá verze v cestě jako prázdný zdroj: v logu
        jeden řádek o mapě linek a nic dalšího. Přesně tak Wizz Air mlčel.
        """
        try:
            return self.routes()
        except Exception as exc:  # noqa: BLE001 — výpadek nesmí shodit běh
            log.warning("Wizz Air: mapa linek selhala: %s", exc)
        if not self._preladit():
            return []
        try:
            return self.routes()
        except Exception as exc:  # noqa: BLE001
            log.warning("Wizz Air: mapa linek selhala i po přeladění: %s", exc)
            return []

    def fetch(self) -> list[Offer]:
        vsechny = self._trasy()
        if not vsechny:
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


def _cislo_verze(verze: str) -> tuple[int, ...]:
    """"29.13.0" -> (29, 13, 0). Nečíselné části padnou na nulu."""
    return tuple(int(c) if c.isdigit() else 0 for c in verze.split("."))


_MESTA = {"PRG": "Prahy", "BRQ": "Brna", "OSR": "Ostravy",
          "PED": "Pardubic", "VIE": "Vídně", "BTS": "Bratislavy"}
