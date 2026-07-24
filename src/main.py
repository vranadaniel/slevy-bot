"""Orchestrace běhu.

    python -m src.main --dry-run              projde zdroje, nic neodešle ani nezapíše
    python -m src.main --dry-run --explain X  rozepíše signály u konkrétní položky
    python -m src.main --bootstrap            první běh: označí feedy za viděné, nealertuje
    python -m src.main                        ostrý sken s okamžitými upozorněními
    python -m src.main --digest               odešle denní souhrn
    python -m src.main --test-telegram        ověří token a chat_id
    python -m src.main --print-chat-id        vypíše chat_id z posledních zpráv botovi
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config
from .fx import load_fx
from .net import build_http
from .notify import Telegram, format_digest, format_instant, group_of
from .oracles.declared import DeclaredOracle
from .oracles.history import HistoryOracle
from .oracles.itad import ItadOracle
from .oracles.judge import JudgeOracle
from .oracles.refs import ReferenceOracle
from .score import DIGEST, INSTANT, Scorer
from .shipping import ShippingPolicy
from .sources.base import CATALOG
from .sources.fly4free import Fly4FreeSource
from .sources.kinguin import KinguinSource
from .sources.pepper import build_pepper_sources
from .store import Store

log = logging.getLogger("slevy")


def build_sources(http, fx, cfg) -> list:
    sources = []
    if cfg.get("sources.kinguin.enabled", True):
        sources.append(KinguinSource(http, fx, cfg))
    if cfg.get("sources.pepper.enabled", True):
        sources.extend(build_pepper_sources(http, fx, cfg))
    if cfg.get("sources.fly4free.enabled", True):
        sources.append(Fly4FreeSource(http, fx, cfg))
    return sources


def collect(sources) -> list:
    """Stáhne všechny zdroje. Spadlý zdroj se přeskočí, běh pokračuje."""
    offers = []
    for source in sources:
        try:
            offers.extend(source.fetch())
        except Exception as exc:  # noqa: BLE001
            log.error("Zdroj %s selhal a přeskakuji ho: %s", source.name, exc)
    return offers


def run_scan(cfg, args) -> int:
    http = build_http(cfg)
    store = Store(cfg.db_path)
    fx = load_fx(http, store)

    sources = build_sources(http, fx, cfg)
    offers = collect(sources)
    log.info("Celkem %s nabídek z %s zdrojů", len(offers), len(sources))

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump([o.__dict__ for o in offers], fh, ensure_ascii=False,
                      indent=2, default=str)
        log.info("Syrová data uložena do %s", args.dump)

    # Feedy: co jsme už viděli, nemá smysl znovu řešit.
    if not args.dry_run:
        fresh = []
        for offer in offers:
            if offer.kind == CATALOG:
                store.record_price(offer.source, offer.uid, offer.name, offer.url,
                                   offer.category, offer.price_czk)
                fresh.append(offer)
            elif not store.is_seen(offer.source, offer.uid):
                fresh.append(offer)
        store.commit()
        log.info("Po odečtení už viděných zbývá %s nabídek", len(fresh))
    else:
        fresh = offers

    history = HistoryOracle(store)
    shipping = ShippingPolicy(cfg.merchants)
    oracles = [history, ReferenceOracle(cfg.references)]

    if cfg.itad_enabled:
        itad = ItadOracle(http, store, fx, cfg)
        # Dávkové doplnění cache musí proběhnout před scoringem — jinak by se
        # oracle ptal na každou hru zvlášť.
        itad.prepare(fresh)
        oracles.append(itad)
    elif cfg.get("itad.enabled", False):
        log.info("ITAD vypnutý (chybí ITAD_API_KEY), hry zůstanou bez ocenění")

    oracles.append(DeclaredOracle())
    scorer = Scorer(cfg, store, oracles, history, shipping)

    verdicts = scorer.prescore(fresh)

    # AI soudce jen na to, co levné oracles neocenily a co vypadá slibně.
    # Při bootstrapu se stejně nic neodesílá, takže by to byly vyhozené peníze.
    if cfg.judge_enabled and not args.no_ai and not args.bootstrap:
        judge = JudgeOracle(http, store, cfg)
        candidates = scorer.ai_candidates(verdicts, judge.max_items)
        if candidates:
            log.info("AI soudce dostane %s položek", len(candidates))
            scorer.apply_ai(verdicts, judge.judge(candidates))
    elif not args.no_ai:
        log.info("AI soudce vypnutý (chybí klíč nebo judge.enabled=false)")

    if args.explain:
        return explain(verdicts, args.explain)

    instant = [v for v in verdicts if v.level == INSTANT]
    digest = [v for v in verdicts if v.level == DIGEST]
    instant.sort(key=lambda v: v.value_ratio or 1.0)

    if args.dry_run:
        report(verdicts, instant, digest)
        store.close()
        return 0

    if args.bootstrap:
        for verdict in verdicts:
            if verdict.offer.kind != CATALOG:
                store.mark_seen(verdict.offer.source, verdict.offer.uid)
        store.commit()
        store.close()
        log.info("Bootstrap hotov: %s položek označeno jako viděné, nic se neodeslalo",
                 len(verdicts))
        return 0

    telegram = Telegram(http, cfg.telegram_token, cfg.telegram_chat_id) \
        if cfg.has_telegram else None
    if telegram is None:
        log.warning("Telegram není nastavený, upozornění se jen zapíší do fronty")

    sent = 0
    max_instant = int(cfg.get("telegram.max_instant_per_run", 8))
    realert_drop = float(cfg.get("dedup.realert_drop", 0.15))
    realert_days = int(cfg.get("dedup.realert_days", 30))

    def fresh_enough(verdict) -> bool:
        """Katalog vidíme pořád dokola, takže bez tohohle by ti tytéž klíče
        k Windows chodily v souhrnu každý večer znovu."""
        offer = verdict.offer
        if offer.kind != CATALOG:
            return True
        return store.should_alert(offer.source, offer.uid, offer.price_czk,
                                  realert_drop, realert_days)

    for verdict in instant:
        offer = verdict.offer
        if not fresh_enough(verdict):
            continue

        if sent >= max_instant:
            # Zbytek nezahazujeme, jen ho odsuneme do souhrnu.
            _queue(store, verdict)
            continue

        if telegram and telegram.send(format_instant(verdict)):
            sent += 1
            store.mark_alerted(offer.source, offer.uid, offer.price_czk, INSTANT)
        else:
            _queue(store, verdict)

    for verdict in digest:
        if fresh_enough(verdict):
            _queue(store, verdict)

    for verdict in verdicts:
        if verdict.offer.kind != CATALOG:
            store.mark_seen(verdict.offer.source, verdict.offer.uid)

    store.prune(keep_days=60)
    store.commit()
    log.info("Odesláno %s okamžitých upozornění, ve frontě souhrnu %s položek",
             sent, store.digest_size())
    store.close()
    return 0


def _queue(store, verdict) -> None:
    offer = verdict.offer
    store.queue_digest(offer.source, offer.uid, {
        "name": offer.name,
        "url": offer.url,
        "price_czk": offer.price_czk,
        "value_ratio": verdict.value_ratio,
        "group": group_of(offer),
    })
    # Zapsat i u souhrnu, jinak by deduplikace neměla o čem rozhodovat příště.
    store.mark_alerted(offer.source, offer.uid, offer.price_czk, DIGEST)


def run_digest(cfg) -> int:
    http = build_http(cfg)
    store = Store(cfg.db_path)
    items = store.pop_digest()
    max_items = int(cfg.get("telegram.max_digest_items", 25))

    if not items:
        log.info("Fronta souhrnu je prázdná, nic neposílám")
        store.close()
        return 0

    if not cfg.has_telegram:
        print(format_digest(items, max_items))
        store.close()
        return 0

    telegram = Telegram(http, cfg.telegram_token, cfg.telegram_chat_id)
    ok = telegram.send(format_digest(items, max_items))
    store.commit()
    store.close()
    return 0 if ok else 1


def explain(verdicts, needle: str) -> int:
    needle = needle.lower()
    hits = [v for v in verdicts
            if needle == v.offer.uid.lower() or needle in v.offer.name.lower()]
    if not hits:
        print(f"Nic nesedí na '{needle}'.")
        return 1

    for verdict in hits[:10]:
        offer = verdict.offer
        print("=" * 70)
        print(offer.name)
        print(f"  zdroj          {offer.source} ({offer.kind})  uid={offer.uid}")
        print(f"  cena           {offer.price_czk:.0f} Kč"
              f"  ({offer.price_orig} {offer.currency})")
        print(f"  důvěryhodnost  {offer.credibility:.2f}")
        print(f"  doručení do ČR {verdict.ships_to_cz}")
        print(f"  hist. minimum  {verdict.all_time_low}")
        if verdict.value:
            print(f"  hodnota        {verdict.value.real_value_czk:.0f} Kč"
                  f"  ({verdict.value.origin}, důvěra {verdict.value.confidence:.2f})")
            print(f"                 {verdict.value.note}")
        else:
            print("  hodnota        NEURČENA")
        if verdict.value_ratio is not None:
            print(f"  poměr          {verdict.value_ratio * 100:.2f} % reálné ceny")
        print(f"  VERDIKT        {verdict.level.upper()}")
        for reason in verdict.reasons:
            print(f"    • {reason}")
        print(f"  {offer.url}")
    return 0


def report(verdicts, instant, digest) -> None:
    print()
    print(f"Vyhodnoceno {len(verdicts)} nabídek")
    print(f"  INSTANT {len(instant)}   DIGEST {len(digest)}")
    print()
    for label, group in (("INSTANT", instant), ("DIGEST", digest[:20])):
        if not group:
            continue
        print(f"--- {label} ---")
        for verdict in sorted(group, key=lambda v: v.value_ratio or 1.0):
            ratio = f"{verdict.value_ratio * 100:5.1f} %" if verdict.value_ratio else "   ?  "
            origin = verdict.value.origin if verdict.value else "-"
            print(f"  {verdict.offer.price_czk:>8.0f} Kč  {ratio}  "
                  f"[{origin:<10}] {verdict.offer.name[:60]}")
        print()


def run_check_itad(cfg) -> int:
    """Ověří klíč k ITAD na jedné známé hře a ukáže, v jaké měně chodí ceny."""
    if not cfg.itad_key:
        print("Chybí ITAD_API_KEY v prostředí.")
        return 1

    http = build_http(cfg)
    country = cfg.get("itad.country", "CZ")
    headers = {"ITAD-API-Key": cfg.itad_key, "Content-Type": "application/json"}
    print(f"Klíč načten ({len(cfg.itad_key)} znaků), ptám se na zemi {country}.\n")

    try:
        found = http.post_json(
            "https://api.isthereanydeal.com/lookup/id/title/v1", ["Portal 2"], headers
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Překlad názvu selhal: {exc}")
        return 1

    game_id = found.get("Portal 2")
    if not game_id:
        print("ITAD nezná ani 'Portal 2' — to je divné, zkus to za chvíli znovu.")
        return 1
    print(f"Překlad názvu OK: Portal 2 -> {game_id}")

    try:
        data = http.post_json(
            f"https://api.isthereanydeal.com/games/historylow/v1?country={country}",
            [game_id], headers,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nHistorické minimum SELHALO: {exc}")
        print("\n'Invalid or expired api key' znamená, že klíč dorazil, ale ITAD ho nezná.")
        print("Nejčastěji je to zaměněná hodnota — na stránce aplikace je vedle API klíče")
        print("i OAuth Client ID a Client Secret. Potřebujeme API key.")
        return 1

    low = (data[0] if data else {}).get("low") or {}
    price = low.get("price") or {}
    regular = low.get("regular") or {}
    print("\nHistorické minimum OK — klíč funguje.")
    print(f"  nejníž  {price.get('amount')} {price.get('currency')}"
          f"  ({(low.get('shop') or {}).get('name')})")
    print(f"  běžně   {regular.get('amount')} {regular.get('currency')}")
    if price.get("currency") and price["currency"] != "CZK":
        print(f"\n  Pozor: ceny chodí v {price['currency']}, ne v CZK."
              f" Bot je přepočítá kurzem ČNB.")
    return 0


def run_test_telegram(cfg) -> int:
    if not cfg.has_telegram:
        print("Chybí TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID v prostředí.")
        return 1
    http = build_http(cfg)
    telegram = Telegram(http, cfg.telegram_token, cfg.telegram_chat_id)
    ok = telegram.send(
        "✅ <b>Test spojení</b>\n\nBot na slevy je nastavený správně."
    )
    print("Odesláno." if ok else "Odeslání selhalo.")
    return 0 if ok else 1


def run_print_chat_id(cfg) -> int:
    if not cfg.telegram_token:
        print("Chybí TELEGRAM_BOT_TOKEN v prostředí.")
        return 1
    http = build_http(cfg)
    telegram = Telegram(http, cfg.telegram_token, "")
    try:
        data = telegram.get_updates()
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram neodpověděl: {exc}")
        return 1

    results = data.get("result") or []
    if not results:
        print("Žádné zprávy. Napiš svému botovi na Telegramu cokoliv a spusť znovu.")
        return 1
    seen = set()
    for update in results:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") and chat["id"] not in seen:
            seen.add(chat["id"])
            print(f"chat_id = {chat['id']}   ({chat.get('type')}, "
                  f"{chat.get('username') or chat.get('title') or '?'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot na lov extrémních slev")
    parser.add_argument("--dry-run", action="store_true",
                        help="projde zdroje a vypíše skóre, nic neodešle ani nezapíše")
    parser.add_argument("--explain", metavar="UID_NEBO_NÁZEV",
                        help="rozepíše jednotlivé signály u konkrétní položky")
    parser.add_argument("--bootstrap", action="store_true",
                        help="první běh: označí feedy za viděné a nic neodešle")
    parser.add_argument("--digest", action="store_true", help="odešle denní souhrn")
    parser.add_argument("--dump", metavar="SOUBOR", help="uloží syrové nabídky do JSON")
    parser.add_argument("--no-ai", action="store_true", help="vypne AI soudce")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--print-chat-id", action="store_true")
    parser.add_argument("--check-itad", action="store_true",
                        help="ověří klíč k IsThereAnyDeal a měnu odpovědí")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = load_config()

    if args.print_chat_id:
        return run_print_chat_id(cfg)
    if args.check_itad:
        return run_check_itad(cfg)
    if args.test_telegram:
        return run_test_telegram(cfg)
    if args.digest:
        return run_digest(cfg)
    return run_scan(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
