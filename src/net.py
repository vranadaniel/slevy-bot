"""Sdílená HTTP session s retry a exponenciálním backoffem.

Zdroje jsou neoficiální API a RSS — musí se počítat s 429 a výpadky. Selhání
jednoho požadavku nikdy neshodí celý běh, jen vyhodí výjimku, kterou zdroj
odchytí a přeskočí.
"""

from __future__ import annotations

import logging
import re
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
        # Hlášku z requests je nutné ořezat: nese v sobě celou URL i s query
        # stringem, takže klíč předaný parametrem by skončil v logu. Stejný
        # důvod jako v `_describe_error`.
        raise RuntimeError(f"Nepodařilo se stáhnout {url}: {_bez_query(last_exc)}")

    def get_json(self, url: str, **kwargs) -> dict:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, payload: dict | list, headers: dict | None = None,
                  timeout_s: int | None = None):
        """POST s JSON tělem. Tělo smí být i pole — ITAD tak bere dávky."""
        resp = self.session.post(
            url, json=payload, headers=headers or {},
            timeout=timeout_s or self.timeout_s,
        )
        if not resp.ok:
            raise RuntimeError(_describe_error(resp))
        return resp.json()


_QUERY_RE = re.compile(r"\?\S*")


def _bez_query(exc) -> str:
    """Text výjimky bez query stringů.

    `requests` píše do hlášky celou URL. Kdyby zdroj předával klíč parametrem,
    ocitl by se rovnou v logu i v tracebacku.
    """
    return _QUERY_RE.sub("?…", str(exc))


def _describe_error(resp) -> str:
    """Chybová hláška včetně těla odpovědi.

    Samotné "403 Client Error" neřekne nic o příčině; API obvykle důvod píše
    v těle. URL se ořezává o query string, aby se do logu nedostal klíč
    předaný parametrem.
    """
    url = resp.url.split("?")[0]
    body = (resp.text or "").strip().replace("\n", " ")[:300]
    return f"HTTP {resp.status_code} z {url}" + (f" — {body}" if body else "")


def build_http(cfg) -> Http:
    return Http(
        user_agent=cfg.get("http.user_agent", "Mozilla/5.0"),
        timeout_s=int(cfg.get("http.timeout_s", 25)),
        retries=int(cfg.get("http.retries", 3)),
    )
