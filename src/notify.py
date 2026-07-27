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
    "travelfree": "travelfree.info",
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

    def send_long(self, text: str) -> bool:
        """Souhrn se čtyřmi sekcemi se do jedné zprávy nemusí vejít.

        Telegram bere 4096 znaků a odkazy v HTML se do limitu počítají celé,
        takže reálná kapacita je znatelně menší, než kolik je vidět. Bez dělení
        by celý souhrn tiše spadl na chybě.
        """
        return all(self.send(part) for part in split_message(text))

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


HRY = "🎮 Hry"
PREDPLATNE = "🔑 Předplatné a software"
CESTOVANI = "✈️ Cestování"
OSTATNI = "🛒 Ostatní"

# Pořadí sekcí ve zprávě je pevné, ať víš, kam se dívat, a nemusíš to hledat.
GROUP_ORDER = [HRY, PREDPLATNE, CESTOVANI, OSTATNI]


def format_digest(items: list[dict], per_group: int = 8, max_items: int = 32) -> str:
    """Souhrn po sekcích, každá s vlastní kvótou.

    Kvóta je celý smysl rozdělení: bez ní zaberou hry celou zprávu a trhák
    na předplatném nebo letence propadne dolů, kde ho ořízne strop.
    """
    if not items:
        return "📭 Dnes nic, co by stálo za řeč."

    by_group: dict[str, list[dict]] = {}
    for item in items:
        by_group.setdefault(item.get("group") or OSTATNI, []).append(item)

    sections: list[tuple[str, list[dict], int]] = []
    shown = 0
    for group in GROUP_ORDER + [g for g in by_group if g not in GROUP_ORDER]:
        group_items = by_group.get(group)
        if not group_items:
            continue
        quota = min(per_group, max(0, max_items - shown))
        if not quota:
            break
        picked = sorted(group_items, key=_rank_key)[:quota]
        shown += len(picked)
        sections.append((group, picked, len(group_items)))

    total = len(items)
    header = (f"📋 <b>Denní souhrn — {shown} nabídek</b>" if shown == total
              else f"📋 <b>Denní souhrn — {shown} nejlepších z {total}</b>")

    lines = [header, ""]
    for group, picked, available in sections:
        count = f" ({len(picked)} z {available})" if available > len(picked) else ""
        lines.append(f"<b>{html.escape(group)}</b>{count}")
        for item in picked:
            name = html.escape(item["name"][:90])
            lines.append(f'• <a href="{html.escape(item["url"], quote=True)}">{name}</a>')
            lines.append("  " + " · ".join(_detail(item)))
        lines.append("")

    return "\n".join(lines).strip()


def _rank_key(item: dict):
    """Uvnitř sekce rozhoduje popularita, teprve pak sleva.

    U her je to podstatné: seřazeno podle slevy vyhraje vždycky starý titul,
    o který nikdo nestojí. Položky bez známé popularity (všechno mimo hry)
    mají stejnou váhu, takže se u nich pořadí řídí slevou jako dřív.
    """
    popularity = item.get("popularity")
    return (-(popularity if popularity is not None else 0.0),
            item.get("value_ratio") or 1.0)


def _detail(item: dict) -> list[str]:
    parts = [f"<b>{_fmt_czk(item['price_czk'])}</b>"]
    ratio = item.get("value_ratio")
    if ratio:
        parts.append(f"{_fmt_ratio(ratio)} ceny")
    score, count = item.get("reviews_score"), item.get("reviews_count")
    if score is not None and count:
        parts.append(f"★ {score} % z {_fmt_count(count)}")
    released = (item.get("released") or "")[:4]
    if released.isdigit():
        parts.append(released)
    return parts


def _fmt_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} mil.".replace(".", ",")
    if count >= 1000:
        return f"{count // 1000} tis."
    return str(count)


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Rozdělí zprávu na části pod limitem Telegramu, vždy na hranici řádku."""
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        if current and length + len(line) + 1 > limit:
            parts.append("\n".join(current).strip())
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        parts.append("\n".join(current).strip())
    return [p for p in parts if p] or [text]


def group_of(offer) -> str:
    """Škatulka pro souhrn. Čtyři sekce, víc by se přestalo číst."""
    product_type = (offer.extra.get("product_type") or offer.category or "").upper()
    if product_type in _GAME_TYPES:
        return HRY
    if product_type in _SUBSCRIPTION_TYPES:
        return PREDPLATNE

    category = (offer.category or "").lower()
    if offer.source == "fly4free" or any(w in category for w in _TRAVEL_WORDS):
        return CESTOVANI
    return OSTATNI


# Typy produktů z Kinguinu. `INGAME_TOPUP` je navzdory názvu škatulka na
# předplatné — patří do ní YouTube Premium, Spotify i to Gemini za 65 Kč.
_GAME_TYPES = {"GAME", "GAME_ACCOUNT", "DLC", "ALTERGIFT", "RANDOM_KEY",
               "INGAME_CURRENCY", "INGAME_ITEM"}
_SUBSCRIPTION_TYPES = {"INGAME_TOPUP", "SOFTWARE", "PREPAID"}
_TRAVEL_WORDS = ("flight", "hotel", "urlaub", "reisen", "travel", "voyage",
                 "podróże", "podroze", "vakanties")
