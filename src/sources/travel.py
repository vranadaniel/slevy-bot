"""Cestovatelské RSS — letenky, hotely a zájezdy.

Jeden parser na všechny weby, stejně jako u Pepperu. Mají totiž stejný tvar:
WordPress RSS, odletové město v titulku nebo v kategoriích, cena v titulku.
Liší se jen doménou a mírou důvěry.

Ověřeno živými požadavky 27. 7. 2026:

* **cestujlevne.com** — 19 nabídek, česky, ceny v korunách a odlety přesně
  z našich letišť včetně Brna, Ostravy a Pardubic, které zahraniční weby
  neznají. Feed navíc sám uvádí světadíl v `<category-web>`. Zdaleka nejbližší
  tomu, co od bota chceme.
* **travelfree.info** — 25 položek, čtyři příspěvky za hodinu a půl. Kategorie
  nesou region i odletové město (`Copenhagen`, `Central Europe`). Nejsilnější
  zdroj pro střední Evropu, jaký se dá bez klíče sehnat.
* **fly4free.com** — hlavní proud plus dvě archivní kategorie `error-fare`
  a `mistake-fare`, z velké části položky z let 2020–2021. Proto `max_age_days`.

Co jsme zkusili a nefunguje: `secretflying.com/feed/` vrací HTML místo RSS,
`fly4free.pl` error-fare feed je prázdný a odlety má stejně z Polska, veřejné
náhledy telegramových kanálů (`t.me/s/…`) u těchhle webů neexistují.

Dvě cesty, jak se položka kvalifikuje:

1. **Feed pro konkrétní letiště** (`airport` v konfiguraci) — třeba
   `travelfree.info/tag/prague/feed/`. Tam je relevantní všechno a město se
   v titulku hledat nemusí; zachytí se tím i nabídky, které už z hlavního
   feedu vypadly.
2. **Obecný feed** — hledá se některé z našich letišť v titulku a kategoriích.
   Výjimka: error fare vyhlášený pro celou Evropu obvykle zahrnuje i naše
   letiště, takže se nezahazuje jen proto, že v titulku není konkrétní město.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from .. import money
from ..sources.base import FEED, Offer
from ..text import fold_term, haystack

log = logging.getLogger(__name__)

# Error fare je redakčně ověřená chyba v ceníku, ne běžná akce — proto výš.
ERROR_FARE_CREDIBILITY = 0.95


class TravelSource:
    kind = FEED

    def __init__(self, http, fx, cfg, site: dict, store=None) -> None:
        self.http = http
        self.fx = fx
        # Bez store se jen vypne podmíněné stahování, jinak nic nemění.
        self.store = store
        self.name = site["name"]
        self.feeds: list[dict] = site.get("feeds", []) or []
        self.credibility = float(site.get("credibility", 0.8))
        # České weby píšou ceny v korunách, zahraniční v eurech.
        self.currency = str(site.get("currency", "EUR")).upper()
        self.max_age_days = int(cfg.get("sources.travel.max_age_days", 7))
        self.delay_s = float(cfg.get("sources.travel.delay_s", 0.5))
        self.airports: list[dict] = cfg.get("sources.travel.airports", []) or []
        # Cizí uzly (Dublin, Frankfurt…). Projdou jen u dálkových cílů,
        # o čemž rozhoduje až `main.drop_pointless_hubs` podle ceníku.
        self.hubs: list[dict] = cfg.get("sources.travel.hub_airports", []) or []

    def fetch(self) -> list[Offer]:
        offers: dict[str, Offer] = {}

        for feed in self.feeds:
            url = feed["url"]
            try:
                root = self._load(url)
            except Exception as exc:  # noqa: BLE001 — jeden feed neshodí zdroj
                log.warning("%s: feed %s selhal: %s", self.name, url, exc)
                continue
            if root is None:      # 304, feed se od minule nezměnil
                continue

            for item in root.findall(".//item"):
                offer = self._to_offer(item, feed)
                if offer is not None:
                    # Tatáž nabídka bývá v hlavním i v letištním feedu.
                    offers[offer.uid] = offer

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("%s: %s nabídek", self.name, len(offers))
        return list(offers.values())

    def _load(self, url: str) -> ET.Element | None:
        """Stáhne feed. Vrátí `None`, když se od minule nezměnil.

        Bez tohohle by rychlý sken tahal zbytečně velká data: hlavní feed
        travelfree.info měří 14 MB a tagový 5 MB, takže při běhu každých deset
        minut jde o zhruba 3,5 GB denně. Server na podmíněný dotaz odpoví
        `304` s prázdným tělem — ověřeno, travelfree.info to umí.

        Nevýhoda: položka, kterou se minule nepodařilo ocenit, se znovu nabídne
        až s další změnou feedu. U zdroje, který přispívá každou půlhodinu, je
        to zdržení v řádu minut.
        """
        # Prohlížeč o RSS říká, co čeká; `requests` posílá holé `*/*`, což je
        # u WAF nad WordPressem jeden ze signálů „tohle je bot".
        headers = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}
        if self.store is not None:
            for header, key in (("If-None-Match", "etag"),
                                ("If-Modified-Since", "modified")):
                cached = self.store.get_meta(f"feed:{key}:{url}")
                if cached:
                    headers[header] = cached

        resp = self.http.get(url, headers=headers)
        if resp.status_code == 304:
            log.debug("%s: %s beze změny", self.name, url)
            return None

        # Parsovat MUSÍME dřív, než si zapamatujeme ETag. Když server vrátí 200
        # s rozbitým tělem a my si značku uložíme, příští běh pošle
        # If-None-Match, dostane 304 a feed nám zmlkne NATRVALO — dokud se
        # obsah náhodou nezmění. Tichá ztráta celého zdroje za jednu vadnou
        # odpověď je horší než stáhnout ho příště znovu celý.
        root = ET.fromstring(resp.content)

        if self.store is not None:
            for header, key in (("ETag", "etag"), ("Last-Modified", "modified")):
                if resp.headers.get(header):
                    self.store.set_meta(f"feed:{key}:{url}", resp.headers[header])

        return root

    def _to_offer(self, item: ET.Element, feed: dict) -> Offer | None:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            return None

        if not self._is_fresh(item.findtext("pubDate")):
            return None

        categories = [(c.text or "").lower() for c in item.findall("category")]
        text = f"{title.lower()} {' '.join(categories)}"
        is_error_fare = bool(feed.get("error_fare"))
        region = _region(item)

        airport = feed.get("airport") or self._match_airport(text)
        hub = None
        if airport is None:
            # Odlet z domácího letiště nenalezen. Zkusíme cizí uzel — ten se
            # ale vyplatí jen u dálkové trasy, takže se položka označí
            # a rozhodne se o ní až podle rozpoznaného regionu.
            hub = self._match_airport(text, self.hubs)
            if hub is not None:
                airport = hub
            elif is_error_fare and "europe" in text:
                airport = "EU"
            else:
                return None

        parsed = money.parse_price(title, self.currency)
        if parsed is None:
            return None
        amount, currency = parsed
        if amount <= 0:
            return None

        return Offer(
            source=self.name,
            kind=FEED,
            uid=(item.findtext("guid") or link).strip(),
            name=title,
            price_czk=self.fx.to_czk(amount, currency),
            price_orig=amount,
            currency=currency,
            ref_price_czk=None,  # feed původní cenu neuvádí, řeší to až oracle
            url=link,
            category=_category(text),
            merchant=self.name,
            credibility=ERROR_FARE_CREDIBILITY if is_error_fare else self.credibility,
            extra={
                "airport": airport,
                # Nese kód uzlu, ne jen True — ať je ve zprávě vidět,
                # odkud se vlastně letí.
                "hub_departure": hub,
                "error_fare": is_error_fare,
                "categories": categories[:8],
                # Světadíl přímo z feedu. Kde ho zdroj uvádí, nemusí se hádat
                # z názvu — a ceník letenek se dá vzít rovnou podle něj.
                "region": region,
            },
        )

    def _is_fresh(self, pub_date: str | None) -> bool:
        if not pub_date:
            return False
        try:
            published = parsedate_to_datetime(pub_date)
        except (TypeError, ValueError):
            return False
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - published
        return age.days <= self.max_age_days

    def _match_airport(self, raw: str, kde: list[dict] | None = None) -> str | None:
        """Odletové letiště z názvu a kategorií.

        Bez diakritiky — české zdroje píšou „z Vídně", konfigurace „vídeň",
        a někdy se totéž město objeví i bez háčků. Viz `text.fold`.
        """
        text = haystack(raw)
        for entry in (self.airports if kde is None else kde):
            for term in entry.get("terms", []):
                if fold_term(term) in text:
                    return entry["code"]
        return None


def _category(haystack: str) -> str:
    """Letenka, nebo pobyt? Prahy se u obojího nastavují zvlášť."""
    if "zájezd" in haystack or "hotel" in haystack or "pobyt" in haystack:
        return "hotel"
    return "flight"


def _region(item: ET.Element) -> str | None:
    """Světadíl z feedu, když ho zdroj uvádí.

    cestujlevne.com posílá `<category-web>` se slugem (`evropa`, `asie`,
    `afrika`, `stredni-amerika`). Hledá se podle lokálního jména značky,
    ať to funguje i kdyby ji zdroj někdy dal do jmenného prostoru.
    """
    for child in item:
        if child.tag.rsplit("}", 1)[-1] == "category-web" and child.text:
            return child.text.strip().lower()
    return None


def build_travel_sources(http, fx, cfg, store=None) -> list[TravelSource]:
    sites = cfg.get("sources.travel.sites", []) or []
    return [TravelSource(http, fx, cfg, site, store) for site in sites]
