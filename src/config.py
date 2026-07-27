"""Načtení konfigurace a tokenů.

Tokeny se berou výhradně z prostředí (GitHub Secrets / .env), nikdy ze souboru
v repozitáři.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(name: str) -> Any:
    path = ROOT / name
    if not path.exists():
        raise FileNotFoundError(f"Chybí konfigurační soubor {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Config:
    def __init__(self) -> None:
        self.raw: dict = _load_yaml("config.yaml")
        self.references: list[dict] = _load_yaml("references.yaml") or []
        self.merchants: dict = _load_yaml("merchants.yaml") or {}
        self.flights: list[dict] = (_load_yaml("flights.yaml") or {}).get("regions", [])

        # Tokeny z prostředí. Jejich absence není fatální — bot umí běžet
        # v --dry-run a bez AI soudce.
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.itad_key = os.environ.get("ITAD_API_KEY", "").strip()

    def get(self, path: str, default: Any = None) -> Any:
        """Přístup přes tečkovou cestu, např. cfg.get("thresholds.instant_ratio")."""
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def db_path(self) -> Path:
        p = Path(self.get("db_path", "data/deals.db"))
        return p if p.is_absolute() else ROOT / p

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def judge_enabled(self) -> bool:
        return bool(self.get("judge.enabled", False)) and bool(self.openrouter_key)

    @property
    def itad_enabled(self) -> bool:
        return bool(self.get("itad.enabled", False)) and bool(self.itad_key)


def load_config() -> Config:
    return Config()
