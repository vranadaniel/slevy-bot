# Nasazení na DigitalOcean krok za krokem

Návod počítá s tím, že máš droplet s Ubuntu nebo Debianem. Celé to zabere
zhruba deset minut.

---

## Než začneš: připrav si pět hodnot

**GitHub Secrets se nedají přečíst zpátky** — jsou jednosměrné. Když sis tokeny
nikam neuložil, musíš si je obstarat znovu. Připrav si je do poznámkového bloku
ještě předtím, než se přihlásíš na server.

| Hodnota | Kde ji vzít |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/mybots` → tvůj bot → **API Token** |
| `TELEGRAM_CHAT_ID` | znáš z dřívějška, nebo se dá znovu vypsat (viz krok 6) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) — starý klíč nejde zobrazit, vytvoř nový |
| `ITAD_API_KEY` | [isthereanydeal.com/apps/my/](https://isthereanydeal.com/apps/my/) → tvoje aplikace |
| `TRAVELPAYOUTS_TOKEN` | [travelpayouts.com](https://www.travelpayouts.com) → registrace zdarma → API token |

Poslední tři jsou volitelné. Bez nich bot běží dál — jen mlčí AI soudce,
hry zůstanou neoceněné a nejlevnější cíle napříč dopravci se nesbírají.

---

## 1. Přihlas se na droplet

**Přes web DigitalOcean** (nejjednodušší, nic nenastavuješ):

1. Otevři [cloud.digitalocean.com](https://cloud.digitalocean.com)
2. V levém menu klikni na **Droplets**
3. Klikni na název svého dropletu
4. Vpravo nahoře je tlačítko **Console** — klikni na něj
5. Otevře se černé okno terminálu, přihlášené jako `root`

**Nebo přes SSH z Windows**, když máš klíč nastavený. Otevři PowerShell a použij
IP adresu, kterou vidíš na stránce dropletu:

```bash
ssh root@TVOJE.IP.ADRESA
```

---

## 2. Ověř, že je to Ubuntu nebo Debian

Instalační skript používá `apt`, takže na CentOS ani Alpine nepoběží.

```bash
cat /etc/os-release
```

Na prvním řádku musí být `Ubuntu` nebo `Debian`. Když tam bude něco jiného,
napiš mi to a vyřešíme to jinak.

---

## 3. Spusť instalaci

Když řádek s výzvou končí `#`, jsi root a `sudo` nepotřebuješ:

```bash
curl -fsSL https://raw.githubusercontent.com/vranadaniel/slevy-bot/main/deploy/install.sh | bash
```

Když končí `$`, jsi běžný uživatel:

```bash
curl -fsSL https://raw.githubusercontent.com/vranadaniel/slevy-bot/main/deploy/install.sh | sudo bash
```

Poběží to minutu až dvě. Uvidíš modré řádky `==>` ukazující, co se právě děje.
Na konci vypíše seznam timerů a co zbývá udělat.

Když skončí hláškou o převzetí stavu z větve `data`, je to dobře — znamená to,
že si bot přinesl cenovou historii a paměť už odeslaných upozornění z GitHubu.

---

## 4. Vlož tokeny

```bash
nano /etc/slevy-bot/env
```

Otevře se jednoduchý editor. Šipkami najeď na řádek a přepiš text za `=`
svými hodnotami. Výsledek má vypadat nějak takhle:

```
TELEGRAM_BOT_TOKEN=8123456789:AAF...zbytek
TELEGRAM_CHAT_ID=123456789
OPENROUTER_API_KEY=sk-or-v1-...
ITAD_API_KEY=...
```

Kolem `=` nedávej mezery a hodnoty nedávej do uvozovek.

**Uložení v nano:**

1. `Ctrl` + `O` — uložit
2. `Enter` — potvrdit název souboru
3. `Ctrl` + `X` — zavřít editor

---

## 5. Ověř, že Telegram funguje

```bash
cd /opt/slevy-bot && set -a && . /etc/slevy-bot/env && set +a && .venv/bin/python -m src.main --test-telegram
```

Na Telegramu ti má přijít zpráva „Test spojení". Když nepřijde, zkontroluj
token a chat_id v souboru z předchozího kroku.

---

## 6. Neznáš chat_id?

Napiš svému botovi na Telegramu libovolnou zprávu a pak spusť:

```bash
cd /opt/slevy-bot && set -a && . /etc/slevy-bot/env && set +a && .venv/bin/python -m src.main --print-chat-id
```

Vypíše číslo, které patří do `TELEGRAM_CHAT_ID`. Vrať se ke kroku 4 a doplň ho.

---

## 7. Spusť první sken

```bash
systemctl start slevy-scan
```

Chvíli to trvá — projít 5 000 položek a doplnit ceny z ITAD zabere minutu.
Pak se podívej, co se dělo:

```bash
journalctl -u slevy-scan -n 40 --no-pager
```

Hledej poslední řádek, vypadá takhle:

```
INFO  slevy: Odesláno 3 okamžitých upozornění, ve frontě souhrnu 47 položek
```

Nula odeslaných není chyba — znamená to, že všechno, co by stálo za zprávu,
už ti bot poslal dřív a deduplikace správně mlčí.

---

## 8. Zkontroluj, že poběží samo

```bash
systemctl list-timers 'slevy-*'
```

Uvidíš tabulku s tím, **kdy poběží příště** — přesně ten přehled, který
GitHub Actions nikdy neuměly zobrazit:

```
NEXT                        LEFT     UNIT                 ACTIVATES
Fri 2026-07-24 12:20:00 CET 3min     slevy-travel.timer   slevy-travel.service
Fri 2026-07-24 12:43:00 CET 21min    slevy-scan.timer     slevy-scan.service
Fri 2026-07-24 19:09:00 CET 7h       slevy-digest.timer   slevy-digest.service
Sat 2026-07-25 04:20:00 CET 16h      slevy-backup.timer   slevy-backup.service
```

Když jsou ve všech čtyřech řádcích rozumné časy, máš hotovo. Hlavní sken poběží
v :13 a :43 každou hodinu, rychlý sken letenek každých deset minut a souhrn
v 19:09.

---

## Když se něco pokazí

**Sken skončil chybou**

```bash
systemctl status slevy-scan --no-pager
journalctl -u slevy-scan -n 60 --no-pager
```

**Chodí moc zpráv**

Prahy uprav **doma v repozitáři**, ne na serveru — `install.sh` dělá
`git reset --hard`, takže by ti změny na serveru při další aktualizaci zmizely.

Doma v `config.yaml` sniž `instant_ratio` z `0.05` třeba na `0.03`, pak:

```bash
git commit -am "Zprisnit prah" && git push
```

A na serveru:

```bash
bash /opt/slevy-bot/deploy/install.sh
```

Rychlá zkouška bez čekání na další běh:

```bash
systemctl start slevy-scan && journalctl -u slevy-scan -n 20 --no-pager
```

**Chci vidět souhrn hned**

```bash
systemctl start slevy-digest
```

**Aktualizace na novou verzi**

Tentýž příkaz jako při instalaci. Je idempotentní a tokeny nepřepisuje:

```bash
bash /opt/slevy-bot/deploy/install.sh
```

> **Pozor: tenhle příkaz patří NA SERVER, ne do PowerShellu na tvém počítači.**
> Windows příkaz `bash` nezná a adresář `/opt/slevy-bot` na notebooku
> neexistuje. Když uvidíš hlášku `The term 'bash' is not recognized`, znamená
> to, že jsi ho napsal na špatném místě — vrať se ke kroku 1 a otevři si
> konzoli dropletu.

Podrobný postup krok za krokem je v `AKTUALIZACE.md` vedle tohohle souboru.

**Chci vidět, co našly letenky**

```bash
systemctl start slevy-travel && journalctl -u slevy-travel -n 30 --no-pager
```

**Chci to celé vypnout**

```bash
systemctl disable --now slevy-scan.timer slevy-travel.timer \n                        slevy-digest.timer slevy-backup.timer
```

---

## Kde co leží

| Cesta | Co to je | Přežije aktualizaci? |
|---|---|---|
| `/opt/slevy-bot` | kód, virtuální prostředí, konfigurace | **ne** — přepíše se z gitu |
| `/opt/slevy-bot/data/deals.db` | cenová historie a paměť odeslaných upozornění | ano |
| `/etc/slevy-bot/env` | tokeny, práva 600, čte jen root | ano |
| `journalctl -u slevy-scan` | logy | ano |

Cokoliv, co chceš zachovat, patří buď do gitu (kód, prahy, ceník), nebo mimo
adresář s kódem (databáze, tokeny).
