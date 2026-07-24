"""Sdílená HTTP session s retry a exponenciálním backoffem.

Zdroje jsou neoficiální API a RSS — musí se počítat s 429 a výpadky. Selhání
jednoho požadavku nikdy neshodí celý běh, jen vyhodí výjimku, kterou zdroj
odchytí a přeskočí.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class Http:
    def __init__(self, user_agent: str, timeout_s: int = 25, retries: int = 3) -> None:
        self.timeout_s = timeout_s
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "cs,en;q=0.8,de;q=0.5",
        })

    def get(self, url: str, **kwargs) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, timeout=self.timeout_s, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001 — retry na cokoliv síťového
                last_exc = exc
                if attempt < self.retries - 1:
                    backoff = 2 ** attempt
                    log.debug("%s selhalo (%s), zkouším znovu za %ss", url, exc, backoff)
                    time.sleep(backoff)
        raise RuntimeError(f"Nepodařilo se stáhnout {url}: {last_exc}")

    def get_json(self, url: str, **kwargs) -> dict:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, payload: dict | list, headers: dict | None = None,
                  timeout_s: int | None = None):
        """POST s JSON tělem. Tělo smí být i pole — ITAD tak bere dávky."""
        resp = self.session.post(
            url, json=payload, headers=headers or {},
            timeout=timeout_s or self.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()


def build_http(cfg) -> Http:
    return Http(
        user_agent=cfg.get("http.user_agent", "Mozilla/5.0"),
        timeout_s=int(cfg.get("http.timeout_s", 25)),
        retries=int(cfg.get("http.retries", 3)),
    )
