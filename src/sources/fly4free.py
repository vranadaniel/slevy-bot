"""fly4free.com — feedový zdroj pro letenky a hotely.

Tři feedy: hlavní proud plus dvě archivní kategorie `error-fare` a `mistake-fare`.
Kategorie nesou odletové město (`cheap flights from Vienna`), takže jde filtrovat
na konkrétní letiště.

Dvě věci ověřené na živých datech:

* Error-fare feedy jsou z velké části **archiv** — nejnovější položka je ze srpna
  2025, zbytek z let 2020–2021. Proto filtr `max_age_days`; bez něj by první běh
  poslal šest let staré nabídky.
* Vídeň má v hlavním feedu silné zastoupení (7 položek z 50), Praha se objeví
  zřídka a Brno, Ostrava ani Pardubice vůbec. Reálné pokrytí je tedy hlavně
  VIE a BTS.
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

_TAG_RE_CHARS = ("<", ">")


class Fly4FreeSource:
    name = "fly4free"
    kind = FEED

    def __init__(self, http, fx, cfg) -> None:
        self.http = http
        self.fx = fx
        self.feeds: list[dict] = cfg.get("sources.fly4free.feeds", []) or []
        self.max_age_days = int(cfg.get("sources.fly4free.max_age_days", 7))
        self.delay_s = float(cfg.get("sources.fly4free.delay_s", 0.5))
        self.airports: list[dict] = cfg.get("sources.fly4free.airports", []) or []

    def fetch(self) -> list[Offer]:
        offers: dict[str, Offer] = {}

        for feed in self.feeds:
            url = feed["url"]
            is_error_fare = bool(feed.get("error_fare"))
            try:
                root = ET.fromstring(self.http.get(url).content)
            except Exception as exc:  # noqa: BLE001
                log.warning("fly4free feed %s selhal: %s", url, exc)
                continue

            for item in root.findall(".//item"):
                offer = self._to_offer(item, is_error_fare)
                if offer is not None:
                    offers[offer.uid] = offer

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("fly4free: %s nabídek", len(offers))
        return list(offers.values())

    def _to_offer(self, item: ET.Element, is_error_fare: bool) -> Offer | None:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            return None

        if not self._is_fresh(item.findtext("pubDate")):
            return None

        categories = [(c.text or "").lower() for c in item.findall("category")]
        haystack = f"{title.lower()} {' '.join(categories)}"

        airport = self._match_airport(haystack)
        if airport is None:
            # Error fare vyhlášený pro celou Evropu obvykle zahrnuje i naše letiště,
            # takže ho nezahazujeme jen proto, že v titulku není konkrétní město.
            if not (is_error_fare and "europe" in haystack):
                return None
            airport = "EU"

        parsed = money.parse_price(title, "EUR")
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
            category="flight" if "hotel" not in haystack else "hotel",
            merchant="fly4free",
            # Redakčně vybírané, tedy konstantně vysoká důvěra; error fare ještě výš.
            credibility=0.95 if is_error_fare else 0.8,
            extra={
                "airport": airport,
                "error_fare": is_error_fare,
                "categories": categories[:8],
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
