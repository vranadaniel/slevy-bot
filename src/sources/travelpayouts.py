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
# Nejlevnější nabídka ke každému dni. Ověřeno 27. 8. 2026: 83 dnů na jeden
# požadavek a stejná pole jako u `API`, včetně `link` na ten levnější termín.
KALENDAR_API = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
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

        Dvě věci, na které se přišlo až během s ostrým tokenem:

        * **`departure_at` se schválně neposílá.** Není to mez okna, ale
          skutečný termín letu — původní rozsah `departure_at` + `return_at`
          půl roku od sebe vracel `400`. A i správně zadaný měsíc odpověď
          **zúží z 100 tras na 31**. Bez něj se ptáme na „nejlevnější, co je
          teď na téhle trase v prodeji", což je pro cenovou historii lepší
          definice: nemá skok na přelomu měsíce.
        * **`limit` je potřeba.** Bez něj vrátí API 30 tras, s ním 100.

        Bez `destination` vrátí nejlevnější cíle, což je celý smysl zdroje.
        """
        return {
            "origin": origin,
            "currency": "czk",
            "sorting": "price",
            "one_way": "true",
            "limit": self.limit,
            "page": 1,
        }

    def _headers(self) -> dict:
        """Token do hlavičky, ne do URL — v query stringu by skončil v logách."""
        return {"X-Access-Token": self.token, "Accept": "application/json"}

    def fetch(self) -> list[Offer]:
        if not self.token:
            log.info("Travelpayouts: chybí TRAVELPAYOUTS_TOKEN, zdroj přeskakuji")
            return []

        headers = self._headers()
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

    # ---------- cenový kalendář trasy ----------

    def cheapest_in_window(self, origin: str, dest: str,
                           one_way: bool = True) -> tuple[float, str, str] | None:
        """Nejlevnější termín na trase v nejbližších měsících.

        Bot jinak vidí jen dnešní cenu a nemá jak poznat, že kouká na drahý
        termín. `grouped_prices` vrátí nejlevnější nabídku ke KAŽDÉMU dni —
        změřeno 27. 8. 2026: 83 dnů na jeden požadavek, a záznamy mají stejná
        pole jako `prices_for_dates`, včetně `link`. Dá se tedy odkázat rovnou
        na ten levnější termín, ne jen na něj ukázat prstem.

        `one_way` se posílá podle nabídky. Jednosměrná stojí zhruba polovinu
        zpáteční, takže porovnat jedno s druhým by vyrobilo falešný propad —
        tentýž důvod, proč mají jednosměrné trasy u Ryanairu vlastní uid.

        Vrací `(cena, den, odkaz)`, nebo `None`, když API nic nenabídne.
        """
        data = self.http.get_json(KALENDAR_API, headers=self._headers(), params={
            "origin": origin,
            "destination": dest,
            "currency": "czk",
            "group_by": "departure_at",
            "market": "cz",
            "one_way": "true" if one_way else "false",
        })

        nejlepsi: tuple[float, str, str] | None = None
        for den, zaznam in ((data or {}).get("data") or {}).items():
            cena = (zaznam or {}).get("price")
            if not cena:
                continue
            try:
                cena = float(cena)
            except (TypeError, ValueError):
                continue
            if nejlepsi is None or cena < nejlepsi[0]:
                nejlepsi = (cena, str(den)[:10], str(zaznam.get("link") or ""))
        return nejlepsi

    def enrich_calendar(self, offers: list[Offer], max_routes: int = 12) -> None:
        """Doplní u letenek nejlevnější termín na téže trase.

        Volá se **až na tom, co se chystá odejít**, ne na celém katalogu —
        stejný důvod jako u `ItadOracle.enrich_popularity`: jeden požadavek
        na trasu. Trasy se navíc deduplikují, protože tutéž vidíme z víc
        zdrojů (Ryanair i Travelpayouts znají PRG-BGY).

        Nikdy z toho nevzniká hodnota. Je to údaj do zprávy: porovnávat cenu
        dopravce s trhem je přesně ten kruh, kvůli kterému bot hlásil Krakov
        za 748 Kč jako trhák.
        """
        if not self.token or max_routes <= 0:
            return

        podle_trasy: dict[tuple[str, str, bool], list[Offer]] = {}
        for offer in offers:
            odkud = offer.extra.get("airport")
            kam = offer.extra.get("destination")
            if offer.category != "flight" or not odkud or not kam:
                continue
            # Cíl musí být kód letiště. U feedů je to název regionu z ceníku,
            # na který se API ptát nedá.
            if len(str(kam)) != 3 or not str(kam).isalpha():
                continue
            klic = (str(odkud), str(kam).upper(), bool(offer.extra.get("one_way")))
            podle_trasy.setdefault(klic, []).append(offer)

        for poradi, (klic, skupina) in enumerate(podle_trasy.items()):
            if poradi >= max_routes:
                log.info("Travelpayouts: strop kalendáře %s tras na běh", max_routes)
                break
            odkud, kam, one_way = klic
            try:
                nejlepsi = self.cheapest_in_window(odkud, kam, one_way)
            except Exception as exc:  # noqa: BLE001 — výpadek nesmí shodit běh
                log.warning("Travelpayouts: kalendář %s-%s selhal: %s", odkud, kam, exc)
                continue

            if nejlepsi is not None:
                cena, den, odkaz = nejlepsi
                for offer in skupina:
                    offer.extra["kalendar_min_czk"] = self.fx.to_czk(cena, "CZK")
                    offer.extra["kalendar_min_date"] = den
                    if odkaz:
                        offer.extra["kalendar_url"] = (
                            f"{AVIASALES}{odkaz}" if odkaz.startswith("/") else odkaz)

            if self.delay_s:
                time.sleep(self.delay_s)


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
