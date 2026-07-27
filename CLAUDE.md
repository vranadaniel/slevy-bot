# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Jazyk

Kód, komentáře, commit messages i výstup pro uživatele jsou **česky**. Commit
messages se píšou bez diakritiky.

## Příkazy

```bash
.venv/Scripts/python -m pytest                      # celá sada (Windows)
.venv/bin/python -m pytest                          # Linux
python -m pytest tests/test_score.py                # jeden soubor
python -m pytest tests/test_score.py::TestGeminiMustAlert -v
python -m pytest -k "addon"                         # podle názvu
```

Na Windows nastav `PYTHONIOENCODING=utf-8`, jinak české výstupy spadnou na
`UnicodeEncodeError` (konzole jede v cp1250).

```bash
python -m src.main --dry-run                 # projde zdroje, nic neodešle ani nezapíše
python -m src.main --only travel             # jen letenky a hotely, běh na vteřiny
python -m src.main --dry-run --explain UID   # rozpad signálů u položky (název i uid)
python -m src.main --dump offers.json        # syrová data ze zdrojů
python -m src.main --check-itad              # ověří ITAD klíč a měnu odpovědí
python -m src.main --test-telegram
python -m src.main --bootstrap               # označí feedy za viděné, nic nepošle
```

`--explain` je hlavní ladicí nástroj — ukáže cenu, credibility, odkud přišla
hodnota a proč verdikt dopadl, jak dopadl.

Tokeny se berou z prostředí: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`OPENROUTER_API_KEY`, `ITAD_API_KEY`. Chybějící volitelný klíč nesmí nic shodit.

## Architektura

Trychtýř: sběr → cenová historie → heuristika → AI na hrstce zbylých → Telegram.
Vše se sbíhá do jediného čísla **`value_ratio` = zaplatíš ÷ reálná hodnota**.

### Dva švy

`Source` (`src/sources/`) dodává nabídky, `ValueOracle` (`src/oracles/`) odhaduje,
co ta věc doopravdy stojí. Nic pod vrstvou `Offer` nesmí vědět, ze kterého webu
data pocházejí — žádné `if source == "kinguin"` v `score.py`, `store.py` ani
`notify.py`.

### Zdroje nejsou zaměnitelné

`Source.kind` rozlišuje `catalog` a `feed` a `score.py` podle toho volí větev:

- **catalog** (Kinguin) — tutéž položku vidíme opakovaně, stavíme si vlastní
  cenovou historii, deduplikuje se podle uid a poklesu ceny
- **feed** (Pepper, fly4free) — proud jedinečných příspěvků, historie nedává
  smysl, deduplikuje se podle `guid` v tabulce `seen`

Sloučit je do jednoho rozhraní by bylo chybné.

### Pořadí oracles je významné

V `main.py`: `history → references → flights → itad → declared`, AI běží zvlášť
dávkově až na tom, co zbylo. První oracle s odpovědí vyhrává, takže levné a důvěryhodné
zdroje předbíhají placené. `HistoryOracle` schválně vrací `None`, když cena
neklesla pod vlastní medián — tím pustí ke slovu ostatní.

### Credibility hlídá ocenění, ne položku

Každý zdroj odvozuje `Offer.credibility` z toho, co prodejce nezfalšuje:
prodejnost na Kinguinu, komunitní teplota na Pepperu, redakční výběr u fly4free.
Nízká hodnota položku nezahodí, jen jí zavře cestu k okamžitému upozornění.

Výjimka v `_finalize`: když hodnota přišla z `references` nebo `history`
s vysokou jistotou, práh credibility se přeskočí — víme, co ta věc stojí,
a nezáleží na tom, kolikátá je v žebříčku.

## Věci, které vypadají jako chyba, ale nejsou

Následující rozhodnutí vzešla z měření na živých datech. Bez znalosti kontextu
vypadají jako nedodělek a svádějí k „opravě", která zhorší chování.

**ITAD nikdy nespouští okamžité upozornění.** `_finalize` má u
`value.origin == "itad"` natvrdo `qualifies_instant = False`. Kinguin prodává
regionální klíče pod cenami, na které oficiální obchody nikdy nejdou — levnější
než historické minimum je zhruba třetina her. Jako spouštěč to dávalo 153 zpráv
v jednom běhu, poměr k doporučené ceně 43. Trhák u her pozná až vlastní cenová
historie. ITAD slouží k zobrazení hodnoty a k utišení nabídek, které ani
oficiální minimum nepodlezou.

**`price.market` z Kinguinu se nikdy nepoužije jako hodnota.** MSRP si nastavuje
prodejce sám; šunta „Tanks Battle" se tváří na 97,50 € a 99 % slevu.
`DeclaredOracle` proto platí jen pro feedy, kde původní cenu píše komunita.

**`store.price_profile` počítá časově vážený medián.** Do `price_log` se zapisují
jen změny ceny, takže prostý medián by lhal: položka za 149 Kč tři týdny a pak
za 60 Kč má v logu dva řádky a medián 104 Kč, přestože běžná cena je 149 Kč.

**Popularita her se zjišťuje až u položek mířících do souhrnu.** Endpoint
`/games/info/v2` bere jednu hru na dotaz, takže na celém katalogu by to bylo
5 000 požadavků. `ItadOracle.enrich_popularity` proto běží v `main.py` až na
seznamu `digest`, ne v `prepare()`. Vzorec kombinuje počet hodnocení
(logaritmicky) a skóre; hry bez hodnocení na Steamu — Battlefield, Call of Duty
— padají na `Offer.credibility`, tedy prodejnost na Kinguinu. `stats.rank`
z ITAD se schválně nepoužívá: neměřili jsme jeho rozložení.

**Popularita se nedostala do `score.py`.** Je to věc řazení a filtrování
souhrnu, ne ocenění, a `score.py` nemá vědět, že něco jako hra existuje.
Filtruje `drop_unpopular` v `main.py`, řadí `_rank_key` v `notify.py`. Filtr
se týká jen položek se **známou** popularitou — mlčet o něčem jen proto, že
o tom nemáme data, by bylo horší než to poslat.

**`INGAME_TOPUP` na Kinguinu není hra.** Navzdory názvu je to škatulka na
předplatné: YouTube Premium, Spotify, ChatGPT i to referenční Gemini za 65 Kč.
Proto v `notify.group_of` patří do sekce s předplatným, ne mezi hry.

**Cestování má vlastní prahy.** Letenka za 5 % běžné ceny neexistuje — i
legendární error fare bývá „jen" o 40–60 % pod cenou. Proto
`thresholds.by_category` s klíčem `flight`/`hotel` na 0,45 a 0,70. Bez toho
nedoručí přidávání cestovatelských zdrojů ani jednu zprávu navíc — změřeno.

**U letenek nefunguje jednotný poměr, proto `instant_below_czk`.** Skvělá cena
do Evropy leží na 36 % běžné, do jihovýchodní Asie na 62 %. Práh nastavený na
Evropu by dálkové lety umlčel, práh na dálkové lety by z Evropy posílal běžné
ceny. `flights.yaml` proto u každého regionu nese `great_czk` a `FlightOracle`
ho předá jako `instant_below_czk`; `score.py` mu dá přednost před poměrem.
Pole je schválně obecné — `score.py` nemá vědět, že jde o letenky.

**`FlightOracle` neoceňuje katalogové nabídky (Ryanair).** Čísla v ceníku
vznikla z cen, které slevové weby **vypsaly jako akci** — to je jiná populace
než ceník dopravce, kde je běžná cena z podstaty níž. Posuzovat jedno druhým je
tentýž kruh, kvůli kterému bot hlásil Krakov za 748 Kč jako trhák. U Ryanairu
smí rozhodnout jen vlastní cenová historie; než se nasbírá, správná odpověď je
mlčet. Bez tohohle pravidla zaplnily souhrn běžné ceny (Kodaň 892 Kč, Bristol
947 Kč) hned první běh.

**`FlightOracle` oceňuje jen `category == "flight"`.** Zájezd má v ceně
i ubytování a stravu, takže cena letenky o něm nevypovídá. Ten ať ocení AI
soudce, nebo zůstane neoceněný.

**Při shodě ve dvou regionech vyhrává ten dražší.** Zdroje píšou i výchozí
město („from Milan to PHUKET"), takže se titulek běžně trefí dvakrát. Cíl je
u těchhle webů skoro vždycky ta exotičtější půlka. Regiony jsou proto
v `FlightOracle.__init__` seřazené podle `typical_czk` sestupně.

**V `flights.yaml` nejsou holé názvy zemí jako `france` nebo `india`.**
Obsahují je názvy aerolinek: „Air France flights … to Tunisia" by skončilo ve
Francii, „Filipíny s Air India" v Indii. Místo nich jsou tam města nebo tvary
s předložkou (`to china`, `india from`).

**Hodnotu letenky umí dodat i AI soudce**, když ji ceník nezná — do výběru
pouští feedy `min_credibility_ai`.

**Feedová položka, kterou se nepodařilo ocenit, se nezapisuje do `seen`.**
Viz `_retry_later` v `main.py`. Zápis by ji umlčel natrvalo, přestože hodnota
mohla přijít příští běh (vyčerpaný denní strop AI, výpadek API). Nekonečné to
není: položka za pár dní vypadne z RSS.

**Rychlý sken cestování běží na vlastním timeru.** `--only travel` každých deset
minut, hlavní sken v :13 a :43. Minuty jsou schválně různé — oba běhy sahají na
tutéž SQLite databázi. `busy_timeout` ve `store.py` je druhá pojistka.

**Počet kusů skladem se ve scoringu schválně nepoužívá.** Vypadá to jako
signál naléhavosti, ale není. Změřeno: čtyři položky s posledními kusy byly
po hodině beze změny — stejný počet kusů i cena. Digitální klíč nemá frontu
u pokladny; „1 ks" je atribut nabídky jednoho prodejce, ne odpočet. Ze stejného
důvodu **nemá smysl zrychlovat katalogový sken** pod stávajících 30 minut.
Rychlost rozhoduje u error fares, ne u klíčů — proto má vlastní timer cestování.
Sklad se jen vypisuje ve zprávě jako doplňující údaj.

**`_queue` v `main.py` volá `mark_alerted`.** Není to překlep — bez toho by se
katalogové položky vracely do souhrnu každý večer znovu.

**Regex na regiony v `titles.py` je bez `IGNORECASE`.** Kinguin píše regiony
verzálkami; s ignorováním velikosti ukusoval regex „Us" z „The Last of Us".

**V `titles.candidates` se dělí na `" + "` dřív než se testuje `is_addon`.**
Spojka rozhoduje o tom, co je hlavní produkt: „DOOM + Pre-Order Bonus DLC" je
hra s přibaleným bonusem, kdežto „EA Sports FC 24 - Pre-order Bonus DLC" je sám
bonus. U přídavků se hledá jen jejich plný název — když ho ITAD nezná, správná
odpověď je „neumím ocenit", ne cena celé hry.

**GitHub Actions mají `schedule` schválně vypnutý.** Cron tam za čtyři hodiny
nespustil ani jeden běh, přestože workflow byly aktivní a cron platný (u nových
účtů GitHub plánované běhy omezuje). Ostrý provoz běží na serveru přes systemd
timery; **zdrojem pravdy je databáze na serveru**, ne větev `data`. Ruční běh na
GitHubu pracuje s vlastní kopií stavu a může poslat, co server už odeslal.

## Ověřeno a nefunguje

Ať se to nezkouší podruhé. Vše měřeno živě, ne z dokumentace.

**Dopravci:** easyJet a Eurowings vracejí 403 (Akamai, resp. Cloudflare),
Smartwings, Norwegian a Vueling nemají veřejné endpointy na ceny. Dálkoví
dopravci veřejné API na ceny nemají vůbec — cena jde přes globální rezervační
systémy. Zbývá Amadeus (free tier, klíč) a Travelpayouts (token zdarma).

**České e-shopy:** Alza 403, CZC captcha (DataDome), Mall 404, Heureka 403,
Slevomat 403, Hlídač shopů 403, Skrz nemá feed, Sleviště vrací HTML.

**Cestovatelské weby:** `secretflying.com/feed/` vrací HTML, `fly4free.pl`
error-fare feed je prázdný, veřejné náhledy `t.me/s/…` u těchhle webů neexistují.
`theflightdeal.com` a `airfarespot.com` fungují, ale mají US trasy.

**Herní obchody:** Eneba 400 + IP allowlist, G2A 403, Gamivo 404, Humble 403,
Reddit vyžaduje OAuth. Fanatical a Steam odpovídají a stojí za zvážení.

## Externí rozhraní

**Kinguin** — neoficiální interní JSON API, bez klíče. Ceny v **eurocentech**.
Serverové filtry `priceFrom`, `marketingProductType` a `currency` **ignoruje**,
filtruje se lokálně. Katalog se prochází seřazený podle `bestseller.total`.

**Pepper** (mydealz, hotukdeals, dealabs, pepper.pl) — RSS, jeden parser na
všechny. Teplota je v titulku (`^\d+°`), cena a obchod v atributech
`<pepper:merchant>`. Čtyři národní zápisy čísel řeší `src/money.py`; parser
vyžaduje symbol měny, jinak by z „modern 4 hotel … from €34" vypadla cena 4.

**Ryanair** (`sources/ryanair.py`) — veřejné API bez klíče, ceny rovnou
v korunách a už zpáteční. Jediný **katalogový** zdroj u cestování, takže si
u něj bot staví vlastní cenovou historii — to je to, co má natrvalo nahradit
odhady v `flights.yaml`. `uid` je **trasa** (`PRG-BGY`), ne termín; jinak by
se historie nikdy nenasbírala. Parametr `limit` API odmítá s `InvalidLimit`
bez ohledu na hodnotu, bez něj vrátí kolem 32 tras na letiště. Pardubice
neobsluhuje, Brno a Ostrava mají po třech trasách.

**Wizz Air** (`sources/wizzair.py`) — druhý katalogový zdroj, doplňuje Ryanair:
z Vídně nelétá, zato z Bratislavy má 38 tras (Praha 20). Tři věci, bez kterých
to nefunguje a na které se přijde jen měřením:

* **Cookies se musí před každým dotazem zahodit.** Server přiloží
  `RequestVerificationToken` a u dalšího dotazu ho chce zpátky v hlavičce,
  jinak vrátí `InvalidProtocol`. Bez toho projde z celé dávky **jen první
  trasa** a zbytek tiše propadne — chyba, která se tváří jako prázdný zdroj.
* `dayInterval` musí být **aspoň 3**, jinak validace odmítne dotaz.
* Verze API je v cestě (`be.wizzair.com/29.8.0/…`) a zvedá se; zjišťuje se
  z jejich webu, ne z konfigurace.

Trasy se mezi běhy **střídají** (`routes_per_run`). Zeptat se na všech 58
každých deset minut by bylo přes osm tisíc požadavků denně.

**Cestování** (`sources/travel.py`) — RSS, jeden parser na všechny weby.
`travelfree.info` je nejsilnější zdroj pro střední Evropu (25 položek, nové
příspěvky každou půlhodinu) a podporuje tagové feedy pro jednotlivá města;
`airport` u feedu znamená „sem se dívej celé". `fly4free.com` má error-fare
feedy z velké části jako archiv z let 2020–2021, proto `max_age_days`.
Ověřeno a nefunguje: `secretflying.com/feed/` vrací HTML, `fly4free.pl`
error-fare feed je prázdný, veřejné náhledy `t.me/s/…` u těchhle webů neexistují.

**ITAD** — dva dávkové endpointy, cache v SQLite: překlad názvů natrvalo
(i nenalezené, ať se marné dotazy neopakují), minima s TTL.

## Ladění chování

Souhrn chodí ve čtyřech pevných sekcích (`notify.GROUP_ORDER`) s kvótou
`digest.per_group` na každou. Kvóta je celý smysl rozdělení — bez ní zaberou
hry celou zprávu a trhák na předplatném propadne pod strop. Delší zpráva se
dělí přes `notify.split_message`, protože odkazy se do limitu 4096 znaků
počítají i s celým `href`.

Prahy jsou v `config.yaml`, kód se kvůli nim upravovat nemá. `references.yaml`
je ruční ceník — každé pravidlo je položka oceněná zadarmo a přesně.
`flights.yaml` je totéž pro letenky, jen po regionech světa místo po názvech.
`merchants.yaml` řeší doručení do ČR trojstavově: neznámý obchod projde, ale
nikdy jako okamžité upozornění.

Testy v `tests/test_score.py` a `tests/test_itad.py` používají **ostrou
konfiguraci z repozitáře**, takže hlídají i to, jestli prahy a ceník pořád dávají
smysl. Dva akceptační testy drží obě strany problému: Gemini AI Pro na 18 měsíců
za 65 Kč **musí** projít jako okamžité upozornění, „Tanks Battle" s vymyšlenou
MSRP **nesmí** projít vůbec.
