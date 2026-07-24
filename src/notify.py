"""Odesílání na Telegram.

Dvě úrovně: extrém pingne hned zvlášť, zbytek přijde jednou denně jako souhrn.
"""

from __future__ import annotations

import html
import logging

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

_SOURCE_LABELS = {
    "kinguin": "Kinguin",
    "mydealz": "mydealz.de",
    "hotukdeals": "hotukdeals.com",
    "dealabs": "dealabs.com",
    "pepperpl": "pepper.pl",
    "fly4free": "fly4free",
}


class Telegram:
    def __init__(self, http, token: str, chat_id: str) -> None:
        self.http = http
        self.token = token
        self.chat_id = chat_id

    def _call(self, method: str, payload: dict) -> dict:
        return self.http.post_json(API.format(token=self.token, method=method), payload)

    def send(self, text: str) -> bool:
        try:
            self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram odmítl zprávu: %s", exc)
            return False

    def get_updates(self) -> dict:
        return self.http.get(
            API.format(token=self.token, method="getUpdates")
        ).json()


def _fmt_czk(value: float) -> str:
    """1234.5 -> '1 234 Kč' (s pevnou mezerou, ať se to nezalomí)."""
    return f"{value:,.0f}".replace(",", " ") + " Kč"


def format_instant(verdict) -> str:
    offer = verdict.offer
    lines = [f"🔥 <b>{html.escape(offer.name[:180])}</b>", ""]

    price = _fmt_czk(offer.price_czk)
    if verdict.value and verdict.value_ratio is not None:
        real = _fmt_czk(verdict.value.real_value_czk)
        lines.append(
            f"<b>{price}</b>  (běžně ~{real} → "
            f"<b>{_fmt_ratio(verdict.value_ratio)}</b> ceny)"
        )
    else:
        lines.append(f"<b>{price}</b>")

    detail = []
    stock = offer.extra.get("stock")
    if stock:
        detail.append(f"{stock} ks skladem")
    temperature = offer.extra.get("temperature")
    if temperature:
        detail.append(f"{temperature}° na {_SOURCE_LABELS.get(offer.source, offer.source)}")
    airport = offer.extra.get("airport")
    if airport:
        detail.append(f"odlet {airport}")
    if offer.merchant and offer.source != "kinguin":
        detail.append(html.escape(offer.merchant))
    if detail:
        lines.append(" · ".join(detail))

    if verdict.reasons:
        lines.append("")
        for reason in verdict.reasons[:3]:
            lines.append(f"• {html.escape(reason)}")

    lines.append("")
    lines.append(f'<a href="{html.escape(offer.url, quote=True)}">Otevřít nabídku</a>')
    if offer.source != "kinguin":
        lines.append(f"<i>zdroj: {_SOURCE_LABELS.get(offer.source, offer.source)}</i>")
    return "\n".join(lines)


def _fmt_ratio(ratio: float) -> str:
    """U extrémů má desetina smysl — rozdíl mezi 0,5 % a 1,4 % není zaokrouhlení."""
    pct = ratio * 100
    return f"{pct:.1f} %".replace(".", ",") if pct < 10 else f"{pct:.0f} %"


def format_digest(items: list[dict], max_items: int = 25) -> str:
    if not items:
        return "📭 Dnes nic, co by stálo za řeč."

    total = len(items)
    items = sorted(items, key=lambda i: i.get("value_ratio") or 1.0)[:max_items]

    header = f"📋 <b>Denní souhrn — {len(items)} nabídek</b>"
    if total > len(items):
        header = f"📋 <b>Denní souhrn — {len(items)} nejlepších z {total}</b>"
    lines = [header, ""]
    by_category: dict[str, list[dict]] = {}
    for item in items:
        by_category.setdefault(item.get("group") or "Ostatní", []).append(item)

    for group, group_items in by_category.items():
        lines.append(f"<b>{html.escape(group)}</b>")
        for item in group_items:
            price = _fmt_czk(item["price_czk"])
            ratio = item.get("value_ratio")
            suffix = f" — {_fmt_ratio(ratio)} ceny" if ratio else ""
            name = html.escape(item["name"][:90])
            lines.append(f'• <a href="{html.escape(item["url"], quote=True)}">{name}</a>')
            lines.append(f"  {price}{suffix}")
        lines.append("")

    return "\n".join(lines).strip()


def group_of(offer) -> str:
    """Škatulka pro souhrn — hrubá, ale čitelná."""
    category = (offer.category or "").lower()
    if offer.source == "fly4free" or "flight" in category:
        return "✈️ Cestování"
    if "hotel" in category or "urlaub" in category or "reisen" in category:
        return "✈️ Cestování"
    if offer.source == "kinguin":
        return "🎮 Klíče a předplatné"
    if any(w in category for w in ("fashion", "moda", "mode", "bekleidung")):
        return "👕 Móda"
    if any(w in category for w in ("elektronik", "electronics", "elektronika")):
        return "💻 Elektronika"
    return "🛒 Ostatní"
