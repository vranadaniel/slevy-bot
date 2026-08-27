"""Orchestrace běhu.

    python -m src.main --dry-run              projde zdroje, nic neodešle ani nezapíše
    python -m src.main --only travel          rychlý běh jen na letenkách a hotelech
    python -m src.main --dry-run --explain X  rozepíše signály u konkrétní položky
    python -m src.main --bootstrap            první běh: označí feedy za viděné, nealertuje
    python -m src.main                        ostrý sken s okamžitými upozorněními
    python -m src.main --digest               odešle denní souhrn
    python -m src.main --stats                co bot nasbíral, bez sahání na síť
    python -m src.main --backup               konzistentní kopie databáze
    python -m src.main --test-telegram        ověří token a chat_id
    python -m src.main --print-chat-id        vypíše chat_id z posledních zpráv botovi
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from .config import load_config
from .fx import load_fx
from .net import build_http
from .notify import (HRY, Telegram, format_digest, format_health,
                     format_instant, format_term, format_watch,
                     format_watch_list, group_of)
from .oracles.declared import DeclaredOracle
from .oracles.flights import FlightOracle
from .oracles.history import HistoryOracle
from .oracles.itad import ItadOracle
from .oracles.judge import JudgeOracle
from .oracles.refs import ReferenceOracle
from .score import DIGEST, INSTANT, NONE, Scorer
from .shipping import ShippingPolicy
from .sources.base import CATALOG
from .sources.kinguin import KinguinSource
from .sources.pepper import build_pepper_sources
from .sources.ryanair import RyanairSource
from .sources.wizzair import WizzAirSource
from .sources.travel import build_travel_sources
from .sources.travelpayouts import TravelpayoutsSource
from .watch import (NAPOVEDA, ChybaZadani, Watch, WatchEngine,
                    _bez_diakritiky, parse_watch)
from .store import Store

log = logging.getLogger("slevy")


def build_sources(http, fx, cfg, only: str | None = None, store=None) -> list:
    """Zdroje k projití. `only` omezí běh na jednu rodinu zdrojů.

    Cestovatelské feedy se vyplatí číst častěji než katalog — error fare mizí
    během hodin, kdežto klíč k Windows počká. `--only travel` proto umí projít
    jen je, za pár sekund a šest požadavků.
    """
    families = {
        "kinguin": lambda: [KinguinSource(http, fx, cfg)],
        "pepper": lambda: build_pepper_sources(http, fx, cfg),
        # Ryanair patří do rodiny "travel": chodí spolu s ostatními letenkami
        # na rychlém timeru, i když je to katalog a ne feed.
        "travel": lambda: build_travel_sources(http, fx, cfg, store)
                          + ([RyanairSource(http, fx, cfg)]
                             if cfg.get("sources.ryanair.enabled", True) else [])
                          + ([WizzAirSource(http, fx, cfg, store)]
                             if cfg.get("sources.wizzair.enabled", True) else [])
                          # Bez tokenu se zdroj ani nezakládá — jinak by každý
                          # běh hlásil, že ho přeskakuje.
                          + ([TravelpayoutsSource(http, fx, cfg, store)]
                             if cfg.travelpayouts_enabled else []),
    }
    if only and only not in families:
        raise SystemExit(f"Neznámá rodina zdrojů '{only}'. "
                         f"Na výběr je: {', '.join(families)}.")

    sources = []
    for family, build in families.items():
        if only and family != only:
            continue
        if cfg.get(f"sources.{family}.enabled", True):
            sources.extend(build())
    return sources


def collect(sources, store=None) -> tuple[list, list[str]]:
    """Stáhne všechny zdroje. Spadlý zdroj se přeskočí, běh pokračuje.

    Vrací i seznam zdrojů, které si zaslouží zprávu. Bez toho bylo selhání
    **tiché**: log napsal „přeskakuji" a bot mohl týden mlčet, aniž by to
    vypadalo jinak než na to, že prostě nejsou slevy.

    Hlásí se až `PRAH_SELHANI` selhání po sobě, a to právě jednou — jinak by
    zablokovaný zdroj posílal zprávu každých deset minut. Po prvním úspěchu
    se čítač nuluje, takže příští výpadek zase upozorní.
    """
    offers, hlasit = [], []
    for source in sources:
        try:
            nabidky = source.fetch()
        except Exception as exc:  # noqa: BLE001
            log.error("Zdroj %s selhal a přeskakuji ho: %s", source.name, exc)
            if store is not None:
                pocet = store.source_failed(source.name)
                if pocet == PRAH_SELHANI:
                    hlasit.append(f"{source.name} selhal {pocet}x po sobě: {exc}")
            continue

        offers.extend(nabidky)
        if store is not None and store.source_ok(source.name) >= PRAH_SELHANI:
            hlasit.append(f"{source.name} zase funguje")
    return offers, hlasit


# Kolik selhání po sobě je porucha, ne výpadek sítě. Cestování běží po deseti
# minutách, takže tři selhání znamenají půl hodiny ticha.
PRAH_SELHANI = 3


def run_scan(cfg, args) -> int:
    http = build_http(cfg)
    store = Store(cfg.db_path)
    fx = load_fx(http, store)

    # Při dry-runu se stahuje vždycky nanovo. Zapamatovaný ETag by způsobil,
    # že druhé spuštění nevypíše nic, a ladění by bylo k ničemu.
    sources = build_sources(http, fx, cfg, args.only,
                            None if args.dry_run else store)
    # Při dry-runu se zdraví zdrojů nezapisuje — ladicí běh nesmí měnit stav.
    offers, hlasit = collect(sources, None if args.dry_run else store)
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
    # Ceník letenek stojí před ITAD i před AI: je zadarmo, okamžitý a u téže
    # trasy odpoví pokaždé stejně.
    oracles = [history, ReferenceOracle(cfg.references), FlightOracle(cfg.flights)]

    itad = None
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
        return explain(verdicts, args.explain, itad)

    # Cizí uzly (Dublin, Frankfurt) mají smysl jen u dálkových tras. Musí to
    # proběhnout až po AI — region se rozpozná při oceňování.
    verdicts = drop_pointless_hubs(
        verdicts, float(cfg.get("sources.travel.hub_min_typical_czk", 0)))

    # Drobnosti za pár korun mají díky poměru systémovou výhodu nad velkými
    # hrami. Musí to proběhnout před rozdělením, ať se to týká i upozornění.
    verdicts = drop_cheap_games(
        verdicts, float(cfg.get("games.min_value_czk", 0)))

    instant = [v for v in verdicts if v.level == INSTANT]
    digest = [v for v in verdicts if v.level == DIGEST]
    instant.sort(key=lambda v: v.value_ratio or 1.0)

    # Hodnocení hráčů se doptáváme až tady, na hrstce položek mířících do
    # souhrnu — endpoint bere jednu hru na dotaz.
    if itad is not None:
        itad.enrich_popularity([v.offer for v in digest])
        digest = drop_unpopular(
            digest, float(cfg.get("itad.min_popularity", 0.6)),
            bool(cfg.get("itad.require_known_popularity", True)))

    # Nejlevnější termín na téže trase. Až tady, na hrstce, co se chystá
    # odejít — je to jeden požadavek na trasu. Nikdy z toho nevzniká hodnota,
    # je to údaj do zprávy; porovnávat cenu dopravce s trhem je ten kruh,
    # kvůli kterému bot hlásil Krakov za 748 Kč jako trhák.
    if cfg.travelpayouts_token:
        TravelpayoutsSource(http, fx, cfg).enrich_calendar(
            [v.offer for v in instant + digest],
            int(cfg.get("sources.travelpayouts.calendar_max_per_run", 12)))

    if args.dry_run:
        report(verdicts, instant, digest, scorer)
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

    # Porucha zdroje jde ven hned a mimo frontu souhrnu. Čekat s ní do večera
    # by znamenalo, že o půldenním výpadku víš až po něm.
    if hlasit:
        for zprava in hlasit:
            log.warning("Zdraví zdrojů: %s", zprava)
        if telegram:
            telegram.send(format_health(hlasit))

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
        if verdict.offer.kind != CATALOG and not _retry_later(scorer, verdict):
            store.mark_seen(verdict.offer.source, verdict.offer.uid)

    store.prune(keep_days=60)
    store.commit()
    log.info("Odesláno %s okamžitých upozornění, ve frontě souhrnu %s položek",
             sent, store.digest_size())
    store.close()
    return 0


def _retry_later(scorer, verdict) -> bool:
    """Má se položka z feedu nechat na příští běh místo označení za viděnou?

    Letenku ani hotel neumí ocenit žádný levný oracle, takže hodnota přijde až
    od AI soudce. Když soudce nedojel (vyčerpaný denní strop, výpadek API),
    zapsat položku do `seen` by ji umlčelo natrvalo — a to je přesně ten typ
    nabídky, kvůli které bot existuje. Necháme ji tedy na příště.

    Samo se to zastaví: položka za pár dní vypadne z RSS a víc se nenabídne.
    """
    return (verdict.value is None
            and verdict.offer.credibility >= scorer.min_cred_ai)


def drop_unpopular(digest: list, min_popularity: float,
                   require_known: bool = True) -> list:
    """Vyhodí hry, o které nikdo nestojí.

    Sleva sama o sobě vytahuje nahoru staré tituly — čím míň lidí hru chce,
    tím hlouběji jde cena.

    **Neznámá popularita je u her důvod k mlčení, ne k propuštění.** Původně to
    bylo naopak, s odůvodněním „mlčet o něčem jen proto, že o tom nemáme data,
    by bylo horší". To platilo, dokud byl katalog poloviční a neznámých pár.
    Po rozšíření na celých 10 000 produktů se poměr obrátil: neznámá popularita
    znamená buď že ji ITAD vůbec nezná (tedy obskurní šunta), nebo že jsme
    vyčerpali strop dotazů — a v obou případech je to slabší kandidát než hra,
    o které víme, že ji lidi chtějí. Sekce se pak plnila bezcennými tituly za
    pár korun, protože ty mají nejextrémnější poměr ceny.

    Týká se to jen HER. U předplatného, cestování a ostatního se popularita
    nezjišťuje vůbec, takže by tenhle filtr vymazal celý souhrn.
    """
    if min_popularity <= 0:
        return digest

    kept, nezajimave, nezname = [], 0, 0
    for verdict in digest:
        popularity = verdict.offer.extra.get("popularity")
        je_hra = group_of(verdict.offer) == HRY

        if popularity is None:
            if je_hra and require_known:
                nezname += 1
                continue
            kept.append(verdict)
            continue

        if popularity < min_popularity:
            nezajimave += 1
            continue
        kept.append(verdict)

    if nezajimave or nezname:
        log.info("Ze souhrnu vypadlo %s neatraktivních her a %s s neznámou "
                 "popularitou", nezajimave, nezname)
    return kept


def drop_cheap_games(verdicts: list, min_value_czk: float) -> list:
    """Zahodí hry, které nestojí za řeč ani v plné ceně.

    O úrovni rozhoduje POMĚR ceny k hodnotě, a ten levné hry systémově
    zvýhodňuje: hra za 3 Kč z původních 100 vyjde na 3 %, kdežto AAA za 200 Kč
    z patnácti stovek na 13 %. Souhrn se tím plnil drobnostmi, o které nikdo
    nestojí, a velký titul v obrovské slevě mezi nimi zapadl.

    Popularita to nevyřeší — ta měří, jestli hru někdo hrál, ne jestli je to
    velký titul. Povedená indie hra má hodnocení jako AAA. Rozhodnout musí
    **ceníková cena**: pod šesti stovkami velká hra prakticky není a nad nimi
    prakticky není obskurní (změřeno na katalogu GOG, viz `config.yaml`).

    Sedí to vedle `drop_unpopular` ze stejného důvodu — je to věc výběru do
    zprávy, ne ocenění, a `score.py` nemá vědět, že něco jako hra existuje.
    """
    if min_value_czk <= 0:
        return verdicts

    kept, levne = [], 0
    for verdict in verdicts:
        hodnota = verdict.value.real_value_czk if verdict.value else None
        if (verdict.level != NONE and hodnota is not None
                and hodnota < min_value_czk
                and group_of(verdict.offer) == HRY):
            levne += 1
            continue
        kept.append(verdict)

    if levne:
        log.info("Zahozeno %s her, které ani v plné ceně nestojí %s Kč",
                 levne, int(min_value_czk))
    return kept


def drop_pointless_hubs(verdicts: list, min_typical_czk: float) -> list:
    """Zahodí nabídky z cizího uzlu, u kterých se ta cesta nevyplatí.

    Doletět do Dublinu za osm stovek a ušetřit deset tisíc na Ameriku dává
    smysl. Jet do Frankfurtu kvůli Malaze ne — na cestě do uzlu bys utratil
    víc, než ušetříš. Nabídka z cizího letiště proto projde jen u dálkového
    cíle.

    Rozhoduje `typical_czk` regionu z `flights.yaml`, který tam už je; žádný
    další ruční seznam. Data se dělí sama: dálkové regiony mají 12 000 Kč
    a výš, celá Evropa 2 500-4 500 a Blízký východ 5 500.

    Když region nerozpoznáme, položka se zahodí. U cizího odletu je mlčení
    správná odpověď — nevíme, jestli se ta cesta vyplatí, a nabídka z Frankfurtu
    má proti nabídce z Prahy důkazní břemeno navíc.
    """
    if min_typical_czk <= 0:
        return verdicts

    kept, dropped = [], 0
    for verdict in verdicts:
        hub = verdict.offer.extra.get("hub_departure")
        if not hub:
            kept.append(verdict)
            continue

        typical = verdict.offer.extra.get("flight_typical_czk") or 0
        if typical >= min_typical_czk:
            kept.append(verdict)
        else:
            dropped += 1

    if dropped:
        log.info("Zahozeno %s nabídek z cizích uzlů, kde se cesta nevyplatí",
                 dropped)
    return kept


def _queue(store, verdict) -> None:
    offer = verdict.offer
    store.queue_digest(offer.source, offer.uid, {
        "name": offer.name,
        "url": offer.url,
        "price_czk": offer.price_czk,
        "value_ratio": verdict.value_ratio,
        # Plná cena patří do zprávy: „13 %" samo o sobě neřekne, jestli jde
        # o velkou hru za dvě stě, nebo o drobnost za tři koruny.
        "value_czk": verdict.value.real_value_czk if verdict.value else None,
        "group": group_of(offer),
        "popularity": offer.extra.get("popularity"),
        "reviews_score": offer.extra.get("reviews_score"),
        "reviews_count": offer.extra.get("reviews_count"),
        "released": offer.extra.get("released"),
        # Termín se skládá teď, ne až večer — fronta si nese hotový text
        # a nemusí si pamatovat, ze kterých polí vznikl.
        "term": format_term(offer.extra),
        # Kalendář se ptá teď; večer už by ten levnější termín mohl být pryč.
        "kalendar_min_czk": offer.extra.get("kalendar_min_czk"),
        "kalendar_min_date": offer.extra.get("kalendar_min_date"),
        "kalendar_url": offer.extra.get("kalendar_url"),
    })
    # Zapsat i u souhrnu, jinak by deduplikace neměla o čem rozhodovat příště.
    store.mark_alerted(offer.source, offer.uid, offer.price_czk, DIGEST)


def run_digest(cfg) -> int:
    http = build_http(cfg)
    store = Store(cfg.db_path)
    items = store.pop_digest()
    per_group = int(cfg.get("digest.per_group", 8))
    max_items = int(cfg.get("digest.max_items", 32))

    if not items:
        log.info("Fronta souhrnu je prázdná, nic neposílám")
        store.close()
        return 0

    text = format_digest(items, per_group, max_items)
    if not cfg.has_telegram:
        print(text)
        store.close()
        return 0

    telegram = Telegram(http, cfg.telegram_token, cfg.telegram_chat_id)
    ok = telegram.send_long(text)
    store.commit()
    store.close()
    return 0 if ok else 1


def explain(verdicts, needle: str, itad=None) -> int:
    needle = needle.lower()
    hits = [v for v in verdicts
            if needle == v.offer.uid.lower() or needle in v.offer.name.lower()]
    if not hits:
        print(f"Nic nesedí na '{needle}'.")
        return 1

    hits = hits[:10]
    # Popularita se jinak zjišťuje jen u souhrnu; tady je to hlavní důvod,
    # proč se člověk ptá — podle čeho jinak nastavit itad.min_popularity.
    if itad is not None:
        itad.enrich_popularity([v.offer for v in hits])

    for verdict in hits:
        offer = verdict.offer
        print("=" * 70)
        print(offer.name)
        print(f"  zdroj          {offer.source} ({offer.kind})  uid={offer.uid}")
        print(f"  cena           {offer.price_czk:.0f} Kč"
              f"  ({offer.price_orig} {offer.currency})")
        print(f"  důvěryhodnost  {offer.credibility:.2f}")
        print(f"  doručení do ČR {verdict.ships_to_cz}")
        print(f"  hist. minimum  {verdict.all_time_low}")
        print(f"  sekce souhrnu  {group_of(offer)}")
        popularity = offer.extra.get("popularity")
        if popularity is not None:
            score = offer.extra.get("reviews_score")
            count = offer.extra.get("reviews_count")
            source = (f"{score} % z {count} hodnocení" if count
                      else "bez hodnocení, bere se prodejnost na Kinguinu")
            print(f"  popularita     {popularity:.2f}  ({source}"
                  f", vydáno {offer.extra.get('released') or '?'})")
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


def report(verdicts, instant, digest, scorer=None) -> None:
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
            popularity = verdict.offer.extra.get("popularity")
            pop = f"{popularity:.2f}" if popularity is not None else "  - "
            print(f"  {verdict.offer.price_czk:>8.0f} Kč  {ratio}  pop {pop}  "
                  f"[{origin:<10}] {verdict.offer.name[:60]}")
        print()

    if scorer is not None:
        _report_tesne_pod(verdicts, scorer)


def _report_tesne_pod(verdicts, scorer, limit: int = 15) -> None:
    """Oceněné nabídky, které práh minuly — a o kolik.

    Tohle je jediný způsob, jak poznat, jestli jsou prahy utažené správně.
    Prázdná sekce znamená, že se nic neblíží; plná sekce položek těsně nad
    prahem znamená, že se práh možná ubírá o kus moc.

    Neoceněné položky se sem nedávají: ty práh neminuly, jen jim nikdo neurčil
    hodnotu, a to je jiná diagnóza.
    """
    blizko = []
    for verdict in verdicts:
        if verdict.level != NONE or verdict.value_ratio is None:
            continue
        _, digest_prah = scorer._thresholds_for(verdict.offer)
        if digest_prah <= 0:
            continue
        # Kolikrát dál je od prahu, než aby prošla. 1,2 znamená "chybělo 20 %".
        blizko.append((verdict.value_ratio / digest_prah, digest_prah, verdict))

    if not blizko:
        print("--- TĚSNĚ POD PRAHEM ---")
        print("  Nic se prahu neblíží.\n")
        return

    blizko.sort(key=lambda t: t[0])
    print(f"--- TĚSNĚ POD PRAHEM ({len(blizko)} oceněných nabídek neprošlo) ---")
    print("  chybělo   cena        poměr  práh   zdroj hodnoty")
    for odstup, prah, verdict in blizko[:limit]:
        chybelo = (odstup - 1.0) * 100
        print(f"  {chybelo:>6.0f} %  {verdict.offer.price_czk:>8.0f} Kč  "
              f"{verdict.value_ratio * 100:5.1f} %  {prah * 100:3.0f} %  "
              f"[{verdict.value.origin:<10}] {verdict.offer.name[:46]}")
    print()


def run_backup(cfg) -> int:
    """Denní záloha databáze. Volá ji systemd timer `slevy-backup`."""
    store = Store(cfg.db_path)
    cil_dir = cfg.db_path.parent / cfg.get("backup.dir", "zalohy")
    keep = int(cfg.get("backup.keep", 7))
    try:
        cil = store.backup(cil_dir, keep)
    finally:
        store.close()

    velikost = cil.stat().st_size / 1_048_576
    log.info("Záloha hotová: %s (%.1f MB), držím posledních %s", cil, velikost, keep)
    print(f"Záloha: {cil} ({velikost:.1f} MB)")
    return 0


def run_stats(cfg) -> int:
    """Co bot nasbíral. Čte jen databázi, na síť nesahá.

    Vzniklo proto, že po přidání tří katalogových zdrojů u cestování a po
    zdvojnásobení katalogu Kinguinu nebylo jak zjistit, jestli to celé funguje.
    Nejdůležitější sloupec je **zralé**: kolik položek už má porovnání proti
    dřívější ceně, tedy kolik jich `HistoryOracle` vůbec umí ocenit. Dokud je
    nula, katalogový zdroj mlčí právem — a je dobré to vidět černé na bílém,
    místo aby to vypadalo jako porucha.
    """
    store = Store(cfg.db_path)
    print(f"Databáze: {cfg.db_path}\n")

    # Stejna mez, jakou pouziva HistoryOracle - at sloupec zrale znamena
    # doopravdy "tohle uz umime ocenit", ne "videli jsme to dvakrat".
    zdroje = store.stats_sources(HistoryOracle(store).min_span_days)
    if not zdroje:
        print("Katalog je prázdný — bot ještě neproběhl.")
        store.close()
        return 0

    print("--- KATALOG: zralost cenové historie ---")
    print(f"  {'zdroj':<16} {'položek':>8} {'zralé':>7} {'pozor.':>7}  {'první záznam':<12} naposledy")
    for r in zdroje:
        podil = 100.0 * (r["zrale"] or 0) / max(1, r["polozek"])
        print(f"  {r['source']:<16} {r['polozek']:>8} {r['zrale'] or 0:>6} "
              f"{podil:>3.0f}% {r['pozorovani'] or 0:>7}  "
              f"{_den(r['nejstarsi']):<12} {_den(r['naposledy'])}")

    # Kde vlastne lezi dosazitelne minimum? Prah 0,70 je odhad, dokud se
    # nezmeri, jak hluboko se polozky doopravdy dostanou.
    oracle = HistoryOracle(store)
    rozptyl = store.stats_price_spread(oracle.min_span_days, oracle.window_days)
    if rozptyl:
        print()
        print("--- JAK HLUBOKO POD VLASTNÍ MEDIÁN SE POLOŽKY DOSTANOU ---")
        print("  nejlepší poměr za 30 dní; práh na souhrn je u cestování 0,70")
        print(f"  {'zdroj':<16} {'zralých':>8} {'medián':>8} {'nejlepší':>9}"
              f"  {'<=0,90':>7} {'<=0,80':>7} {'<=0,70':>7}")
        for zdroj, pomery in sorted(rozptyl.items(), key=lambda p: -len(p[1])):
            serazene = sorted(pomery)
            stred = serazene[len(serazene) // 2]
            pod = [sum(1 for r in serazene if r <= mez) for mez in (0.90, 0.80, 0.70)]
            print(f"  {zdroj:<16} {len(serazene):>8} {stred:>8.2f} "
                  f"{serazene[0]:>9.2f}  " + " ".join(f"{n:>7}" for n in pod))

    pohyb = store.stats_price_moves(7)
    print(f"\n  Za týden se cena změnila {pohyb['zmen']}x u {pohyb['polozek']} položek.")
    print("  (do price_log se zapisují jen ZMĚNY, ne každý běh)")

    print(f"\n--- FEEDY ---")
    print(f"  Zpracovaných příspěvků v tabulce `seen`: {store.stats_seen()}")

    print("\n--- ODESLANÁ UPOZORNĚNÍ ---")
    for dny, popis in ((1, "24 hodin"), (7, "7 dní"), (30, "30 dní")):
        radky = store.stats_alerts(dny)
        if not radky:
            print(f"  za {popis:<9} nic")
            continue
        souhrn = ", ".join(f"{r['source']} {r['level']} {r['pocet']}x" for r in radky[:6])
        print(f"  za {popis:<9} {sum(r['pocet'] for r in radky):>3}  ({souhrn})")

    print(f"\n--- FRONTA VEČERNÍHO SOUHRNU ---")
    print(f"  Čeká {store.digest_size()} položek.")

    nemocne = store.source_health()
    print("\n--- ZDRAVÍ ZDROJŮ ---")
    if nemocne:
        for jmeno, pocet in sorted(nemocne.items(), key=lambda p: -p[1]):
            print(f"  {jmeno:<16} selhal {pocet}x po sobě")
    else:
        print("  Všechny zdroje se naposledy ozvaly v pořádku.")

    volani = store.daily_counter("judge")
    strop = int(cfg.get("judge.max_calls_per_day", 60))
    print(f"\n--- AI SOUDCE ---")
    print(f"  Dnes {volani} volání ze stropu {strop}.")

    print("\nCo běží dál po prahem, ukáže `--dry-run` v sekci TĚSNĚ POD PRAHEM.")
    store.close()
    return 0


def _den(iso: str | None) -> str:
    return (iso or "")[:10] or "-"


class _FxKoruny:
    """Kurz pro ověřovací příkaz: ptáme se rovnou v korunách, převod je identita.

    Existuje proto, aby `--check-travelpayouts` nemusel otevírat databázi.
    """

    @staticmethod
    def to_czk(amount, currency):
        return amount


def _zkusit_endpointy(http, headers, origin: str, dest: str) -> None:
    """Žebřík přes endpointy, které bychom mohli chtít.

    Existuje ze stejného důvodu jako žebřík variant výš: mapování polí
    u Travelpayouts vzniklo z dokumentace a dvakrát se ukázalo, že se
    s realitou rozchází (`departure_at` není mez okna, `limit` je povinný,
    ať API nevrátí jen třicet tras). Než se na nějaký endpoint navěsí kód,
    chce to vidět, co doopravdy vrátí — tenhle výpis je ten měřicí krok.

    Cílem je **cenový kalendář trasy**: umět ke zprávě dopsat „a za tři týdny
    je to za 6 200 Kč". Dnes bot vidí jen dnešní cenu a nemá jak poznat, jestli
    je to shodou okolností drahý termín.
    """
    mesic = (dt.date.today() + dt.timedelta(days=45)).strftime("%Y-%m")
    prvni_v_mesici = mesic + "-01"

    kandidati = [
        ("v3/grouped_prices", "https://api.travelpayouts.com/aviasales/v3/grouped_prices",
         {"origin": origin, "destination": dest, "currency": "czk",
          "group_by": "departure_at", "market": "cz"}),
        ("v3/get_latest_prices", "https://api.travelpayouts.com/aviasales/v3/get_latest_prices",
         {"origin": origin, "destination": dest, "currency": "czk",
          "period_type": "month", "one_way": "true", "limit": 30, "market": "cz"}),
        ("v1/prices/calendar", "https://api.travelpayouts.com/v1/prices/calendar",
         {"origin": origin, "destination": dest, "depart_date": mesic,
          "currency": "czk", "calendar_type": "departure_date"}),
        ("v1/prices/month-matrix", "https://api.travelpayouts.com/v1/prices/month-matrix",
         {"origin": origin, "destination": dest, "month": prvni_v_mesici,
          "currency": "czk", "show_to_affiliates": "true"}),
        ("v2/prices/month-matrix", "https://api.travelpayouts.com/v2/prices/month-matrix",
         {"origin": origin, "destination": dest, "month": prvni_v_mesici,
          "currency": "czk", "show_to_affiliates": "true"}),
        ("v1/prices/cheap", "https://api.travelpayouts.com/v1/prices/cheap",
         {"origin": origin, "destination": dest, "depart_date": mesic,
          "currency": "czk"}),
        ("v1/city-directions", "https://api.travelpayouts.com/v1/city-directions",
         {"origin": origin, "currency": "czk"}),
    ]

    print(f"\n--- DALŠÍ ENDPOINTY (trasa {origin}-{dest}, měsíc {mesic}) ---")
    for jmeno, url, params in kandidati:
        try:
            # Session přímo: `Http.get` u 4xx zahodí tělo, a právě v něm API
            # píše, který parametr se mu nelíbí.
            resp = http.session.get(url, params=params, headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"  {jmeno:24} SÍŤ: {exc}")
            continue

        if not resp.ok:
            telo = (resp.text or "").strip().replace("\n", " ")[:120]
            print(f"  {jmeno:24} {resp.status_code}  {telo}")
            continue

        try:
            data = resp.json()
        except ValueError:
            print(f"  {jmeno:24} 200, ale odpověď není JSON")
            continue

        zaznamy = data.get("data") if isinstance(data, dict) else data
        if isinstance(zaznamy, dict):          # klíčem bývá datum nebo cíl
            klice = list(zaznamy)[:3]
            prvni = zaznamy[klice[0]] if klice else None
            print(f"  {jmeno:24} 200  záznamů: {len(zaznamy)}  klíče: {klice}")
        elif isinstance(zaznamy, list):
            prvni = zaznamy[0] if zaznamy else None
            print(f"  {jmeno:24} 200  záznamů: {len(zaznamy)}")
        else:
            print(f"  {jmeno:24} 200  neznámý tvar: {str(data)[:90]}")
            continue

        if isinstance(prvni, dict):
            print(f"  {'':24}      pole: {sorted(prvni)}")
            ceny = [k for k in prvni if "price" in k or "value" in k]
            if ceny:
                print(f"  {'':24}      cena: "
                      + ", ".join(f"{k}={prvni[k]}" for k in ceny))
        elif prvni is not None:
            print(f"  {'':24}      první záznam: {str(prvni)[:90]}")

    print("\nPošli mi tenhle výpis. Z tvaru odpovědi poznám, který endpoint")
    print("unese cenový kalendář trasy, a navěsím ho na zprávy o letenkách.")


def _watch_z_radku(row) -> Watch:
    return Watch(
        id=int(row["id"]), origin=row["origin"], destination=row["destination"],
        od=dt.date.fromisoformat(row["od"]), do=dt.date.fromisoformat(row["do"]),
        nights_min=int(row["nights_min"]), nights_max=int(row["nights_max"]),
        out_day=row["out_day"], out_after_h=row["out_after_h"],
        out_before_h=row["out_before_h"], back_day=row["back_day"],
        back_after_h=row["back_after_h"], back_before_h=row["back_before_h"],
        best_czk=row["best_czk"], best_key=row["best_key"],
    )


def _prikaz(cfg, store, text: str):
    """Jeden příkaz z Telegramu. Vrací text odpovědi, nebo None."""
    slovo = _bez_diakritiky(text.split()[0].lower().lstrip("/").split("@")[0])

    if slovo == "hlidat":
        try:
            zadani = parse_watch(text, _domaci_letiste(cfg))
        except ChybaZadani as exc:
            return str(exc)
        cislo = store.watch_add(zadani)
        watch = _watch_z_radku(store.watch_list()[-1])
        return (f"✅ Založeno hlídání č. <b>{cislo}</b>\n\n"
                f"{watch.label()}\n\nOzvu se, jakmile něco najdu.")

    if slovo == "hlidani":
        return format_watch_list([_watch_z_radku(r) for r in store.watch_list()])

    if slovo == "zrusit":
        casti = text.split()
        if len(casti) < 2 or not casti[1].isdigit():
            return "Které? Napiš třeba <code>/zrusit 1</code>."
        if store.watch_delete(int(casti[1])):
            return f"Hlídání č. {casti[1]} zrušeno."
        return f"Hlídání č. {casti[1]} neexistuje."

    if slovo in ("pomoc", "help", "start"):
        return NAPOVEDA

    return None


def _zpracuj_prikazy(cfg, store, telegram) -> int:
    """Přečte nové zprávy z Telegramu a odpoví na příkazy.

    Bot dosud uměl jen mluvit. Tohle je jediné místo, kde poslouchá — bez
    webhooku, jen doptáním při každém běhu, takže není potřeba otevřený port
    ani veřejná adresa.

    `offset` je podstatný: bez potvrzení vrací Telegram tutéž zprávu pořád
    dokola a `/hlidat` by se zakládalo při každém běhu znovu.
    """
    if telegram is None:
        return 0

    ulozeny = store.get_meta("telegram:offset")
    try:
        data = telegram.get_updates(int(ulozeny) + 1 if ulozeny else None)
    except Exception as exc:  # noqa: BLE001 — výpadek nesmí shodit hlídání
        log.warning("Nepodařilo se přečíst zprávy z Telegramu: %s", exc)
        return 0

    posledni, zpracovano = None, 0
    for update in (data or {}).get("result") or []:
        posledni = update.get("update_id")
        zprava = update.get("message") or update.get("channel_post") or {}
        text = (zprava.get("text") or "").strip()
        # Příkazy se berou JEN z vlastního chatu. Bota si může najít kdokoliv
        # a zakládat hlídání cizím lidem není žádoucí.
        chat = str((zprava.get("chat") or {}).get("id") or "")
        if chat != str(cfg.telegram_chat) or not text.startswith("/"):
            continue

        odpoved = _prikaz(cfg, store, text)
        if odpoved:
            telegram.send(odpoved)
            zpracovano += 1

    if posledni is not None:
        store.set_meta("telegram:offset", str(posledni))
    return zpracovano


def _cerstve_zkontrolovano(row, interval_min: int) -> bool:
    if interval_min <= 0 or not row["checked"]:
        return False
    try:
        kdy = dt.datetime.fromisoformat(row["checked"])
    except ValueError:
        return False
    if kdy.tzinfo is None:
        kdy = kdy.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - kdy < dt.timedelta(minutes=interval_min)


def _domaci_letiste(cfg) -> str:
    letiste = cfg.get("sources.ryanair.airports", []) or ["PRG"]
    return str(letiste[0])


def run_watch(cfg) -> int:
    """Hlídání konkrétních záměrů.

    Opačný směr než zbytek bota: ten sbírá, co zdroje nabídnou, a hlásí, co je
    podezřele levné. Tady člověk řekne, co chce, a bot na to hlídá nejlepší
    možnost — a přehlásí ji, jakmile se objeví lepší.
    """
    http = build_http(cfg)
    store = Store(cfg.db_path)
    telegram = (Telegram(http, cfg.telegram_token, cfg.telegram_chat)
                if cfg.telegram_token and cfg.telegram_chat else None)

    prikazu = _zpracuj_prikazy(cfg, store, telegram)
    if prikazu:
        log.info("Zpracováno %s příkazů z Telegramu", prikazu)

    radky = store.watch_list()
    if not radky:
        log.info("Není co hlídat (založ přes /hlidat v Telegramu)")
        store.close()
        return 0

    engine = WatchEngine(
        http,
        delay_s=float(cfg.get("watch.delay_s", 0.4)),
        max_queries=int(cfg.get("watch.max_queries_per_watch", 12)),
    )

    interval = int(cfg.get("watch.min_interval_min", 60))
    for row in radky:
        # Timer běží po deseti minutách kvůli příkazům; ceny se přepočítávají
        # řidčeji. Bez tohohle by jedno hlídání znamenalo stovky dotazů denně
        # na trasu, jejíž cena se za den sotva hne.
        if _cerstve_zkontrolovano(row, interval):
            continue
        watch = _watch_z_radku(row)
        vysledek = engine.best_trip(watch)
        trip = vysledek.vyhovujici

        if trip is not None:
            # Přehlašuje se JEN zlepšení. Bez toho by hlídání psalo tutéž
            # letenku každou půlhodinu.
            zlepseni = watch.best_czk is None or trip.price_czk < watch.best_czk - 1
            if not zlepseni:
                store.watch_touch(watch.id)
                log.info("Hlídání %s: nic lepšího než %s Kč",
                         watch.destination, int(watch.best_czk or 0))
                continue
            zprava = format_watch(watch.label(), trip, watch.best_czk)
            if telegram is None or telegram.send(zprava):
                store.watch_save_best(watch.id, trip.price_czk, trip.key())
            log.info("Hlídání %s: %s Kč, %s", watch.destination,
                     int(trip.price_czk), trip.label())
            continue

        nahradni = vysledek.nahradni
        # Náhradní nabídka se posílá, jen dokud hlídání nikdy nic nesplnilo,
        # a každá jen jednou — jinak by přeostřené zadání znamenalo otravování.
        klic = f"nahradni:{nahradni.key()}" if nahradni else None
        if nahradni is None or watch.best_czk is not None or watch.best_key == klic:
            store.watch_touch(watch.id)
            continue

        zprava = format_watch(watch.label(), nahradni, None, vyhovuje=False)
        if telegram is None or telegram.send(zprava):
            store.watch_save_best(watch.id, None, klic)
        log.info("Hlídání %s: nic v zadaných časech, poslána náhrada",
                 watch.destination)

    store.close()
    return 0


def run_check_travelpayouts(cfg) -> int:
    """Ověří token a ukáže, jak odpověď doopravdy vypadá.

    Existuje proto, že odpověď se bez tokenu ověřit nedala — mapování polí
    v `sources/travelpayouts.py` vzniklo z dokumentace, ne z měření. Tenhle
    příkaz je ten měřicí krok: vypíše syrová jména polí u první nabídky
    a vedle to, co z nich zdroj složil. Když se něco přejmenovalo, uvidíš to.
    """
    if not cfg.travelpayouts_token:
        print("Chybí TRAVELPAYOUTS_TOKEN v prostředí.")
        return 1

    from .sources.travelpayouts import API, TravelpayoutsSource

    http = build_http(cfg)
    # Databázi schválně neotvíráme. Ověřovací příkaz se pouští ručně, klidně
    # pod jiným uživatelem, a založené soubory `-wal`/`-shm` s cizím vlastníkem
    # by pak shodily ostrý běh pod účtem `slevy`. Kurz tu není potřeba: ptáme
    # se rovnou v korunách, takže převod je identita.
    source = TravelpayoutsSource(http, _FxKoruny(), cfg)
    origin = (source.airports or ["PRG"])[0]
    # Token se nikdy nevypisuje, jen jeho délka — stejně jako u --check-itad.
    print(f"Token načten ({len(cfg.travelpayouts_token)} znaků), "
          f"ptám se na lety z {origin}.\n")

    headers = {"X-Access-Token": cfg.travelpayouts_token, "Accept": "application/json"}
    mesic = (dt.date.today() + dt.timedelta(days=source.days_from)).strftime("%Y-%m")

    # Žebřík variant. API na neplatný parametr odpoví 400 a v těle napíše který,
    # jenže `Http.get` tělo u 4xx zahodí — proto se tu volá session přímo.
    # Jde o to najít nejbohatší tvar dotazu, který ještě projde: mapování
    # vzniklo z dokumentace a tohle je ten chybějící měřicí krok.
    zaklad = {"origin": origin, "currency": "czk"}
    varianty = [
        ("jen origin + měna", zaklad),
        ("+ jednosměrné", {**zaklad, "one_way": "true"}),
        ("+ řazení podle ceny", {**zaklad, "one_way": "true", "sorting": "price"}),
        ("+ stránkování", {**zaklad, "one_way": "true", "sorting": "price",
                           "limit": 100, "page": 1}),
        ("+ měsíc odletu", {**zaklad, "one_way": "true", "sorting": "price",
                            "limit": 100, "page": 1, "departure_at": mesic}),
        ("+ trh CZ", {**zaklad, "one_way": "true", "sorting": "price",
                      "limit": 100, "page": 1, "departure_at": mesic, "market": "cz"}),
        ("současný _params()", source._params(origin)),
    ]

    data, funkcni = None, None
    for popis, params in varianty:
        try:
            resp = http.session.get(API, params=params, headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"  {popis:24} SÍŤ: {exc}")
            continue
        telo = (resp.text or "").strip().replace("\n", " ")[:130]
        if resp.ok:
            radky = (resp.json() or {}).get("data") or []
            print(f"  {popis:24} {resp.status_code}  nabídek: {len(radky)}")
            # Bere se varianta s NEJVÍC nabídkami, ne první, která projde.
            # Zrovna u tohohle API na tom hodně záleží: `limit` zvedne
            # odpověď z 30 na 100, kdežto `departure_at` ji srazí na 31.
            if radky and (data is None or len(radky) > len(data.get("data") or [])):
                data, funkcni = resp.json(), (popis, params)
        else:
            print(f"  {popis:24} {resp.status_code}  {telo}")

    if data is None:
        print("\nŽádná varianta nevrátila nabídky. Pošli mi tenhle výpis, "
              "z chybových hlášek poznám, který parametr API nechce.")
        return 1

    rows = data.get("data") or []
    print(f"\nNejbohatší funkční varianta: {funkcni[0]}")
    print(f"  parametry: {sorted(funkcni[1])}")
    print(f"Odpověď má klíče: {sorted(data)[:8]}")
    print(f"Nabídek: {len(rows)}\n")
    print(f"Pole první nabídky:\n  {sorted(rows[0])}\n")

    offers = [o for o in (source._to_offer(origin, r) for r in rows[:8]) if o]
    print(f"Z prvních osmi se podařilo složit {len(offers)} nabídek:")
    for offer in offers:
        term = format_term(offer.extra) or "termín nerozpoznán"
        print(f"  {offer.price_czk:>8.0f} Kč  {offer.uid:<9} {term}")
        print(f"           {offer.url[:78]}")
    if not offers:
        print("  ŽÁDNOU — pole se přejmenovala, oprav mapování")
        print(f"  syrová první nabídka: {rows[0]}")

    # Druhý žebřík: co dalšího ten token otevírá. Cílem je cenový kalendář
    # trasy, tedy odpověď na "a kdy je to levněji".
    cil = (rows[0].get("destination") if rows else None) or "BCN"
    _zkusit_endpointy(http, headers, origin, cil)
    return 0 if offers else 1


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
    parser.add_argument("--only", metavar="RODINA",
                        help="projde jen jednu rodinu zdrojů: kinguin, pepper, travel")
    parser.add_argument("--dump", metavar="SOUBOR", help="uloží syrové nabídky do JSON")
    parser.add_argument("--no-ai", action="store_true", help="vypne AI soudce")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--print-chat-id", action="store_true")
    parser.add_argument("--backup", action="store_true",
                        help="uloží konzistentní kopii databáze do data/zalohy")
    parser.add_argument("--stats", action="store_true",
                        help="co bot nasbíral: zralost historie, upozornění, fronta")
    parser.add_argument("--check-itad", action="store_true",
                        help="ověří klíč k IsThereAnyDeal a měnu odpovědí")
    parser.add_argument("--watch", action="store_true",
                        help="zkontroluje hlidane trasy a prikazy z Telegramu")
    parser.add_argument("--check-travelpayouts", action="store_true",
                        help="ověří token k Travelpayouts, tvar odpovědi a další endpointy")
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
    if args.backup:
        return run_backup(cfg)
    if args.stats:
        return run_stats(cfg)
    if args.check_itad:
        return run_check_itad(cfg)
    if args.watch:
        return run_watch(cfg)
    if args.check_travelpayouts:
        return run_check_travelpayouts(cfg)
    if args.test_telegram:
        return run_test_telegram(cfg)
    if args.digest:
        return run_digest(cfg)
    return run_scan(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
