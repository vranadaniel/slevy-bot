"""Pepper network — feedové zdroje (mydealz.de, hotukdeals.com, dealabs.com, pepper.pl).

Jeden parser na všechny čtyři weby; liší se jen doménou a měnou. Struktura položky:

    <title>116° - [CB/Uni] HP OMEN Transcend ... effektiv 1.183,81€</title>
    <category>Elektronik</category>
    <pepper:merchant name="HP" price="1.259,29€"/>

Číslo v titulku je **komunitní teplota dealu** — davem ověřené hodnocení kvality,
napříč všemi kategoriemi. To je u feedů jediná obrana proti braku, protože tady
žádnou vlastní cenovou historii nemáme.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from .. import money
from ..sources.base import FEED, Offer

log = logging.getLogger(__name__)

PEPPER_NS = "{http://www.pepper.com/rss}"
_TEMP_RE = re.compile(r"^\s*(-?\d+)\s*°\s*-?\s*")
_TAG_RE = re.compile(r"<[^>]+>")


class PepperSource:
    kind = FEED

    def __init__(self, http, fx, site: dict, min_temperature: int, delay_s: float) -> None:
        self.http = http
        self.fx = fx
        self.name = site["name"]
        self.domain = site["domain"]
        self.currency = site.get("currency", "EUR")
        self.feeds = site.get("feeds", ["hot"])
        self.min_temperature = min_temperature
        self.delay_s = delay_s

    def fetch(self) -> list[Offer]:
        offers: dict[str, Offer] = {}

        for feed in self.feeds:
            url = f"https://{self.domain}/rss/{feed}"
            try:
                raw = self.http.get(url).content
                root = ET.fromstring(raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s feed %s selhal: %s", self.name, feed, exc)
                continue

            for item in root.findall(".//item"):
                offer = self._to_offer(item)
                if offer is not None:
                    offers[offer.uid] = offer  # /hot a /new se překrývají

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("%s: %s nabídek", self.name, len(offers))
        return list(offers.values())

    def _to_offer(self, item: ET.Element) -> Offer | None:
        title_raw = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not title_raw or not link:
            return None

        temperature, title = _split_temperature(title_raw)
        if temperature is None or temperature < self.min_temperature:
            return None

        merchant_el = item.find(f"{PEPPER_NS}merchant")
        merchant = None
        price_text = None
        if merchant_el is not None:
            merchant = (merchant_el.get("name") or "").strip() or None
            price_text = merchant_el.get("price")

        description = _strip_html(item.findtext("description") or "")

        # Cena: nejdřív strukturovaný atribut, teprve pak text příspěvku.
        parsed = money.parse_price(price_text or "", self.currency)
        if parsed is None:
            parsed = money.parse_price(description, self.currency)
        if parsed is None:
            parsed = money.parse_price(title, self.currency)
        if parsed is None:
            return None  # bez ceny nejde spočítat procento slevy

        amount, currency = parsed
        if amount <= 0:
            return None

        haystack = f"{title} {description}"
        ref_czk = None
        original = money.find_original_price(haystack, self.currency)
        if original and original[0] > amount:
            ref_czk = self.fx.to_czk(original[0], original[1])
        else:
            # Když je uvedené procento slevy, dopočítáme z něj původní cenu.
            pct = money.find_discount_percent(haystack)
            if pct:
                ref_czk = self.fx.to_czk(amount / (1 - pct / 100.0), currency)

        return Offer(
            source=self.name,
            kind=FEED,
            uid=guid,
            name=title,
            price_czk=self.fx.to_czk(amount, currency),
            price_orig=amount,
            currency=currency,
            ref_price_czk=ref_czk,
            url=link,
            category=item.findtext("category"),
            merchant=merchant,
            credibility=_credibility(temperature),
            extra={
                "temperature": temperature,
                "description": description[:600],
                "site": self.domain,
            },
        )


def _split_temperature(title: str) -> tuple[int | None, str]:
    m = _TEMP_RE.match(title)
    if not m:
        return None, title
    return int(m.group(1)), title[m.end():].strip()


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text).replace("&nbsp;", " ").strip()


def _credibility(temperature: int) -> float:
    """Teplota dealu -> 0–1. 500° a výš je maximum důvěry."""
    return max(0.0, min(1.0, temperature / 500.0))


def build_pepper_sources(http, fx, cfg) -> list[PepperSource]:
    sites = cfg.get("sources.pepper.sites", []) or []
    min_temp = int(cfg.get("sources.pepper.min_temperature", 150))
    delay_s = float(cfg.get("sources.pepper.delay_s", 0.5))
    return [PepperSource(http, fx, site, min_temp, delay_s) for site in sites]
