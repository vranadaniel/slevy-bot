"""Ryanair — první katalogový zdroj u cestování.

Všechny ostatní cestovatelské zdroje jsou **feedy**: proud jedinečných
příspěvků, u kterých se dá jen věřit tomu, co redakce napíše. Ryanair je jiný —
vrací ceník, tutéž trasu vidíme při každém běhu, a tím se dá stavět **vlastní
cenová historie**. To je přesně to, co u her rozsoudilo šedý trh: ceník se dá
zpochybnit, vlastní měření ne.

Proč na tom záleží: prahy v `flights.yaml` vznikly ze spodní hrany toho, co
slevové weby publikují, a ty publikují i běžné ceny. Změřeno proti tomuhle API,
že 26 ze 101 úplně obyčejných letenek by spustilo okamžité upozornění. Jakmile
si bot nasbírá vlastní historii, `HistoryOracle` tuhle otázku uzavře — ví, za
kolik ta trasa opravdu chodí, a to i podle sezóny, kterou ceník neumí.

Veřejné API bez klíče a bez registrace:

    https://services-api.ryanair.com/farfnd/v4/roundTripFares
      ?departureAirportIataCode=PRG&currency=CZK
      &outboundDepartureDateFrom=…&inboundDepartureDateTo=…

Vrací už **zpáteční** ceny, tedy tutéž veličinu jako `flights.yaml`.

Dvě věci ověřené na živých datech 27. 7. 2026:

* Parametr `limit` API odmítá s `InvalidLimit` bez ohledu na hodnotu; bez něj
  vrátí kolem 32 tras na letiště.
* Brno a Ostrava vracejí jen tři trasy, Pardubice žádnou. Je to očekávané —
  jsou to malá letiště a Ryanair z nich skoro nelétá. Zdroj je i tak jediný,
  který o nich vůbec něco ví.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from ..sources.base import CATALOG, Offer

log = logging.getLogger(__name__)

API = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"


class RyanairSource:
    name = "ryanair"
    kind = CATALOG

    def __init__(self, http, fx, cfg) -> None:
        self.http = http
        self.fx = fx
        self.airports: list[str] = cfg.get("sources.ryanair.airports", []) or []
        self.days_from = int(cfg.get("sources.ryanair.days_from", 21))
        self.days_to = int(cfg.get("sources.ryanair.days_to", 180))
        self.delay_s = float(cfg.get("sources.ryanair.delay_s", 0.5))

    def fetch(self) -> list[Offer]:
        today = dt.date.today()
        window = {
            "outboundDepartureDateFrom": (today + dt.timedelta(days=self.days_from)).isoformat(),
            "outboundDepartureDateTo": (today + dt.timedelta(days=self.days_to)).isoformat(),
            "inboundDepartureDateFrom": (today + dt.timedelta(days=self.days_from)).isoformat(),
            "inboundDepartureDateTo": (today + dt.timedelta(days=self.days_to)).isoformat(),
            "currency": "CZK",
        }

        offers: dict[str, Offer] = {}
        for code in self.airports:
            try:
                data = self.http.get_json(
                    API, params={"departureAirportIataCode": code, **window}
                )
            except Exception as exc:  # noqa: BLE001 — jedno letiště neshodí zdroj
                log.warning("Ryanair: letiště %s selhalo: %s", code, exc)
                continue

            for fare in data.get("fares") or []:
                offer = self._to_offer(code, fare)
                if offer is not None:
                    offers[offer.uid] = offer

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("Ryanair: %s tras", len(offers))
        return list(offers.values())

    def _to_offer(self, origin: str, fare: dict) -> Offer | None:
        outbound = fare.get("outbound") or {}
        inbound = fare.get("inbound") or {}
        arrival = outbound.get("arrivalAirport") or {}
        price = (fare.get("summary") or {}).get("price") or {}

        dest = arrival.get("iataCode")
        amount = price.get("value")
        if not dest or not amount:
            return None

        city = arrival.get("city", {}).get("name") or arrival.get("name") or dest
        # API vrací "2026-10-13T21:45:00", odkaz chce jen datum.
        tam = str(outbound.get("departureDate") or "")[:10]
        zpet = str(inbound.get("departureDate") or "")[:10]

        return Offer(
            source=self.name,
            kind=CATALOG,
            # Uid je TRASA, ne konkrétní termín. O to celé jde: cena téže trasy
            # se sleduje v čase, takže se pozná, kdy spadla pod vlastní obvyklou
            # hladinu — a to i mimo sezónu, kterou ceník neumí.
            uid=f"{origin}-{dest}",
            name=f"Letenky z {_MESTA.get(origin, origin)} do {city}",
            price_czk=self.fx.to_czk(float(amount), price.get("currencyCode") or "CZK"),
            price_orig=float(amount),
            currency=price.get("currencyCode") or "CZK",
            # Ryanair neuvádí "původní cenu" a je to tak dobře — tuhle roli
            # zastane vlastní historie.
            ref_price_czk=None,
            url=_odkaz(origin, dest, tam, zpet),
            category="flight",
            merchant="ryanair",
            # Ceníková cena přímo od dopravce. Nikdo si ji nevymýšlí, takže
            # nejvyšší důvěra ze všech zdrojů.
            credibility=1.0,
            extra={
                "airport": origin,
                "destination": dest,
                # Termín se ve zprávě vypisuje: nejlevnější kombinace bývá
                # přílet pozdě večer a odlet druhý den dopoledne, což je cena
                # pravdivá, ale nabídka nepoužitelná. Musí to jít poznat.
                "outbound": outbound.get("departureDate"),
                "outbound_arrival": outbound.get("arrivalDate"),
                "inbound": inbound.get("departureDate"),
            },
        )


def _odkaz(origin: str, dest: str, tam: str, zpet: str) -> str:
    """Odkaz rovnou na konkrétní termín.

    Bez `dateOut` a `dateIn` rezervační stránka jen dlouho točí kolečkem
    a skončí hláškou „Nemáte aktivní vyhledávání" — ověřeno v prohlížeči.
    Sada parametrů je celá povinná; `tp*` je duplicitní kopie, kterou si
    jejich aplikace čte při obnovení stránky.
    """
    if not (tam and zpet):
        # Bez termínů je jediný funkční odkaz obyčejné vyhledávání.
        return "https://www.ryanair.com/cz/cs"

    params = {
        "adults": 1, "teens": 0, "children": 0, "infants": 0,
        "dateOut": tam, "dateIn": zpet,
        "isConnectedFlight": "false", "isReturn": "true",
        "discount": 0, "promoCode": "",
        "originIata": origin, "destinationIata": dest,
        "tpAdults": 1, "tpTeens": 0, "tpChildren": 0, "tpInfants": 0,
        "tpStartDate": tam, "tpEndDate": zpet,
        "tpDiscount": 0, "tpPromoCode": "",
        "tpOriginIata": origin, "tpDestinationIata": dest,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://www.ryanair.com/cz/cs/trip/flights/select?{query}"


# Jen pro čitelnost názvu v Telegramu; kód letiště zůstává v `extra`.
_MESTA = {
    "PRG": "Prahy", "BRQ": "Brna", "OSR": "Ostravy",
    "PED": "Pardubic", "VIE": "Vídně", "BTS": "Bratislavy",
}
