"""Kinguin — katalogový zdroj.

Interní JSON API, které web používá pro vlastní výpis produktů. Bez klíče,
bez registrace, bez Cloudflare. Ceny vrací v **eurocentech** a ignoruje
serverové filtry `priceFrom`, `marketingProductType` i `currency` — filtruje
se proto až lokálně.

Katalog se prochází seřazený podle `bestseller.total`. To je zásadní: pole
`price.market` si nastavuje prodejce sám, takže řazení podle slevy vrací brak
(šunta s vymyšlenou cenou 97,50 € a "99 % slevou"). Prodejnost zfalšovat nejde,
takže pozice v žebříčku slouží jako důvěryhodnost položky.
"""

from __future__ import annotations

import logging
import time

from ..sources.base import CATALOG, Offer

log = logging.getLogger(__name__)

API = "https://www.kinguin.net/services/library/api/v1/products/search"


class KinguinSource:
    name = "kinguin"
    kind = CATALOG

    def __init__(self, http, fx, cfg) -> None:
        self.http = http
        self.fx = fx
        self.pages = int(cfg.get("sources.kinguin.pages", 50))
        self.page_size = int(cfg.get("sources.kinguin.page_size", 100))
        self.delay_s = float(cfg.get("sources.kinguin.delay_s", 0.3))
        self.country = cfg.get("sources.kinguin.country", "CZ")

    def _params(self, page: int) -> dict:
        return {
            "store": "kinguin",
            "visible": 1,
            "active": 1,
            "countries": self.country,
            "projection": "productDetails",
            "sort": "bestseller.total,DESC",
            "page": page,
            "size": self.page_size,
        }

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        rank = 0

        for page in range(self.pages):
            try:
                data = self.http.get_json(API, params=self._params(page))
            except Exception as exc:  # noqa: BLE001
                log.warning("Kinguin strana %s selhala: %s", page, exc)
                break

            products = (data.get("_embedded") or {}).get("products") or []
            if not products:
                break

            for product in products:
                offer = self._to_offer(product, rank)
                rank += 1
                if offer is not None:
                    offers.append(offer)

            if self.delay_s:
                time.sleep(self.delay_s)

        log.info("Kinguin: %s nabídek", len(offers))
        return offers

    def _to_offer(self, product: dict, rank: int) -> Offer | None:
        price = product.get("price") or {}
        calculated = price.get("calculated")
        if not calculated:
            return None

        attributes = product.get("attributes") or {}
        url_key = attributes.get("urlKey") or ""
        external_id = product.get("externalId") or ""
        url = (
            f"https://www.kinguin.net/category/{external_id}/{url_key}"
            if external_id and url_key
            else f"https://www.kinguin.net/catalogsearch/result?q={product.get('name', '')}"
        )

        market = price.get("market")
        ingame = product.get("ingameAttributes") or {}

        return Offer(
            source=self.name,
            kind=CATALOG,
            uid=str(product.get("id")),
            name=str(product.get("name") or "").strip(),
            price_czk=self.fx.to_czk(calculated / 100.0, "EUR"),
            price_orig=calculated / 100.0,
            currency="EUR",
            # Deklarovaná cena prodejce. Scoring ji smí použít jen jako slabý signál.
            ref_price_czk=self.fx.to_czk(market / 100.0, "EUR") if market else None,
            url=url,
            category=attributes.get("marketingProductType") or "GAME",
            merchant="kinguin",
            credibility=_credibility(rank),
            extra={
                "rank": rank,
                "stock": ingame.get("stock"),
                "claimed_discount": price.get("discount"),
                "product_type": attributes.get("marketingProductType"),
            },
        )


def _credibility(rank: int) -> float:
    """Pozice v žebříčku prodejnosti -> 0–1.

    Rank 0 dá 1.0, rank 1000 zhruba 0.5, rank 5000 kolem 0.17. Práh pro okamžité
    upozornění je 0.5, takže bez dalších signálů projde zhruba tisícovka
    nejprodávanějších položek.
    """
    return 1.0 / (1.0 + rank / 1000.0)
