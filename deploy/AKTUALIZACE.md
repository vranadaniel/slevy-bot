# Jak nasadit novou verzi na server

Návod pro situaci, kdy je bot **už jednou nainstalovaný** a jen se má
aktualizovat na novou verzi. První instalace je v `NAVOD.md`.

Celé to trvá dvě minuty a skládá se ze dvou příkazů. Nejtěžší na tom je dostat
se na server — proto je to rozepsané tak podrobně.

---

## Nejdřív to nejdůležitější

Jsou tu **dva různé počítače** a je zásadní nesplést si je:

| | Tvůj notebook (Windows) | Droplet (server u DigitalOcean) |
|---|---|---|
| Co tam je | pracovní kopie projektu v OneDrive | běžící bot |
| Jaká konzole | PowerShell | černé okno konzole dropletu |
| Co se tam dělá | úpravy kódu, testy, `git push` | `install.sh` |
| Znak před kurzorem | `PS C:\...>` | `root@nazev-dropletu:~#` |

**Příkazy z tohoto návodu patří na droplet.** Když je napíšeš do PowerShellu na
notebooku, Windows odpoví:

```
The term 'bash' is not recognized as the name of a cmdlet...
```

To není chyba bota. Znamená to jen „tohle patří jinam".

---

## Krok 1 — Otevři si konzoli serveru

Nejjednodušší cesta, nic se nenastavuje:

1. Otevři v prohlížeči **[cloud.digitalocean.com](https://cloud.digitalocean.com)** a přihlas se.
2. V levém menu klikni na **Droplets**.
3. Klikni na **název svého dropletu** (ten, na kterém bot běží).
4. Vpravo nahoře je tlačítko **Console**. Klikni na něj.
5. Otevře se nové okno s černým terminálem. Chvíli to trvá, než naskočí.

Poznáš, že jsi na správném místě, podle řádku, který končí `#` a začíná
`root@` — třeba:

```
root@slevy-bot:~#
```

**Tenhle řádek je znamení, že jsi na serveru.** Když před kurzorem vidíš
`PS C:\WINDOWS\System32>`, jsi pořád na notebooku a musíš se vrátit ke kroku 1.

> V konzoli DigitalOcean nefunguje `Ctrl+V`. Text se vkládá přes ikonu
> schránky vpravo nahoře, nebo se dá prostě opsat — příkazy jsou krátké.

---

## Krok 2 — Stáhni novou verzi

Do konzole napiš (nebo vlož) tenhle jediný příkaz a stiskni Enter:

```bash
bash /opt/slevy-bot/deploy/install.sh
```

Poběží to zhruba minutu. Uvidíš modré řádky začínající `==>`, které říkají, co
se právě děje — stahuje se kód z GitHubu, doinstalují se knihovny, načtou se
systémové služby.

**Skript nikdy nesahá na tvoje tokeny ani na databázi s cenovou historií.**
Můžeš ho pouštět kolikrát chceš, nic se tím nerozbije.

Na konci vypíše přehled. Když nikde nesvítí červené `chyba` nebo `error`, jsi
hotový.

---

## Krok 3 — Ověř, že to jede

```bash
systemctl list-timers 'slevy-*' --no-pager
```

Mají se vypsat **tři řádky** a u každého čas, kdy poběží příště:

```
NEXT                        LEFT     UNIT                ACTIVATES
Mon 2026-07-27 21:20:00 CEST 3min    slevy-travel.timer  slevy-travel.service
Mon 2026-07-27 21:43:00 CEST 26min   slevy-scan.timer    slevy-scan.service
Tue 2026-07-28 19:09:00 CEST 21h     slevy-digest.timer  slevy-digest.service
```

Co které je:

- **slevy-travel** — letenky a hotely, každých 10 minut
- **slevy-scan** — celý katalog klíčů a předplatného, v :13 a :43
- **slevy-digest** — večerní souhrn v 19:09

Když vidíš tři rozumné časy, hotovo. Můžeš okno zavřít, bot běží sám.

---

## Krok 4 (nepovinný) — Nečekat a spustit hned

Chceš vidět, jestli nová verze funguje, a nečekat na další běh:

```bash
systemctl start slevy-travel && journalctl -u slevy-travel -n 30 --no-pager
```

Vypíše se log posledního běhu. Hledej řádek podobný tomuhle:

```
INFO  slevy: Odesláno 0 okamžitých upozornění, ve frontě souhrnu 12 položek
```

**Nula odeslaných není chyba.** Znamená to, že všechno, co by stálo za zprávu,
už ti bot poslal dřív a správně mlčí.

Totéž pro hlavní sken (trvá zhruba minutu, prochází 5 000 položek):

```bash
systemctl start slevy-scan && journalctl -u slevy-scan -n 30 --no-pager
```

A když chceš vidět večerní souhrn hned:

```bash
systemctl start slevy-digest
```

---

## Když se něco pokazí

**„The term 'bash' is not recognized"**

Jsi v PowerShellu na notebooku. Vrať se ke kroku 1.

**„No such file or directory: /opt/slevy-bot/deploy/install.sh"**

Bot na tomhle serveru ještě není nainstalovaný, nebo jsi na jiném dropletu.
Použij `NAVOD.md` a udělej první instalaci.

**Skript skončil chybou**

Zkopíruj posledních pár řádků a pošli mi je. Nebo se podívej, co říká služba:

```bash
systemctl status slevy-scan --no-pager
journalctl -u slevy-scan -n 60 --no-pager
```

**Chodí moc zpráv, nebo naopak žádné**

Prahy se ladí **doma v repozitáři**, ne na serveru — `install.sh` dělá
`git reset --hard`, takže by se úprava provedená na serveru při další
aktualizaci ztratila. Uprav `config.yaml` doma, `git push`, a na serveru zase
krok 2.

---

## Celý postup na jednom místě

Když už tomu rozumíš, je to tohle:

**Doma v PowerShellu** (ve složce projektu):

```bash
.venv/Scripts/python -m pytest
```

```bash
git commit -am "popis zmeny" && git push
```

**Na serveru v konzoli dropletu:**

```bash
bash /opt/slevy-bot/deploy/install.sh
```

Restart služeb řešit nemusíš. Každý běh časovače startuje nový proces, takže
další sken jede automaticky už na nové verzi.
