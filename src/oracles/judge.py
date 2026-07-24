"""AI soudce přes OpenRouter — poslední instance odhadu hodnoty.

Běží dávkově a jen na užší výběr položek, které levnější oracles neumí ocenit.
Většina běhů ho po deduplikaci nezavolá vůbec.

Tři pojistky proti utracení kreditu:

* `max_items_per_call` — jeden dotaz místo N dotazů
* `max_calls_per_day`  — tvrdý denní strop, čítač v databázi
* `enabled` + klíč     — bez klíče modul mlčí a bot běží dál na heuristice

Selhání API nikdy neshodí běh. Když soudce nedojede, položky prostě zůstanou
neoceněné a spadnou do souhrnu.
"""

from __future__ import annotations

import json
import logging
import re

from ..sources.base import Offer
from .base import Value

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

SYSTEM_PROMPT = """Jsi expert na ceny zboží a služeb v Česku a EU.

Pro každou položku odhadni REÁLNOU BĚŽNOU CENU v korunách — tedy kolik by za to
člověk normálně zaplatil u oficiálního prodejce. U předplatného počítej celou
uvedenou dobu (18 měsíců předplatného za 490 Kč/měsíc = 8820 Kč).

Buď střízlivý. Když položku neznáš nebo je to bezcenná šunta, dej real_value_czk
nulu. Radši podhodnoť než nadhodnoť — falešný poplach je horší než zmeškaná sleva.

Odpověz VÝHRADNĚ polem JSON, bez komentáře a bez markdownu:
[{"id":"<id>","real_value_czk":<číslo>,"ships_to_cz":true|false|null,"why":"<max 12 slov česky>"}]

ships_to_cz: doručuje ten obchod do ČR? U digitálního zboží a letenek dej true.
why: krátké vysvětlení, proč je to (ne)zajímavé. Česky."""


class JudgeOracle:
    name = "ai"

    def __init__(self, http, store, cfg) -> None:
        self.http = http
        self.store = store
        self.cfg = cfg
        self.api_key = cfg.openrouter_key
        self.base_url = cfg.get("judge.base_url", "https://openrouter.ai/api/v1/chat/completions")
        self.model = cfg.get("judge.model", "google/gemini-2.5-flash-lite")
        self.max_items = int(cfg.get("judge.max_items_per_call", 25))
        self.max_calls_per_day = int(cfg.get("judge.max_calls_per_day", 20))
        self.timeout_s = int(cfg.get("judge.timeout_s", 60))

    def available(self) -> bool:
        if not self.api_key:
            return False
        if self.store.daily_counter("judge") >= self.max_calls_per_day:
            log.warning("Denní strop volání AI soudce vyčerpán")
            return False
        return True

    def judge(self, offers: list[Offer]) -> dict[str, Value]:
        """Ocení dávku položek. Vrací mapu uid -> Value (jen pro ty, co se povedly)."""
        if not offers or not self.available():
            return {}

        batch = offers[: self.max_items]
        payload_items = [
            {
                "id": o.uid,
                "name": o.name[:180],
                "price_czk": round(o.price_czk),
                "category": o.category,
                "merchant": o.merchant,
                "source": o.source,
            }
            for o in batch
        ]

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Slevy deal bot",
        }

        try:
            self.store.bump_daily_counter("judge")
            data = self.http.post_json(self.base_url, body, headers, self.timeout_s)
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            log.warning("AI soudce selhal: %s", exc)
            return {}

        parsed = _parse_json_array(content)
        if parsed is None:
            log.warning("AI soudce vrátil nečitelnou odpověď: %s", content[:200])
            return {}

        by_uid = {o.uid: o for o in batch}
        out: dict[str, Value] = {}
        for row in parsed:
            uid = str(row.get("id", ""))
            offer = by_uid.get(uid)
            if offer is None:
                continue
            try:
                real_value = float(row.get("real_value_czk") or 0)
            except (TypeError, ValueError):
                continue
            if real_value <= 0:
                continue

            out[uid] = Value(
                real_value_czk=real_value,
                origin="ai",
                note=(row.get("why") or "").strip()[:120] or None,
                # AI je poslední instance, ne autorita — nižší důvěra než u historie.
                confidence=0.7,
            )
            ships = row.get("ships_to_cz")
            if isinstance(ships, bool):
                offer.extra["ai_ships_to_cz"] = ships

        log.info("AI soudce ocenil %s z %s položek", len(out), len(batch))
        return out


def _parse_json_array(content: str) -> list[dict] | None:
    text = _FENCE_RE.sub("", content or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        # Modely občas obalí pole textem — zkusíme vyseknout první [...] blok.
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start: end + 1])
        except ValueError:
            return None

    if isinstance(data, dict):
        for key in ("items", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return None
    return data if isinstance(data, list) else None
