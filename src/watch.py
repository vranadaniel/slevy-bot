"""Hlídání konkrétního záměru, ne čekání, co propadne prahem.

Zbytek bota funguje obráceně: sbírá, co zdroje nabídnou, a hlásí to, co je
podezřele levné. Tohle je opačný směr — člověk řekne „chci do Barcelony na
devět nocí někdy mezi polovinou srpna a polovinou října, odlet v pátek večer
a návrat v neděli odpoledne" a bot na to hlídá nejlepší možnost.

Proč to jde postavit na Ryanairu (změřeno živě 27. 8. 2026):

* `farfnd/v4/roundTripFares` na KONKRÉTNÍ trase bere `durationFrom`
  a `durationTo`, tedy počet nocí. Dotaz na 9–9 vrátil přesně devět nocí
  (7. 10. → 16. 10.). Na hledání tras se tenhle parametr použít nedá, protože
  odpověď zúží na zlomek sítě — u jedné trasy ale funguje, jak má.
* Odpověď nese **přesné časy odletu i příletu** obou letů, takže „pátek večer“
  se dá vyhodnotit, ne odhadnout.
* Ceny jsou rovnou v korunách a endpoint je veřejný, bez klíče.
* `timtbl/3/schedules/…` vrátí letový řád na celý měsíc jedním požadavkem.
  Na trase PRG–BCN je 25 z 27 dnů jen **jeden let denně** a jeho čas se den
  ode dne mění (21:30, 10:05, 13:35, 15:10). To je klíčové zjištění:
  „nejlevnější let toho dne“ a „jediný let toho dne“ je skoro vždycky totéž,
  takže se čas nedá vybrat — dá se vybrat DEN, na který ten čas padne. Proto
  se řád stahuje napřed a na ceny se ptáme jen na dny, které do zadání sedí.

Omezení, které z toho plyne a nemá smysl ho zakrývat: hlídání umí jen síť
Ryanairu. Wizz Air obdobu `duration` nemá a agregátory neumí říct „devět
nocí". Na evropský prodloužený víkend to stačí, na dálkové lety ne.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

TIMETABLE = ("https://services-api.ryanair.com/timtbl/3/schedules"
             "/{origin}/{dest}/years/{year}/months/{month}")
FARES = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"

DNY = ("po", "út", "st", "čt", "pá", "so", "ne")


@dataclass
class Watch:
    """Jeden hlídaný záměr."""

    id: int
    origin: str
    destination: str
    od: dt.date
    do: dt.date
    nights_min: int
    nights_max: int
    # Den v týdnu 0–6 (pondělí = 0), None = na dni nezáleží.
    out_day: int | None = None
    back_day: int | None = None
    # Časové okno odletu a návratu, hodina 0–23, None = nezáleží. Obě meze
    # jsou potřeba: „neděle do 15:00“ splní i let v 5:45, jenže ten tě
    # o ten víkend připraví. Smysl zadání je opačný.
    out_after_h: int | None = None
    out_before_h: int | None = None
    back_after_h: int | None = None
    back_before_h: int | None = None
    best_czk: float | None = None
    best_key: str | None = None

    def label(self) -> str:
        noci = (f"{self.nights_min} nocí" if self.nights_min == self.nights_max
                else f"{self.nights_min}–{self.nights_max} nocí")
        casti = [f"{self.origin}→{self.destination}", noci,
                 f"{_den_mesic(self.od)}–{_den_mesic(self.do)}"]
        tam = _preference(self.out_day, self.out_after_h, self.out_before_h)
        if tam:
            casti.append("tam " + tam)
        zpet = _preference(self.back_day, self.back_after_h, self.back_before_h)
        if zpet:
            casti.append("zpět " + zpet)
        return " · ".join(casti)


@dataclass
class Vysledek:
    """Co hlídání našlo.

    Dvě položky schválně: `vyhovujici` je to, co splňuje úplně všechno, a
    `nahradni` je nejlevnější kombinace v okně bez ohledu na časy. Bez té
    druhé by přeostřené zadání znamenalo ticho — a ticho, ze kterého se nedá
    poznat, jestli se nic nenašlo, nebo jestli je něco rozbité, je v tomhle
    projektu ta nejhorší odpověď. Stejný důvod jako u sekce TĚSNĚ POD PRAHEM.
    """

    vyhovujici: "Trip | None" = None
    nahradni: "Trip | None" = None


@dataclass
class Trip:
    """Nalezená kombinace."""

    price_czk: float
    out_dep: dt.datetime
    out_arr: dt.datetime
    back_dep: dt.datetime
    back_arr: dt.datetime
    url: str

    @property
    def nights(self) -> int:
        return (self.back_dep.date() - self.out_dep.date()).days

    def key(self) -> str:
        """Identita kombinace — ať se táž nabídka nehlásí dvakrát."""
        return f"{self.out_dep:%Y-%m-%dT%H:%M}|{self.back_dep:%Y-%m-%dT%H:%M}"

    def label(self) -> str:
        return (f"{_den_cas(self.out_dep)} → {_den_cas(self.back_dep)}"
                f" · {self.nights} nocí")


class WatchEngine:
    """Hledá nejlepší kombinaci pro jeden záměr."""

    def __init__(self, http, delay_s: float = 0.4, max_queries: int = 12) -> None:
        self.http = http
        self.delay_s = delay_s
        # Strop dotazů na jedno hlídání a běh. Bez něj by široké okno bez
        # omezení na den v týdnu znamenalo dotaz na každý den v okně.
        self.max_queries = max_queries
        self._radky: dict[tuple, dict] = {}

    # ---------- letový řád ----------

    def timetable(self, origin: str, dest: str, rok: int, mesic: int) -> dict:
        """Které dny se letí a v kolik. Jeden požadavek na měsíc."""
        klic = (origin, dest, rok, mesic)
        if klic in self._radky:
            return self._radky[klic]

        try:
            data = self.http.get_json(TIMETABLE.format(
                origin=origin, dest=dest, year=rok, month=mesic))
        except Exception as exc:  # noqa: BLE001 — prázdný měsíc není porucha
            log.debug("Řád %s-%s %s/%s selhal: %s", origin, dest, mesic, rok, exc)
            data = {}

        out: dict[dt.date, list[dt.time]] = {}
        for den in (data or {}).get("days") or []:
            casy = [_hodina(f.get("departureTime"))
                    for f in (den.get("flights") or []) if f.get("departureTime")]
            casy = [c for c in casy if c is not None]
            if not casy:
                continue
            try:
                out[dt.date(rok, mesic, int(den["day"]))] = sorted(casy)
            except (KeyError, ValueError, TypeError):
                continue

        self._radky[klic] = out
        return out

    def _letove_dny(self, origin: str, dest: str, od: dt.date, do: dt.date) -> dict:
        """Řád přes celé okno, po měsících."""
        out: dict[dt.date, list[dt.time]] = {}
        kurzor = dt.date(od.year, od.month, 1)
        while kurzor <= do:
            out.update(self.timetable(origin, dest, kurzor.year, kurzor.month))
            kurzor = (kurzor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return {den: casy for den, casy in out.items() if od <= den <= do}

    # ---------- výběr dnů ----------

    def candidate_days(self, watch: Watch) -> list:
        """Dny odletu, které dávají smysl: letí se a sedí den i čas.

        Filtrovat podle řádu je celý trik. Na trase bývá jeden let denně, takže
        „pátek večer“ nejde vybrat mezi lety — jde vybrat pátek, na který ten
        večerní let padne. Ušetří to i dotazy: ptáme se jen na dny, které
        můžou projít.
        """
        dny = self._letove_dny(watch.origin, watch.destination, watch.od, watch.do)
        dnes = dt.date.today()

        vybrane = []
        for den, casy in sorted(dny.items()):
            if den < dnes:
                continue
            if watch.out_day is not None and den.weekday() != watch.out_day:
                continue
            if not any(_v_okne(c.hour, watch.out_after_h, watch.out_before_h)
                       for c in casy):
                continue
            vybrane.append(den)
        return vybrane

    def _navratove_dny(self, watch: Watch, odlet: dt.date) -> list:
        """Přípustné dny návratu pro daný odlet."""
        prvni = odlet + dt.timedelta(days=watch.nights_min)
        posledni = odlet + dt.timedelta(days=watch.nights_max)
        dny = self._letove_dny(watch.destination, watch.origin, prvni, posledni)

        vybrane = []
        for den, casy in sorted(dny.items()):
            if watch.back_day is not None and den.weekday() != watch.back_day:
                continue
            if not any(_v_okne(c.hour, watch.back_after_h, watch.back_before_h)
                       for c in casy):
                continue
            vybrane.append(den)
        return vybrane

    # ---------- ceny ----------

    def _fare(self, watch: Watch, odlet: dt.date, navrat_od: dt.date,
              navrat_do: dt.date, odlet_do: dt.date | None = None):
        params = {
            "departureAirportIataCode": watch.origin,
            "arrivalAirportIataCode": watch.destination,
            "outboundDepartureDateFrom": odlet.isoformat(),
            "outboundDepartureDateTo": (odlet_do or odlet).isoformat(),
            "inboundDepartureDateFrom": navrat_od.isoformat(),
            "inboundDepartureDateTo": navrat_do.isoformat(),
            "durationFrom": watch.nights_min,
            "durationTo": watch.nights_max,
            "currency": "CZK",
            "adultPaxCount": 1,
        }
        try:
            data = self.http.get_json(FARES, params=params)
        except Exception as exc:  # noqa: BLE001 — jeden den neshodí hlídání
            log.debug("Cena %s %s selhala: %s", watch.destination, odlet, exc)
            return None

        nejlepsi = None
        for fare in (data or {}).get("fares") or []:
            trip = _to_trip(watch, fare)
            if trip is None:
                continue
            if nejlepsi is None or trip.price_czk < nejlepsi.price_czk:
                nejlepsi = trip
        return nejlepsi

    def best_trip(self, watch: Watch) -> "Vysledek":
        """Nejlepší kombinace, která splňuje všechno zadané.

        Když nic nesedí, doptá se JEDNÍM dotazem na celé okno bez omezení na
        časy, ať je co ukázat. Ten dotaz stojí za to jen v případě neúspěchu —
        proto je až tady, a ne rovnou.
        """
        nejlepsi = None
        dotazu = 0

        for odlet in self.candidate_days(watch):
            navraty = self._navratove_dny(watch, odlet)
            if not navraty:
                continue
            if dotazu >= self.max_queries:
                log.info("Hlídání %s: strop %s dotazů na běh",
                         watch.destination, self.max_queries)
                break

            trip = self._fare(watch, odlet, navraty[0], navraty[-1])
            dotazu += 1
            if self.delay_s:
                time.sleep(self.delay_s)

            if trip is None:
                continue
            # Řád říká, že ten den let existuje; jestli se konkrétní kombinace
            # trefila do časů, se musí ověřit na tom, co vrátilo API.
            if not _vyhovuje(watch, trip):
                continue
            if nejlepsi is None or trip.price_czk < nejlepsi.price_czk:
                nejlepsi = trip

        if nejlepsi is not None:
            return Vysledek(vyhovujici=nejlepsi)
        return Vysledek(nahradni=self._nejlevnejsi_v_okne(watch))

    def _nejlevnejsi_v_okne(self, watch: Watch):
        """Nejlevnější kombinace v okně, časy stranou. Jeden dotaz."""
        posledni_navrat = watch.do + dt.timedelta(days=watch.nights_max)
        return self._fare(watch, watch.od, watch.od + dt.timedelta(days=watch.nights_min),
                          posledni_navrat, odlet_do=watch.do)


# ---------- zadání z Telegramu ----------

NAPOVEDA = (
    "<b>Hlídání letenek</b>\n\n"
    "<code>/hlidat BCN 15.8. 15.10. 9</code>\n"
    "Do Barcelony na 9 nocí, odlet kdykoliv mezi 15. 8. a 15. 10.\n\n"
    "<b>Volitelně:</b>\n"
    "• <code>7-10</code> místo <code>9</code> — rozsah nocí\n"
    "• <code>odkud=VIE</code> — jiné výchozí letiště\n"
    "• <code>tam=pá@18-23</code> — odlet v pátek mezi 18. a 23. hodinou\n"
    "• <code>zpet=ne@11-18</code> — návrat v neděli mezi 11. a 18.\n"
    "Den i čas jsou samostatné: <code>tam=pá</code> i <code>tam=@18-23</code>.\n\n"
    "<b>Příklad na maximum víkendu:</b>\n"
    "<code>/hlidat BCN 15.8. 15.10. 9 tam=pá@17-23 zpet=ne@11-18</code>\n\n"
    "<b>Další příkazy:</b>\n"
    "<code>/hlidani</code> — co se hlídá\n"
    "<code>/zrusit 3</code> — zrušit hlídání číslo 3"
)


class ChybaZadani(ValueError):
    """Zadání se nepodařilo přečíst. Text jde rovnou uživateli."""


def parse_watch(text: str, default_origin: str = "PRG") -> dict:
    """Rozebere `/hlidat …` na zadání.

    Syntaxe je schválně polohovaná a krátká: do Telegramu se píše z mobilu,
    takže `cil=BCN od=… do=…` by nikdo psát nechtěl. Nejednoznačnost hlídá
    pořadí — kód letiště, dvě data, počet nocí — a zbytek jsou pojmenované
    přívlastky, u kterých na pořadí nezáleží.
    """
    kusy = (text or "").split()
    if kusy and kusy[0].startswith("/"):
        kusy = kusy[1:]

    polohove, pojmenovane = [], {}
    for kus in kusy:
        if "=" in kus:
            klic, _, hodnota = kus.partition("=")
            pojmenovane[_bez_diakritiky(klic.lower())] = hodnota
        else:
            polohove.append(kus)

    if len(polohove) < 4:
        raise ChybaZadani(
            "Chybí něco ze zadání. Čekám cíl, dvě data a počet nocí:\n"
            "<code>/hlidat BCN 15.8. 15.10. 9</code>")

    cil = polohove[0].upper()
    if len(cil) != 3 or not cil.isalpha():
        raise ChybaZadani(f"„{polohove[0]}“ nevypadá jako kód letiště. "
                          "Čekám tři písmena, třeba <code>BCN</code>.")

    od = _datum(polohove[1])
    # Druhé datum se dopočítává OD prvního, ne ode dneška. Jinak by „15.8.
    # 15.10." zadané koncem srpna znamenalo srpen příštího roku a říjen
    # letošního, tedy konec dřív než začátek.
    do = _datum(polohove[2], po=od)
    if do < od:
        raise ChybaZadani("Druhé datum je dřív než první.")

    noci_min, noci_max = _noci(polohove[3])
    tam_den, tam_od, tam_do = _cast_dne(pojmenovane.get("tam"))
    zpet_den, zpet_od, zpet_do = _cast_dne(pojmenovane.get("zpet"))

    odkud = (pojmenovane.get("odkud") or default_origin).upper()
    if len(odkud) != 3 or not odkud.isalpha():
        raise ChybaZadani(f"„{odkud}“ nevypadá jako kód letiště.")

    return {
        "origin": odkud, "destination": cil,
        "od": od.isoformat(), "do": do.isoformat(),
        "nights_min": noci_min, "nights_max": noci_max,
        "out_day": tam_den, "out_after_h": tam_od, "out_before_h": tam_do,
        "back_day": zpet_den, "back_after_h": zpet_od, "back_before_h": zpet_do,
    }


def _datum(text: str, po: "dt.date | None" = None) -> dt.date:
    """Bere `2026-08-15`, `15.8.2026` i `15.8.` (dopočte nejbližší rok).

    `po` posouvá dopočet roku: bez něj se bere nejbližší budoucí výskyt ode
    dneška, s ním nejbližší od zadaného data.
    """
    text = text.strip()
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        pass

    casti = [c for c in text.replace(" ", "").split(".") if c]
    try:
        den, mesic = int(casti[0]), int(casti[1])
        if len(casti) >= 3:
            rok = int(casti[2])
            rok += 2000 if rok < 100 else 0
            return dt.date(rok, mesic, den)
        # Bez roku: nejbližší výskyt od hranice. „15.8.“ v prosinci znamená
        # příští srpen, ne ten, co byl.
        hranice = po or dt.date.today()
        try:
            kandidat = dt.date(hranice.year, mesic, den)
        except ValueError:      # 29. 2. v nepřestupném roce
            return dt.date(hranice.year + 1, mesic, den)
        return kandidat if kandidat >= hranice else dt.date(hranice.year + 1, mesic, den)
    except (IndexError, ValueError):
        raise ChybaZadani(
            f"Datu „{text}“ nerozumím. Zkus <code>15.8.</code> "
            "nebo <code>2026-08-15</code>.") from None


def _noci(text: str) -> tuple:
    try:
        if "-" in text:
            a, _, b = text.partition("-")
            noci_min, noci_max = int(a), int(b)
        else:
            noci_min = noci_max = int(text)
    except ValueError:
        raise ChybaZadani(
            f"Počtu nocí „{text}“ nerozumím. Čekám <code>9</code> "
            "nebo rozsah <code>7-10</code>.") from None

    if not 0 <= noci_min <= noci_max <= 60:
        raise ChybaZadani("Počet nocí musí být rozumný rozsah od nuly do 60.")
    return noci_min, noci_max


def _cast_dne(hodnota) -> tuple:
    """`pá@18-23`, `pá`, `@18-23`, `18-23` -> (den, od_hodiny, do_hodiny)."""
    if not hodnota:
        return None, None, None

    den_text, _, cas_text = hodnota.partition("@")
    if not cas_text and _je_cas(den_text):
        den_text, cas_text = "", den_text

    den = None
    if den_text:
        zkratka = _bez_diakritiky(den_text.lower())[:2]
        zkratky = [_bez_diakritiky(d) for d in DNY]
        if zkratka not in zkratky:
            raise ChybaZadani(
                f"Dni „{den_text}“ nerozumím. Čekám {', '.join(DNY)}.")
        den = zkratky.index(zkratka)

    od_h = do_h = None
    if cas_text:
        try:
            if "-" in cas_text:
                a, _, b = cas_text.partition("-")
                od_h, do_h = int(a), int(b)
            else:
                od_h = int(cas_text)
        except ValueError:
            raise ChybaZadani(
                f"Času „{cas_text}“ nerozumím. Čekám hodinu <code>18</code> "
                "nebo rozsah <code>18-23</code>.") from None
        for h in (od_h, do_h):
            if h is not None and not 0 <= h <= 24:
                raise ChybaZadani("Hodina musí být mezi 0 a 24.")

    return den, od_h, do_h


def _je_cas(text: str) -> bool:
    return bool(text) and all(c.isdigit() or c == "-" for c in text)


def _bez_diakritiky(text: str) -> str:
    """Vlastní, protože `text.fold` navíc mění na malá a ořezává mezery —
    tady jde jen o diakritiku, ať `/hlídat` funguje stejně jako `/hlidat`."""
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


# ---------- pomocné ----------


def _v_okne(hodina: int, od, do) -> bool:
    """Spadá hodina do zadaného okna? Chybějící mez znamená „neomezeno“."""
    if od is not None and hodina < od:
        return False
    if do is not None and hodina >= do:
        return False
    return True


def _vyhovuje(watch: Watch, trip: Trip) -> bool:
    if watch.out_day is not None and trip.out_dep.weekday() != watch.out_day:
        return False
    if not _v_okne(trip.out_dep.hour, watch.out_after_h, watch.out_before_h):
        return False
    if watch.back_day is not None and trip.back_dep.weekday() != watch.back_day:
        return False
    if not _v_okne(trip.back_dep.hour, watch.back_after_h, watch.back_before_h):
        return False
    return watch.nights_min <= trip.nights <= watch.nights_max


def _to_trip(watch: Watch, fare: dict):
    from .sources.ryanair import _odkaz

    ven, zpet = fare.get("outbound") or {}, fare.get("inbound") or {}
    cena = ((fare.get("summary") or {}).get("price") or {}).get("value")
    casy = [_datum_cas(ven.get("departureDate")), _datum_cas(ven.get("arrivalDate")),
            _datum_cas(zpet.get("departureDate")), _datum_cas(zpet.get("arrivalDate"))]
    if cena is None or any(c is None for c in casy):
        return None

    return Trip(price_czk=float(cena), out_dep=casy[0], out_arr=casy[1],
                back_dep=casy[2], back_arr=casy[3],
                url=_odkaz(watch.origin, watch.destination,
                           ven.get("departureDate"), zpet.get("departureDate")))


def _datum_cas(hodnota):
    try:
        return dt.datetime.fromisoformat(hodnota) if hodnota else None
    except (ValueError, TypeError):
        return None


def _hodina(hodnota):
    try:
        h, m = str(hodnota)[:5].split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def _den_mesic(den: dt.date) -> str:
    return f"{den.day}. {den.month}."


def _den_cas(kdy: dt.datetime) -> str:
    return f"{DNY[kdy.weekday()]} {kdy.day}. {kdy.month}. {kdy:%H:%M}"


def _preference(den, od, do) -> str:
    casti = []
    if den is not None:
        casti.append(DNY[den])
    if od is not None and do is not None:
        casti.append(f"{od}–{do} h")
    elif od is not None:
        casti.append(f"od {od}:00")
    elif do is not None:
        casti.append(f"do {do}:00")
    return " ".join(casti)
