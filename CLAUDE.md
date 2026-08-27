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
python -m src.main --check-travelpayouts     # ověří token a tvar odpovědi Aviasales
python -m src.main --test-telegram
python -m src.main --bootstrap               # označí feedy za viděné, nic nepošle
python -m src.main --stats                   # co bot nasbíral, bez sahání na síť
python -m src.main --backup                  # konzistentní kopie databáze
python -m src.main --watch                   # hlidane trasy + prikazy z Telegramu
python -m src.main --check-references        # ktera pravidla ceniku pali porad
```

`--explain` je hlavní ladicí nástroj — ukáže cenu, credibility, odkud přišla
hodnota a proč verdikt dopadl, jak dopadl.

`--stats` odpovídá na otázku „funguje to vůbec". Nejdůležitější sloupec je
**zralé**: kolik položek už umí ocenit `HistoryOracle`. Měří se **časem od
prvního záznamu v `price_log`**, ne počtem pozorování — po dvou skenech je
položka viděná dvakrát, ale zralá až za dva dny. Dokud je nula, katalogový
zdroj mlčí právem.

Že katalogové cestování mlčí i se zralou historií je taky normální: trasa musí
spadnout **30 % pod vlastní medián** do souhrnu a **55 %** na okamžité
upozornění. Ceny dopravců se takhle nehýbou často.

Jestli je ten práh dosažitelný, ukáže sekce **JAK HLUBOKO POD VLASTNÍ MEDIÁN
SE POLOŽKY DOSTANOU**. Pro každou zralou položku počítá `minimum ÷ vlastní
vážený medián` za stejné okno, jaké používá `HistoryOracle`, a vypíše, kolika
položkám se to povedlo pod 0,90 / 0,80 / 0,70. Je to jediný způsob, jak
o prahu rozhodnout z dat: **u katalogového cestování se totiž neměří cena
letenky, ale nejlevnější nabídka na trase** — a ta je z podstaty blízko dna,
takže se hýbe podstatně míň než cena konkrétního termínu. Práh 0,70 vznikl
kalibrací na feedy, kde je referencí ceník z `flights.yaml`; na katalog se
přenesl bez měření. Sloupec `<=0,70` plný nul znamená, že se tenhle práh
u dopravců nedá splnit, a ne že ceny nepadají.

`--dry-run` má sekci **TĚSNĚ POD PRAHEM**: oceněné nabídky, které práh minuly,
seřazené podle toho, o kolik. Je to jediný způsob, jak poznat, jestli jsou
prahy utažené správně — plná sekce položek chybějících pár procent znamená, že
se práh možná ubírá o kus moc. Neoceněné položky se tam schválně nedávají: ty
práh neminuly, jen jim nikdo neurčil hodnotu, a to je jiná diagnóza.

Tokeny se berou z prostředí: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`OPENROUTER_API_KEY`, `ITAD_API_KEY`, `TRAVELPAYOUTS_TOKEN`. Chybějící
volitelný klíč nesmí nic shodit.

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

- **catalog** (Kinguin, Ryanair, Wizz Air, Travelpayouts) — tutéž položku vidíme
  opakovaně, stavíme si vlastní cenovou historii, deduplikuje se podle uid
  a poklesu ceny
- **feed** (Pepper, cestujlevne, zaletsi, travelfree, fly4free) — proud
  jedinečných příspěvků, historie nedává smysl, deduplikuje se podle `guid`
  v tabulce `seen`

Sloučit je do jednoho rozhraní by bylo chybné.

### Pořadí oracles je významné

V `main.py`: `history → references → flights → itad → declared`, AI běží zvlášť
dávkově až na tom, co zbylo. Katalogové cestování se k AI nedostane vůbec, viz
`catalog_history_only` níž. První oracle s odpovědí vyhrává, takže levné a důvěryhodné
zdroje předbíhají placené. `HistoryOracle` schválně vrací `None`, když cena
neklesla pod vlastní medián — tím pustí ke slovu ostatní.

### Credibility hlídá ocenění, ne položku

Každý zdroj odvozuje `Offer.credibility` z toho, co prodejce nezfalšuje:
prodejnost na Kinguinu, komunitní teplota na Pepperu, redakční výběr u fly4free.
Nízká hodnota položku nezahodí, jen jí zavře cestu k okamžitému upozornění.

Výjimka v `_finalize`: když hodnota přišla z `references` nebo `history`
s vysokou jistotou, práh credibility se přeskočí — víme, co ta věc stojí,
a nezáleží na tom, kolikátá je v žebříčku.

## Hlídání konkrétního záměru

`src/watch.py` je jediná část, která jde proti hlavnímu proudu bota. Zbytek
sbírá, co zdroje nabídnou, a hlásí, co je podezřele levné. Hlídání funguje
obráceně: člověk řekne „do Barcelony na devět nocí mezi polovinou srpna
a polovinou října, odlet v pátek večer, návrat v neděli odpoledne" a bot na to
hledá nejlepší možnost. Zakládá se z Telegramu přes `/hlidat`.

Stojí to na dvou endpointech Ryanairu, oba veřejné a bez klíče (změřeno
27. 8. 2026):

* **`farfnd/v4/roundTripFares` na konkrétní trase bere `durationFrom`
  a `durationTo`.** Dotaz na 9–9 vrátil přesně devět nocí. Je to týž endpoint,
  ze kterého `sources/ryanair.py` bere trasy — tam se `duration` použít nedá,
  protože odpověď zúží na zlomek sítě, ale **u jedné trasy funguje**. Odpověď
  navíc nese přesné časy odletu i příletu obou letů, takže „pátek večer" se dá
  vyhodnotit, ne odhadnout.
* **`timtbl/3/schedules/…` vrátí letový řád na měsíc jedním požadavkem.**
  Tohle je ta věc, na které celý návrh stojí: na trase PRG–BCN je **25 z 27 dnů
  jen jeden let denně** a jeho čas se den ode dne mění (21:30, 10:05, 13:35).
  „Nejlevnější let toho dne" a „jediný let toho dne" je tedy skoro vždycky
  totéž — čas se nedá vybrat, dá se vybrat **den, na který ten čas padne**.
  Řád se proto stáhne napřed a na ceny se ptáme jen na dny, které do zadání
  sedí. Je to zároveň jediná úspora dotazů, která tu funguje.

Věci, které vypadají jako nedodělek, ale nejsou:

**Časové okno má obě meze.** „Neděle do 15:00" splní i let v 5:45 — jenže ten
tě o ten víkend připraví, a smysl zadání byl opačný. Proto `back_after_h`
i `back_before_h`.

**Když nic nesedí, pošle se náhrada.** `Vysledek` nese dvě položky:
`vyhovujici` a `nahradni`. Přeostřené zadání (třeba neděle 11–18 z Barcelony,
odkud Ryanair v neděli létá jen v 5:45) by jinak znamenalo ticho — a ticho,
ze kterého se nepozná, jestli se nic nenašlo nebo je něco rozbité, je tady ta
nejhorší odpověď. Stejný důvod jako u sekce TĚSNĚ POD PRAHEM. Náhradní dotaz
stojí jeden požadavek a posílá se jen u hlídání, které dosud nikdy nic
nesplnilo, a každý jen jednou.

**Přehlašuje se jen zlepšení.** Rozhoduje `watches.best_czk`; do něj se zapisuje
**jen cena vyhovující nabídky**, náhradní si ukládá pouze `best_key`. Bez toho
by hlídání psalo tutéž letenku každou hodinu.

**Timer běží po deseti minutách kvůli PŘÍKAZŮM, ne kvůli cenám.** `run_watch`
je jediné místo, kde bot čte, co jsi mu napsal — bez webhooku, jen doptáním
přes `getUpdates`, takže není potřeba otevřený port ani veřejná adresa. Delší
cyklus by znamenal, že na odpověď na `/hlidat` čekáš půl hodiny. Ceny se
přepočítávají nejvýš jednou za `watch.min_interval_min` (výchozí hodina);
řídí to sloupec `checked`, ne timer.

**`offset` u `getUpdates` je povinný.** Bez potvrzení vrací Telegram tutéž
zprávu pořád dokola a `/hlidat` by se zakládalo při každém běhu znovu. Poslední
`update_id` se drží v `meta`.

**Příkazy se berou jen z vlastního chatu.** Bota si může najít kdokoliv;
`_zpracuj_prikazy` proto porovnává `chat.id` s `TELEGRAM_CHAT_ID` a cizí
zprávy tiše zahodí.

**Hlídání umí jen síť Ryanairu.** Wizz Air obdobu `duration` nemá a agregátory
neumí říct „devět nocí". Na evropský prodloužený víkend to stačí, na dálkové
lety ne — a nemá smysl to zakrývat.

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
10 000 požadavků. `ItadOracle.enrich_popularity` proto běží v `main.py` až na
seznamu `digest`, ne v `prepare()`. Vzorec kombinuje počet hodnocení
(logaritmicky) a skóre; hry bez hodnocení na Steamu — Battlefield, Call of Duty
— padají na `Offer.credibility`, tedy prodejnost na Kinguinu. `stats.rank`
z ITAD se schválně nepoužívá: neměřili jsme jeho rozložení.

**Neznámá popularita je u her důvod k mlčení.** Původně to bylo naopak —
„mlčet o něčem jen proto, že o tom nemáme data, by bylo horší". To platilo,
dokud byl katalog poloviční. Po rozšíření na celých 10 000 produktů se poměr
obrátil: neznámá popularita znamená buď že hru ITAD nezná (obskurní šunta),
nebo že došel strop `max_info_per_run` — a v obou případech je to slabší
kandidát než hra, o které víme, že ji lidi chtějí. Sekce se jinak plní
bezcennými tituly za pár korun, protože ty mají nejextrémnější poměr ceny
a v `notify._rank_key` se dostanou nahoru. Řídí to
`itad.require_known_popularity` a týká se to **jen her**; u předplatného
a cestování se popularita nezjišťuje, takže by tentýž filtr vymazal souhrn celý.

**Hru pod `games.min_value_czk` odmítá i skvělý poměr.** O úrovni rozhoduje
poměr ceny k hodnotě, a ten drobnosti systémově zvýhodňuje: hra za 3 Kč
z původních 100 vyjde na 3 %, kdežto AAA za 200 Kč z patnácti stovek na 13 %.
Souhrn se tím plnil věcmi za jednotky korun a velký titul v obrovské slevě
mezi nimi zapadl. Popularita to nespraví — ta měří, jestli hru někdo hrál, ne
jestli je to velký titul; povedená indie hra má hodnocení jako AAA. Rozhoduje
proto **ceníková cena**. Změřeno na katalogu GOG (12 648 her, ceny rovnou
v korunách): hluboko v katalogu je 100 % titulů pod 600 Kč, kdežto nad tou
hranicí leží Skyrim, System Shock nebo Silent Hill 2. Filtruje
`drop_cheap_games` v `main.py`, a to **před rozdělením na upozornění a souhrn**
— u her umí okamžité upozornění spustit vlastní cenová historie, takže
filtrovat jen souhrn by nestačilo.

**Ruční ceník nesmí spouštět položku, která má vlastní historii.** Ceníková
cena výrobce u licencí a předplatného na šedém trhu nikdy neplatí. Změřeno po
měsíci sbírání (`--check-references`): Windows 10 Pro má v ceníku 4 500 Kč
a na Kinguinu se prodává za 242 Kč, AVG Ultimate 5 400 proti 774, Gemini AI Pro
8 820 proti 145. U **22 pravidel ze 49** by položka prošla prahem i za svou
úplně běžnou cenu — a co pálí vždycky, není signál. Snížit sazby by znamenalo
kalibrovat ceník podle trhu, který zrovna posuzujeme; proto místo toho platí
totéž co u her a ITAD: jakmile má katalogová položka zralou historii, musí
ceníku dát za pravdu i ona, jinak neprojde. Řídí to `_reference_needs_history`
ve `score.py` a podmínkou je **historické minimum**, ne další práh v procentech
— je to bezparametrové a odpovídá to na otázku „je zrovna teď dobrá chvíle to
koupit". Studený start zůstává: bez historie rozhoduje ceník jako dřív, takže
Gemini za 65 Kč projde i u položky, o které ještě nic nevíme.

**Vadné pravidlo v ceníku nepozná pohled, jen měření.** Cena v pravidle může
být úplně správná ceníková cena výrobce a přesto být k ničemu: antivirus,
VPN ani Windows se za ceníkovou cenu nikdy neprodávají, takže pravidlo hlásí
slevu pořád — a co pálí vždycky, není signál. Ceník navíc obchází práh
credibility, takže jedno vadné pravidlo znamená desítky zpráv, ne jednu.
Ukáže to `--check-references`: pro každé pravidlo spočítá, kolik procent
položek by prahem prošlo **i za svou úplně běžnou cenu**. Sto procent je
diagnóza. Schválně se tam neměří „správná hodnota" — na to by se muselo sáhnout
po ceně na trhu, který zrovna posuzujeme, a to je tentýž kruh jako u Krakova
za 748 Kč. Vysoký faktor sám o sobě chyba není, Kinguin je šedý trh.

**`value_czk_per_month` × počet měsíců u víceletých licencí přestřeluje.**
Komentář v `references.yaml` tvrdil, že to „škáluje správně i u tříleté
licence" — neškáluje. Výrobci prodávají tři roky zhruba za dvojnásobek roku,
ne za trojnásobek, takže tříletý ESET dostane hodnotu 3 600 Kč tam, kde
výrobce chce 2 000. Chyba se sčítá s tou předchozí a výsledkem je pravidlo,
které pálí vždycky.

**Popularita se nedostala do `score.py`.** Je to věc řazení a filtrování
souhrnu, ne ocenění, a `score.py` nemá vědět, že něco jako hra existuje.
Filtruje `drop_unpopular` v `main.py`, řadí `_rank_key` v `notify.py`. Ze
stejného důvodu je tam i `drop_cheap_games` — `score.py` nemá vědět, že něco
jako hra existuje.

**V souhrnu se vedle poměru píše plná cena.** „13 %" samo o sobě nerozliší
velkou hru za dvě stě korun od drobnosti za tři, takže `notify._detail`
vypisuje „13 % z 1 490 Kč". Fronta si proto v `_queue` nese i `value_czk`.

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

**Zadní vrátka k AI zavírá `catalog_history_only`.** Samotné mlčení
`FlightOracle` nestačilo: neoceněná položka putuje dál k AI soudci a ta trase
vymyslí „běžnou cenu", proti které je ceník dopravce z podstaty levný — tentýž
kruh, jen o patro níž. Proto `thresholds.by_category` u `flight` a `hotel` nese
`catalog_history_only: true` a `score.ai_candidates` katalogové položky téhle
kategorie vůbec nepustí dál. U her je odhad zvenčí naopak v pořádku: mají
doporučenou cenu, kterou AI zná. Letenka nic takového nemá.

**Historické minimum se počítá proti `products.prev_min`, ne `min_ever`.**
`record_price` v `main.py` běží **dřív** než scoring, takže `min_ever` už
aktuální cenu obsahuje — proti němu by „historicky nejnižší cena" platila pro
každou položku hned při prvním pozorování a pro každou nehybnou cenu napořád.
Změřeno na Ryanairu: všech ~130 tras se takhle první běh označilo za historické
minimum a šlo k AI soudci. `prev_min` drží minimum před zápisem a porovnání je
**ostře** menší; stejná cena jako dosud není nález. Sloupec doplňuje `_migrate`,
protože `CREATE TABLE IF NOT EXISTS` existující databázi nesáhne.

**Cestovatelské prahy se nevybírají podle sekce, ale podle `Offer.category`.**
A ta u Pepperu nese jazyk toho webu — `Urlaub & Reisen`, `Travel`, `Voyage`.
V souhrnu položka do sekce Cestování spadla správně (`notify.group_of`), jenže
`score._thresholds_for` na ni sáhl prahem pro digitální klíče, takže týdenní
pobyt na Korfu za 313° nemohl projít nikdy. `thresholds.by_category` proto nese
i tyhle klíče přes YAML kotvu a test `test_travel_vocabulary_does_not_drift`
hlídá, že se oba seznamy nerozejdou.

**Cizí uzel projde jen u dálkového cíle.** Doletět do Dublinu za osm stovek
a ušetřit deset tisíc na Ameriku dává smysl; jet do Frankfurtu kvůli Malaze ne.
`sources.travel.hub_airports` proto pouští nabídky z patnácti evropských uzlů,
ale `main.drop_pointless_hubs` je zahodí, pokud `typical_czk` regionu cíle
nedosáhne `hub_min_typical_czk` (12 000 Kč). Rozhoduje ceník, ne další ruční
seznam — data se dělí sama: dálkové regiony mají 12 000 a výš, celá Evropa
2 500–4 500 a Blízký východ 5 500. Nerozpoznaný cíl se zahodí; u cizího odletu
má nabídka důkazní břemeno navíc. Změřeno na živých feedech: prošlo 18 nabídek
(Dublin–Toronto za 2 316 Kč, Dublin–USA za 2 533 Kč), zahodily se čtyři
evropské skoky typu Berlín–Madeira.

**Název uzlu je zároveň místem v ceníku.** „from Dublin to New York" tedy
matchne Dublin i New York; vyhrává dražší region (viz níž), takže cíl přebije
odletové město. U nerozpoznaného cíle zbude region uzlu, a protože všechny uzly
jsou evropské, položka správně propadne prahem.

**Obecné „USA" míří na východní pobřeží, ne na západní.** Původně to měly oba
regiony a při řazení podle ceny sestupně vyhrával západ za 18 000 Kč — běžná
letenka do New Yorku tím vypadala jako trhák. Levnější strana je bezpečnější:
podstřelená hodnota mlčí, přestřelená posílá falešné poplachy.

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

**Odpověď 4xx se neopakuje.** `Http.get` zkouší znovu jen `429` a `5xx` — tedy
„teď ne". `403` a `404` znamenají „ne" a trojnásobek požadavků na WAF, který
nás právě odmítl, je nejjistější způsob, jak si blokaci potvrdit natrvalo.
Slouží k tomu `TrvaleOdmitnuto`. Vedle toho existuje `Http.probe`: vrátí
stavový kód a nevyhodí výjimku, protože při oťukávání verze Wizz Airu je `404`
platná odpověď, ne porucha.

**Feedy si říkají o RSS hlavičkou `Accept`.** `requests` posílá holé `*/*`,
což je u WAF nad WordPressem jeden ze signálů „tohle je bot". Adresy feedů se
zároveň píšou v kanonickém tvaru — `travelfree.info` bez `www.`, protože `www.`
je jen přesměrování a každý přeskok navíc je požadavek, o který si říkat
nemusíme.

**ETag se ukládá až po úspěšném parsování.** V `travel._load` je pořadí
podstatné: kdyby server vrátil 200 s rozbitým tělem a my si značku uložili,
příští běh pošle `If-None-Match`, dostane 304 a ten feed **zmlkne natrvalo**,
dokud se obsah náhodou nezmění. Tichá ztráta celého zdroje za jednu vadnou
odpověď je horší než stáhnout ho příště znovu celý.

**Selhání zdroje se hlásí, ale až napotřetí.** `collect` počítá selhání po
sobě v `meta` a při třetím pošle zprávu — právě jednou, jinak by zablokovaný
zdroj psal každých deset minut. Po prvním úspěchu se čítač nuluje a ozve se
i zotavení. Bez toho bylo selhání tiché: log napsal „přeskakuji" a bot mohl
týden mlčet, aniž by to vypadalo jinak než na to, že nejsou slevy.
Aktuální stav ukáže `--stats` v sekci ZDRAVÍ ZDROJŮ.

**Záloha jde přes zálohovací API SQLite, ne přes `cp`.** Sken běží každých
deset minut, takže kopírování souboru by mohlo trefit rozepsanou transakci.
`store.backup` navíc nepotřebuje nástroj `sqlite3` v systému. Timer
`slevy-backup` ji dělá denně ve 4:20 a drží posledních sedm; `Persistent=true`
dohání zmeškané běhy, protože záloha vynechaná kvůli restartu je přesně ta,
která pak chybí.

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

**Samostatné feedy na ubytování** u našich zdrojů nejsou. `cestujlevne.com`
i `fly4free.com` vracejí na `…/ubytovani/feed`, `…/zajezdy/feed`,
`…/hotel-deals/feed/` čtyřistačtyřku. `travelfree.info/tag/hotel/feed/`
odpovídá, ale je to **archiv**: nejnovější z 25 položek je 497 dní stará,
medián přes šest let, jedna se sama označuje `**EXPIRED**`. Filtr `max_age_days`
by ji stejně zahodil. Hotely a pobyty chodí hlavním feedem, kde je všechno
čerstvé — změřeno 24 z 70 položek napříč zdroji, z toho 0 špatně zařazených.

**Velké zahraniční slevové weby nedávají nic**, přestože feedy mají a jsou
plné čerstvých položek: `urlaubspiraten.de` (45 položek, všechny čerstvé),
`holidaypirates.com` (44), `travelpirates.com` (14) a `wakacyjnipiraci.pl` (33).
Protaženo skutečným parserem: přes filtr odletových letišť prošlo u všech
**nula**. Odlétá se z Německa, Británie a Polska. Přidat je by znamenalo pustit
i odlety, na které se odsud nedostaneš.

Jediná pobočka té sítě, která by dávala smysl, je rakouská `ferienpiraten.at`
(odlety z Vídně). **Nefunguje.** Doména se přeloží (45.87.158.7), ale port 443
spojení odmítne — ověřeno i z ostrého serveru, kde `curl` selhal za 181 ms.
Není to blokace naší sítě, ten web na HTTPS prostě neodpovídá.

**U Pepperu původní cena většinou vůbec není.** Parser je stavěný na `statt`,
`UVP` a `RRP`, jenže ve vzorku 107 nabídek se neobjevilo **ani jedno**. Popisy
znějí „192,49€ - AliExpress, Der Deal beginnt am 1. August" — je to komunitní
tip, ne kalkulace slevy. Vylepšovat regulární výrazy je proto práce na špatném
místě; hodnotu musí dodat AI soudce.

Proto `thresholds.min_credibility_ai_shipping_ok`. Ze 107 nabídek jich **53
pochází z obchodů s potvrzeným doručením do ČR**, ale jen 8 má původní cenu.
Při jednotném prahu 0,8 (na Pepperu 400°) se k soudci dostaly 4 a z celého
zdroje chodily dvě zprávy na sto nabídek. Nabídka, u které víme, že se dá
koupit, si zaslouží nižší laťku než ta, u které to nevíme — 0,3 pustí 29 místo
4. Nákladově je to malé, protože feedy se deduplikují podle `guid`, takže každý
příspěvek jde k AI **jednou za život**, ne při každém běhu.

**Zalando Lounge nejde a nešel by ani s přístupem.** Změřeno: titulní stránka
je přihlašovací, `/api/campaigns` i `/api/mobile/campaigns` vracejí 403, běžné
`zalando.cz/api/catalog` taky. V hlavních feedech Pepperu se Lounge zrovna
neobjevil ani jednou; v kategoriových se objevuje, jenže ty nemají teplotu
(viz níž).

Horší než blokace je ale to, že **u módy nemáme z čeho udělat hodnotu**.
`value_ratio` potřebuje referenci, kterou prodejce neurčuje — u her je to ITAD
a vlastní historie, u letenek ceník a historie. U oblečení existuje jen
doporučená cena, kterou si Lounge nastavuje sám, a žádná obdoba ITAD. Je to
přesně tvar pasti „Tanks Battle": vysoká deklarovaná sleva bez možnosti ověření.
Jediná schůdná cesta by byla vlastní cenová historie konkrétního artiklu, a ta
vyžaduje přihlášenou relaci — tedy uložené přihlášení uživatele na serveru.
To se dělat nebude.

**Kategoriové feedy Pepperu nemají teplotu.** `mydealz.de/rss/gruppe/kleidung`,
`…/sneaker`, `…/schuhe` i `…/fashion-accessoires` existují a vracejí po 30
položkách, jenže **v žádném titulku není `123°`** — na rozdíl od `/rss/hot`,
kde ji má všech 30. Ta čísla nejsou navíc jen ozdoba: teplota je jediný signál
kvality, který u Pepperu prodejce nezfalšuje, a odvozuje se z ní `credibility`.
Kategoriový feed tedy není „studený", on prostě **nemá čím doložit kvalitu** —
a bez toho je to jen proud všeho nového. Přidat ho by šlo jen za cenu zrušení
prahu teploty, tedy hlavní obrany proti braku u feedů. Módní slevy, které za
něco stojí, se stejně objeví v `hot` (změřeno: 28 z 30 tam projde, medián 131°).

**Další české pokusy:** `akcniletenky.cz` vrací platné RSS, ale **prázdné**.
`honzovyletenky.cz` má chybný certifikát — obejít se dá jen vypnutím ověřování,
což je za tuhle nabídku špatná cena. `letuska.cz/blog`, `letenky-levne.cz`
a tagové feedy cestujlevne.com vracejí 404; `pelikan.cz`, `travelking.cz`
a `dovolena-levne.cz` vracejí HTML. `secretflying.com` a `flynous.com` odpovídají
403 i s prohlížečovou hlavičkou.

**Travelpayouts** (`sources/travelpayouts.py`) — data Aviasales, třetí
katalogový zdroj. Vidí napříč dopravci včetně přestupů, takže dosáhne i na
dálkové trasy, kde `flights.yaml` odhaduje nejhůř. Dotaz **bez cílové stanice**
vrací nejlevnější cíle — otázku „kam se teď dá letět levně" jiný náš zdroj
položit neumí (u Wizz Airu ověřeno, že obdobu nemá).

Token jde v hlavičce `X-Access-Token`, **nikdy v URL** — v query stringu by
skončil v logu proxy i v historii serveru. Ověřeno s ostrým tokenem 28. 7. 2026
přes `--check-travelpayouts`, který projde žebřík variant dotazu a vypíše
syrová jména polí vedle toho, co z nich zdroj složil.

Dvě věci, na které se přišlo až tím měřením a bez kterých zdroj ztrácí většinu
užitku:

* **`departure_at` se schválně neposílá.** Není to mez okna, ale skutečný
  termín letu — rozsah `departure_at` + `return_at` půl roku od sebe vracel
  `400`. A i správně zadaný měsíc odpověď **zúží ze 100 tras na 31**. Bez něj
  se ptáme na „nejlevnější, co je teď na téhle trase v prodeji", což je pro
  cenovou historii lepší definice: nemá skok na přelomu měsíce.
* **`limit` je potřeba.** Bez něj vrátí API 30 tras, s ním 100.

Odpověď nese `origin`, `destination`, `price`, `transfers`, `departure_at`,
`link`, `airline` a doby letu. `link` je relativní (`/search/PRG2208SKP1?t=…`),
takže se předsazuje `https://www.aviasales.com`.

Používají se **dva endpointy**. `v3/prices_for_dates` dodává nabídky, které se
sbírají, a `v3/grouped_prices` k nim dopisuje **cenový kalendář trasy** — bez
něj bot vidí jen dnešní cenu a nemá jak poznat, že kouká shodou okolností na
drahý termín.

Změřeno ostrým tokenem 27. 8. 2026 (druhá půlka `--check-travelpayouts`):

| endpoint | odpověď |
|---|---|
| `v3/grouped_prices` | **83 dnů**, klíčem datum, pole jako u `prices_for_dates` včetně `link` |
| `v3/get_latest_prices` | 23 záznamů, jiná pole (`value`, `found_at`, `actual`) |
| `v1/prices/calendar` | 51 dnů, má `expires_at`, ale **nemá `link`** |
| `v2/prices/month-matrix` | 30 dnů, pole jako `get_latest_prices` |
| `v1/prices/cheap` | zanořené dvakrát (cíl → pořadí → záznam) |
| `v1/city-directions` | 30 cílů — to už umí `prices_for_dates` bez cílové stanice |
| `v1/prices/month-matrix` | **404**, mrtvý |

Vyhrál `grouped_prices`: nejdelší okno, `link` na ten levnější termín a stejná
jména polí jako u endpointu, který už parsujeme.

Tři věci, bez kterých je kalendář na škodu:

* **`one_way` se posílá podle nabídky.** Jednosměrná stojí zhruba polovinu
  zpáteční, takže porovnat jedno s druhým by vyrobilo falešný propad — tentýž
  důvod, proč mají jednosměrné trasy u Ryanairu vlastní uid.
* **Ptá se se jen na to, co odchází do Telegramu**, a trasy se deduplikují
  (tutéž zná Ryanair i Travelpayouts). Jeden požadavek na trasu, strop
  `calendar_max_per_run`. Na celém katalogu by to bylo nemyslitelné — stejný
  důvod jako u `ItadOracle.enrich_popularity`.
* **Nikdy z toho nevzniká hodnota.** Je to údaj do zprávy. Porovnávat cenu
  dopravce s trhem je přesně ten kruh, kvůli kterému bot hlásil Krakov za
  748 Kč jako trhák; hodnotu u katalogového cestování smí dodat jen vlastní
  historie, viz `catalog_history_only`.

**Google Flights nemá jak.** Vlastní API Googlu na letenky (QPX Express) je
vypnuté od dubna 2018 a náhrada není — ITA Software, která Google Flights
pohání, se licencuje smluvně dopravcům, ne samoobsluhou. Zbývalo by stahovat
web, jenže ten z Evropy vůbec nedojde k výsledkům: `www.google.com/travel/
flights` přesměruje na **`consent.google.com`** a vrátí souhlasovou zeď
(změřeno — dva megabajty HTML, v nich ani jedna cena). I kdyby se obešla,
výsledky se dokreslují JavaScriptem a hledání se zadává protobufem v parametru
`tfs`, který se mění. Placení překupníci (SerpApi a spol.) existují, ale stojí
desítky dolarů měsíčně za data, která Travelpayouts dává zdarma.

Hlavně by to ale nic nespravilo: u katalogového cestování rozhoduje **vlastní
cenová historie**, ne šíře katalogu. Čtvrtý zdroj tras potřebuje přesně stejné
dva dny zrání jako ty tři stávající.

**Letenková API:** `tequila.kiwi.com` je dnes **jen na pozvání** — portál nemá
samoobslužnou registraci, jen přihlášení a odkaz na `affiliates@kiwi.com`.
`api.travelpayouts.com` odpovídá `401`, tedy žije a chce token, který se dá
získat samoobsluhou.

**Amadeus je nedosažitelný.** `api.amadeus.com` ani `test.api.amadeus.com` se
nepřeložily přes DNS — ověřeno nezávisle z vývojového prostředí i z ostrého
serveru, zatímco `developers.amadeus.com` se přeloží. Nemá smysl to zkoušet
potřetí. I kdyby se to rozchodilo, bezplatná úroveň je **testovací prostředí
s nacachovanými daty**, ne živé ceny; na lovce slev nepoužitelné. Ostrá data
chtějí produkční přístup s platební kartou, kdežto Travelpayouts vrací živé
ceny a token dává zdarma.

**Cestovatelské weby:** `secretflying.com/feed/` vrací HTML, `fly4free.pl`
error-fare feed je prázdný, veřejné náhledy `t.me/s/…` u těchhle webů neexistují.
`theflightdeal.com` a `airfarespot.com` fungují, ale mají US trasy.

**Herní obchody:** Eneba 400 + IP allowlist (GraphQL vrací 400), G2A 403,
Gamivo `401` na `/api/public/v1/products` (endpoint existuje, chce partnerský
klíč), Humble 403, Reddit vyžaduje OAuth.

**Kinguin je mezi marketplace s klíči výjimka, ne pravidlo.** Proměřeno
šestnáct obchodů 28. 7. 2026 a otevřené JSON bez klíče má kromě něj jen HRK
a GOG; zbytek blokuje boty nebo chce schválení partnera: CDKeys 403,
Instant Gaming vrací HTML, Driffle 404, K4G 500, Green Man Gaming 500,
Voidu 410, Nuuvem 404, AllKeyShop 404, Difmark a RoyalCDKeys shodily
certifikát, DLCompare a Gamesplanet nemají JSON. **Pro levné licence na
software a předplatné tedy jiný zdroj tohohle typu nemáme** — a je to
měřením podložené, ne odhad.

**GOG má nejlepší data ze všech, ale jen hry.** `catalog.gog.com/v1/catalog`
je bez klíče, vrací 12 497 produktů, ceny **rovnou v korunách** a hlavně
`price.base` vedle `price.final` — a to je **skutečná ceníková cena obchodu**,
ne MSRP vymyšlená prodejcem jako na Kinguinu. Nese i `storeLink`,
`reviewsRating` a `reviewsCount`, takže popularita je v odpovědi rovnou
a nemusí se doptávat ITAD. Kdyby se hry někdy dělaly pořádně, tohle je ten
zdroj; `DeclaredOracle` by u něj platil bez výhrad.

**HRK Game** (`hrkgame.com/api/products/`) vrací 100 položek bez klíče, ale
jen `title`, `price`, `platform`, `region` a obrázek — **žádný odkaz ani id**.
Bez URL se nedá poslat použitelná zpráva a `limit` API ignoruje.

**Steam jako zdroj nabídek nedává smysl** — `store.steampowered.com/api/
featuredcategories/` sice vrací čistý JSON se slevou proti MSRP, ale je v něm
jen **10 položek** (výběr na titulku, ne katalog akcí) a slevy jsou 15–80 %.
Proti prahu 0,20 pro hry projde do souhrnu jedna z deseti a okamžité upozornění
nespustí nikdy. `/api/appdetails` funguje a jako *ceník* by smysl dával — jenže
přesně to už dělá ITAD, který agreguje Steam i ostatní obchody.

**Tequila API od Kiwi.com** odpovídá `403` bez klíče, tedy endpoint žije. Klíč
si musí zařídit uživatel sám; jestli se novým partnerům pořád vydávají, se bez
registrace ověřit nedá a účty nezakládáme.

**AppSumo feed nemá** — `/feed/` je 404, `/rss/` vrací HTML. Zbýval by scraping.

**Fyzické zboží:** `de.camelcamelcamel.com/top_drops/feed` funguje (20 položek
RSS, propady cen na německém Amazonu). **Geizhals nefunguje** — z ostrého
serveru vrací `403` na `.de`, `.at` i `.eu`, tedy ochrana proti botům, ne
problém certifikátu, jak to vypadalo odjinud. Řadí se k Alze, Heurece
a Slevomatu. `honzovyletenky.cz` má chybný certifikát; obejít by to šlo jen
vypnutím ověřování, což je za deset položek špatná cena.

## Externí rozhraní

**Kinguin** — neoficiální interní JSON API, bez klíče. Ceny v **eurocentech**.
Katalog má **strop 10 000 produktů** (100 stran po 100) a bere se celý. Dřív
se braly jen první 4, protože se předpokládalo, že hlouběji už nic není —
**změřeno, že to neplatí**: podíl předplatného a softwaru s hloubkou v žebříčku
neklesá, na poslední straně je stejných ~10 % jako uprostřed. V pořadí
5000–9999 leželo 479 takových položek a mezi nimi Gemini AI Pro na 3 měsíce
za 75 Kč. Na celém katalogu je 956 položek předplatného a softwaru, ceník
z nich ocení 180.
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

Kromě `roundTripFares` se čte i **`oneWayFares`**, protože zpáteční vyhledávání
vidí jen zlomek sítě. Změřeno 28. 7. 2026: zpáteční zná **59 tras**, jednosměrné
**151** — tedy 92 tras, o kterých bot jinak vůbec neneví. U Vídně je rozdíl
18 proti 70. Jednosměrné mají **vlastní uid** (`PRG-BGY:ow`): stojí zhruba
polovinu zpáteční, takže společná časová řada by z každého prohození druhu
udělala falešný propad ceny.

Odkaz na rezervaci musí nést **`dateOut` i `dateIn`** a k tomu celou sadu
parametrů včetně duplicitní `tp*` kopie — viz `_odkaz`. Se samotným
`originIata`/`destinationIata` stránka jen dlouho točí kolečkem a skončí na
„Nemáte aktivní vyhledávání". Ověřeno v prohlížeči. Wizz Air tímhle netrpí,
jeho `/booking/select-flight/PRG/OTP/2026-09-11` funguje.

API vrací **nejlevnější dvojici v celém okně**, a ta bývá nepoužitelná: přílet
ve 22:55 a odlet druhý den v 11:30 je devět hodin na místě, z toho osm
prospaných. Cena je pravdivá, nabídka ne — proto `notify.format_term` vypisuje
termín včetně počtu nocí. Filtrovat to na straně API **nejde**: `durationFrom`
a `durationTo` sice projdou, ale i logicky prázdný rozsah 1–30 nocí srazí
odpověď z 18 tras na 6. Nefiltruje, přepíná do jiného a mnohem chudšího režimu.

**Wizz Air** (`sources/wizzair.py`) — druhý katalogový zdroj, doplňuje Ryanair:
z Vídně nelétá, zato z Bratislavy má 38 tras (Praha 20). Tři věci, bez kterých
to nefunguje a na které se přijde jen měřením:

* **Cookies se musí před každým dotazem zahodit.** Server přiloží
  `RequestVerificationToken` a u dalšího dotazu ho chce zpátky v hlavičce,
  jinak vrátí `InvalidProtocol`. Bez toho projde z celé dávky **jen první
  trasa** a zbytek tiše propadne — chyba, která se tváří jako prázdný zdroj.
* `dayInterval` musí být **aspoň 3**, jinak validace odmítne dotaz.
* **Verze je v cestě** (`be.wizzair.com/29.13.0/…`) a zvedá se. Zastaralá
  znamená `404` na všechno, takže zdroj zmlkne úplně — v logu zůstane jediný
  řádek o mapě linek a vypadá to na prázdný zdroj, ne na poruchu. Přesně tak
  Wizz Air vypadl 24. 8. 2026: v konstantě bylo 29.8.0.

Verze se proto zjišťuje ve třech krocích (`verze`, `_preladit`): zapamatovaná
hodnota z `meta`, číslo vyčtené z jejich webu, a nakonec **oťukání
`be.wizzair.com`**. Ten třetí krok je tam proto, že web se z ostrého serveru
načíst nemusí — `www.wizzair.com` odtamtud vrací `405` — kdežto `be.wizzair.com`
odpovídá normálně. Oťukává se přes `Api/asset/farechart`: GET na živou verzi
vrátí `405` a 82 bajtů, na mrtvou `404`; mapa by stála 666 kB na pokus.
Zapamatování je zároveň úspora — web má dva megabajty a stahovat ho každých
deset minut kvůli jednomu číslu je čtvrt gigabajtu denně. Oťukávání se pouští
nejvýš jednou za šest hodin, jinak by z výpadku byla palba.

Trasy se mezi běhy **střídají** (`routes_per_run`). Zeptat se na všech 58
každých deset minut by bylo přes osm tisíc požadavků denně.

**Cestování** (`sources/travel.py`) — RSS, jeden parser na všechny weby.
`zaletsi.cz` je druhý český zdroj téhož tvaru jako `cestujlevne.com` (koruny,
odlety z PRG/VIE/BTS/OSR, letenky i zájezdy) a prošel existujícím parserem bez
jediné úpravy kódu — je to čistě řádek v konfiguraci.
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
