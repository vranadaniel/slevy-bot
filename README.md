# Bot na lov extrémních slev → Telegram

Hlídá tři zdroje a posílá na Telegram jen věci, které stojí zlomek své skutečné ceny.
Vznikl kvůli jedné konkrétní nabídce: **Google Gemini AI Pro na 18 měsíců za 65 Kč**,
tedy necelé procento běžné ceny.

Dvě úrovně upozornění: **extrém pingne hned**, zbytek přijde večer jako jeden souhrn.

---

## Co bot sleduje

| Zdroj | Druh | Co odtud chodí | Klíč |
|---|---|---|---|
| **Kinguin** | katalog | předplatné, software, herní klíče | není potřeba |
| **Pepper** (mydealz.de, hotukdeals.com, dealabs.com, pepper.pl) | feed | elektronika, móda, cestování, cokoliv | není potřeba |
| **fly4free.com** | feed | letenky a hotely včetně error fare | není potřeba |

Referenční ceny her doplňuje **IsThereAnyDeal** — free klíč z
[isthereanydeal.com/apps/my/](https://isthereanydeal.com/apps/my/), bez něj bot běží dál,
jen hry zůstanou neoceněné.

Fyzické zboží ze zahraničních e-shopů projde jen tehdy, když obchod doručuje do ČR
(seznam v `merchants.yaml`). Letenky se filtrují na PRG, BRQ, PED, OSR, VIE a BTS.

---

## Jak se rozhoduje

Vše se sbíhá do jednoho čísla — **`value_ratio` = zaplatíš ÷ reálná hodnota**.
Gemini za 65 Kč má poměr 0,007, tedy 0,7 % skutečné ceny.

Reálnou hodnotu hledá pětice „oracles" v pořadí od nejdůvěryhodnějšího:

1. **vlastní cenová historie** — nejsilnější, nikdo ji neovlivní, ale potřebuje pár dní
2. **`references.yaml`** — ruční ceník, překlenuje studený start u předplatného
3. **IsThereAnyDeal** — referenční ceny her napříč 50+ oficiálními obchody
4. **cena z příspěvku** — u Pepperu, kde ji píše komunita
5. **AI soudce** — poslední instance pro položky, které nikdo jiný neocení

### Hry: proč ITAD nespouští okamžitá upozornění

U her je doporučená cena mizerné měřítko — slevují se neustále. Nabízelo by se
tedy porovnávat s historickým minimem napříč oficiálními obchody. **Měření na
živých datech ale ukázalo, že ani to nefunguje:**

| měřítko | „extrémních" nabídek v jednom běhu |
|---|---|
| poměr k doporučené ceně | 43 |
| pod historickým minimem | 153 |
| vlastní cenová historie | 9 |

Důvod je v povaze šedého trhu. Kinguin prodává regionální a jinak získané klíče
pod cenami, na které oficiální obchody nikdy nejdou — **levnější než historické
minimum je zhruba třetina her**. Není to tedy výjimka, ale pravidlo, a jako
spouštěč to nemá cenu.

Okamžité upozornění u her proto spouští až **vlastní cenová historie**, jediná,
která šedý trh odráží. ITAD slouží ke dvěma věcem: dodá hodnotu pro zobrazení
a utiší nabídky, které ani oficiální minimum nepodlezou.

Párování názvů řeší [`src/titles.py`](src/titles.py) — ITAD páruje přesnou shodou,
takže „Gothic 1 Remake PC Steam CD Key" se sám netrefí. Změřeno na živých datech:
**98 % her z katalogu se spáruje.**

### Proč nestačí procento slevy

Marketplace jsou plné šuntu s vymyšlenou původní cenou. Na Kinguinu se
„Tanks Battle Steam CD Key" tváří na 97,50 € a 99 % slevu — kdyby bot věřil
deklarované slevě, posílal by samý brak.

Proto každá položka nese **`credibility`**, signál, který prodejce neovlivní:

- Kinguin → pozice v žebříčku prodejnosti
- Pepper → komunitní teplota dealu (to číslo se stupněm v titulku)
- fly4free → redakční výběr

Nízká důvěryhodnost položku nezahodí, jen jí zavře cestu k okamžitému upozornění.
Když ale hodnota přišla z ručního ceníku nebo z vlastní historie, prodejnost
nerozhoduje — víme, co ta věc stojí.

---

## Zprovoznění

### 1. Bot na Telegramu

Napiš [@BotFather](https://t.me/BotFather), pošli `/newbot` a ulož si token.
Pak svému botovi pošli libovolnou zprávu a zjisti `chat_id`:

```bash
python -m src.main --print-chat-id
```

### 2. Lokální běh

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
```

Tokeny do prostředí (PowerShell):

```powershell
$env:TELEGRAM_BOT_TOKEN = "…"; $env:TELEGRAM_CHAT_ID = "…"; $env:OPENROUTER_API_KEY = "…"
```

Zkušební průchod, který nic neodesílá ani nezapisuje:

```bash
python -m src.main --dry-run
```

### 3. Nasazení na server

Podrobný návod krok za krokem včetně toho, kam na DigitalOcean kliknout, je
v [deploy/NAVOD.md](deploy/NAVOD.md). Ve zkratce:

Na Debianu nebo Ubuntu stačí jeden příkaz:

```bash
curl -fsSL https://raw.githubusercontent.com/vranadaniel/slevy-bot/main/deploy/install.sh | sudo bash
```

Skript nainstaluje závislosti, založí systémového uživatele `slevy`, naklonuje
repozitář do `/opt/slevy-bot`, vytvoří virtuální prostředí a zapne systemd timery.
Pokud v repozitáři existuje větev `data`, **převezme z ní databázi** — jinak by
se vynulovala deduplikace a přišla by záplava opakovaných upozornění.

Pak vyplň tokeny a ověř spojení:

```bash
sudo nano /etc/slevy-bot/env
```

```bash
sudo systemctl start slevy-scan && journalctl -u slevy-scan -n 30
```

**Při úplně nové instalaci** (bez převzaté databáze) pusť nejdřív bootstrap, ať
tě bot nezasype tím, co ve světě visí už týden:

```bash
cd /opt/slevy-bot && sudo -u slevy .venv/bin/python -m src.main --bootstrap
```

Aktualizace na novou verzi je tentýž skript — je idempotentní a tokeny nepřepisuje:

```bash
sudo bash /opt/slevy-bot/deploy/install.sh
```

### Provoz

```bash
systemctl list-timers 'slevy-*'      # kdy poběží příště
journalctl -u slevy-scan -n 50       # log posledního skenu
journalctl -u slevy-scan -f          # sledovat živě
systemctl start slevy-digest         # poslat souhrn hned
```

---

## Plán běhů

| Jednotka | Kdy | Co dělá |
|---|---|---|
| `slevy-scan.timer` | v :13 a :43 každou hodinu | projde zdroje, pošle okamžitá upozornění |
| `slevy-digest.timer` | 19:09 místního času | odešle nasbírané položky |

```bash
systemctl list-timers 'slevy-*'
```

Na rozdíl od GitHub Actions systemd rovnou ukáže, **kdy poběží příště** — GitHub
takovou obrazovku vůbec nemá, tam je vidět jen historie.

`Persistent=true` navíc dohoní běh zameškaný výpadkem nebo restartem, což cron neumí.

### Proč to neběží na GitHub Actions

Workflow v `.github/workflows/` zůstávají, ale **jen pro ruční spuštění**.
GitHub u tohohle repozitáře nespustil z cronu ani jeden běh, přestože obě
workflow byly aktivní, Actions povolené a cron platný — vyzkoušeno na `*/30`
i na posunuté minuty. U čerstvě založených účtů GitHub plánované běhy omezuje
jako obranu proti zneužití Actions na těžbu.

Pozor: ruční běh na GitHubu pracuje s vlastní kopií stavu z větve `data`, takže
může poslat upozornění, které server už odeslal. **Zdrojem pravdy je databáze
na serveru.**

## Příkazy

```bash
python -m src.main --dry-run                     # projde zdroje, nic neodešle
python -m src.main --dry-run --explain gemini    # rozepíše signály u položky
python -m src.main --bootstrap                   # první běh, jen označí viděné
python -m src.main                               # ostrý sken
python -m src.main --digest                      # odešle denní souhrn
python -m src.main --dump offers.json            # syrová data na diagnostiku
python -m src.main --test-telegram               # ověří token a chat_id
python -m src.main --no-ai                       # vypne AI soudce
```

`--explain` je hlavní nástroj na ladění. Ukáže cenu, důvěryhodnost, odkud přišla
reálná hodnota a proč to skončilo tak, jak to skončilo.

---

## Ladění

Prahy a ceník patří **do gitu**, ne na server — `install.sh` dělá
`git reset --hard`, takže by se úpravy provedené na serveru při další
aktualizaci ztratily. Postup je tedy vždycky: uprav doma, otestuj, pushni,
na serveru spusť `install.sh`.

Prahy jsou v `config.yaml`, kód se kvůli nim upravovat nemusí:

```yaml
thresholds:
  instant_ratio: 0.05    # <= 5 % reálné hodnoty → pingne hned
  digest_ratio: 0.20     # <= 20 % → do večerního souhrnu
```

Chodí toho moc? Sniž `instant_ratio` na 0,03. Chodí toho málo?
Zvedni `digest_ratio` nebo přidej pravidla do `references.yaml` — každé nové
pravidlo ušetří práci AI soudci a zpřesní ocenění.

### Souhrn a popularita her

Souhrn chodí ve čtyřech sekcích s kvótou na každou, aby trhák na předplatném
nezapadl mezi hrami:

```yaml
digest:
  per_group: 8     # kolik položek nejvýš v jedné sekci
```

Uvnitř sekce s hrami nerozhoduje sleva, ale **popularita**. Bez toho vyhraje
vždycky starý titul, o který nikdo nestojí — čím míň lidí hru chce, tím
hlouběji jde cena. Popularita se počítá z hodnocení hráčů (ITAD, endpoint
`/games/info/v2`) a hry pod prahem se do souhrnu vůbec nedostanou:

```yaml
itad:
  min_popularity: 0.6    # zhruba "aspoň 250 hodnocení"; 0 filtr vypne
```

Prázdná sekce s hrami → sniž na 0,5. Pořád brak → zvedni na 0,7. Skutečná
čísla u konkrétní hry ukáže `--explain`:

```
popularita     0.94  (92 % z 742000 hodnocení, vydáno 2025-05-30)
```

### Cestování

Letenky a hotely se chovají jinak než digitální klíče a mají proto vlastní
prahy — letenka za 5 % běžné ceny neexistuje:

```yaml
thresholds:
  by_category:
    flight: {instant_ratio: 0.45, digest_ratio: 0.70}
    hotel:  {instant_ratio: 0.45, digest_ratio: 0.70}
```

Zdroje se přidávají v `config.yaml` pod `sources.travel.sites`, kód se kvůli
tomu upravovat nemusí. `airport` u feedu znamená „tenhle feed je pro dané
letiště celý relevantní" — hodí se na tagové feedy typu
`travelfree.info/tag/prague/feed/`.

Error fare mizí během hodin, takže cestování běží na vlastním rychlém timeru:

```bash
python -m src.main --only travel     # šest feedů, běh na vteřiny
```

### `references.yaml`

Nejužitečnější soubor v projektu. Přidávej si sem, co tě zajímá:

```yaml
- match: ["gemini", "ai pro"]        # všechny výrazy musí být v názvu
  value_czk_per_month: 490
- match: ["windows 11 pro"]
  value_czk: 4500
```

U předplatného se měsíční hodnota násobí délkou vyčtenou z názvu
(„18 Months", „18-Month", „1 Year"). Právě tenhle přepočet dělá z Gemini trhák.

### Náklady na AI

Soudce běží jen na položkách, které levnější oracles neocenily, dávkově a s tvrdým
denním stropem (`judge.max_calls_per_day`). Po deduplikaci má většina běhů nula
kandidátů a API se nezavolá vůbec. Reálně jde o haléře měsíčně.
Bez klíče nebo při výpadku API bot běží dál na samotné heuristice.

---

## Testy

```bash
.venv/Scripts/python -m pytest
```

Dva testy drží obě strany problému a oba stojí na živých datech:

- Gemini AI Pro na 18 měsíců za 65 Kč **musí** projít jako okamžité upozornění
- „Tanks Battle Steam CD Key" s vymyšlenou MSRP **nesmí** projít vůbec

Testy scoringu schválně používají ostrou konfiguraci z repozitáře, takže hlídají
i to, jestli prahy a ceník pořád dávají smysl.

---

## Vědomá omezení

- **Studený start.** První týden nemá katalog cenovou historii a jede na ceníku
  a AI. Čekej víc falešných poplachů; po týdnu se to srovná samo.
- **Herní klíče se neocení.** Na hry nemáme referenční ceny. Nejbližší krok je
  přidat [IsThereAnyDeal](https://docs.isthereanydeal.com/) jako další oracle —
  má free klíč a dává reálná historická minima. Patří do `src/oracles/`, ne mezi
  zdroje: nedodává nabídky, ale referenční ceny.
- **České zdroje chybí.** Heureka, Slevomat, Mall i Hlídač shopů vracejí 403,
  Skrz feed nemá a český Pepper neexistuje. Schůdnější cesta jsou XML feedy
  jednotlivých e-shopů pro Heureku, ne agregátory.
- **Malá letiště.** Brno, Pardubice a Ostrava se ve zdrojích objeví zřídka.
  Reálné pokrytí je hlavně Vídeň a Praha.
- **Doručení do ČR je odhad**, ne jistota. U neznámého obchodu bot raději upozorní
  s poznámkou, než aby mlčel.
- **Neoficiální API a RSS.** Kinguin i Pepper můžou formát kdykoliv změnit. Proto
  retry, měkké selhání zdroje (jeden spadlý zdroj neshodí běh) a `--dump`.
- **Kupóny.** Kinguin API vrací ceníkovou cenu, ne cenu po slevovém kódu.

---

## Struktura

```
src/
├── main.py           orchestrace a CLI
├── score.py          jádro: value_ratio a dvě úrovně upozornění
├── store.py          SQLite, cenová historie, deduplikace
├── shipping.py       doručuje obchod do ČR?
├── money.py          parsování cen ze čtyř národních zápisů
├── fx.py             kurzy ČNB
├── notify.py         Telegram
├── sources/          odkud nabídky chodí
│   ├── kinguin.py    katalog
│   ├── pepper.py     čtyři weby jedním parserem
│   └── fly4free.py   letenky
└── oracles/          co ta věc doopravdy stojí
    ├── history.py    vlastní cenová historie
    ├── refs.py       ruční ceník
    ├── declared.py   cena z příspěvku
    └── judge.py      AI přes OpenRouter
```

Dva švy drží projekt otevřený: `Source` pro nové zdroje nabídek, `ValueOracle`
pro nové zdroje referenčních cen. Nic pod vrstvou `Offer` neví, ze kterého webu
data pocházejí.
