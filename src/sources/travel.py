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

log = logging.getLogger(__name__)

# Error fare je redakčně ověřená chyba v ceníku, ne běžná akce — proto výš.
ERROR_FARE_CREDIBILITY = 0.95


class TravelSource:
    kind = FEED

    def __init__(self, http, fx, cfg, site: dict) -> None:
        self.http = http
        self.fx = fx
        self.name = site["name"]
        self.feeds: list[dict] = site.get("feeds", []) or []
        self.credibility = float(site.get("credibility", 0.8))
        # České weby píšou ceny v korunách, zahraniční v eurech.
        self.currency = str(site.get("currency", "EUR")).upper()
        self.max_age_days = int(cfg.get("sources.travel.max_age_days", 7))
        self.delay_s = float(cfg.get("sources.travel.delay_s", 0.5))
        self.airports: list[dict] = cfg.get("sources.travel.airports", []) or []

    def fetch(self) -> list[Offer]:
        offers: dict[str, Offer] = {}

        for feed in self.feeds:
            url = feed["url"]
            try:
                root = ET.fromstring(self.http.get(url).content)
            except Exception as exc:  # noqa: BLE001 — jeden feed neshodí zdroj
                log.warning("%s: feed %s selhal: %s", self.name, url, exc)
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

    def _to_offer(self, item: ET.Element, feed: dict) -> Offer | None:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            return None

        if not self._is_fresh(item.findtext("pubDate")):
            return None

        categories = [(c.text or "").lower() for c in item.findall("category")]
        haystack = f"{title.lower()} {' '.join(categories)}"
        is_error_fare = bool(feed.get("error_fare"))
        region = _region(item)

        airport = feed.get("airport") or self._match_airport(haystack)
        if airport is None:
            if not (is_error_fare and "europe" in haystack):
                return None
            airport = "EU"

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
            category=_category(haystack),
            merchant=self.name,
            credibility=ERROR_FARE_CREDIBILITY if is_error_fare else self.credibility,
            extra={
                "airport": airport,
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

    def _match_airport(self, haystack: str) -> str | None:
        for entry in self.airports:
            for term in entry.get("terms", []):
                if term.lower() in haystack:
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


def build_travel_sources(http, fx, cfg) -> list[TravelSource]:
    sites = cfg.get("sources.travel.sites", []) or []
    return [TravelSource(http, fx, cfg, site) for site in sites]
