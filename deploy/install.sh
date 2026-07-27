#!/usr/bin/env bash
#
# Nasazení bota na Debian/Ubuntu server.
#
#   curl -fsSL https://raw.githubusercontent.com/vranadaniel/slevy-bot/main/deploy/install.sh | sudo bash
#
# Skript je idempotentní — dá se pustit znovu kvůli aktualizaci. Tokeny nikdy
# nepřepisuje; ty patří do /etc/slevy-bot/env, který si vyplníš sám.

set -euo pipefail

REPO="https://github.com/vranadaniel/slevy-bot.git"
APP_DIR="/opt/slevy-bot"
ENV_DIR="/etc/slevy-bot"
ENV_FILE="$ENV_DIR/env"
SERVICE_USER="slevy"
TIMEZONE="Europe/Prague"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
    echo "Spusť jako root: sudo bash install.sh" >&2
    exit 1
fi

log "Instaluji závislosti"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

log "Nastavuji časové pásmo na $TIMEZONE"
# Timery běží v místním čase, takže se souhrn sám posune s letním časem.
timedatectl set-timezone "$TIMEZONE" || warn "Časové pásmo se nepodařilo nastavit"

log "Zakládám systémového uživatele $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

log "Stahuji kód do $APP_DIR"
# Git od 2.35.2 odmítne pracovat s repozitářem, který patří jinému uživateli
# ("dubious ownership"). Starší verze skriptu předávala celý adresář uživateli
# slevy, takže root na něj při aktualizaci narazí. Výjimku zapisujeme jednou;
# `--add` bez téhle kontroly by přidával duplicitní řádky při každém běhu.
if ! git config --system --get-all safe.directory 2>/dev/null | grep -qx "$APP_DIR"; then
    git config --system --add safe.directory "$APP_DIR"
fi

if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch --quiet origin
    git -C "$APP_DIR" reset --quiet --hard origin/main
else
    git clone --quiet "$REPO" "$APP_DIR"
fi

log "Připravuji virtuální prostředí"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- převzetí stavu z GitHubu -------------------------------------------------
# Na větvi data leží databáze, kterou nasbíraly běhy v GitHub Actions: cenová
# historie, cache ITAD a hlavně záznam už odeslaných upozornění. Bez ní by
# deduplikace začínala od nuly a přišla by záplava opakovaných zpráv.
mkdir -p "$APP_DIR/data"
if [[ ! -f "$APP_DIR/data/deals.db" ]]; then
    log "Přebírám stav z větve data"
    if git -C "$APP_DIR" fetch --quiet origin data:refs/remotes/origin/data 2>/dev/null &&
       git -C "$APP_DIR" show origin/data:data/deals.db > "$APP_DIR/data/deals.db" 2>/dev/null; then
        echo "    převzato $(du -h "$APP_DIR/data/deals.db" | cut -f1)"
    else
        rm -f "$APP_DIR/data/deals.db"
        warn "Větev data nenalezena — začínáme s prázdnou databází."
        warn "Po první instalaci spusť bootstrap, viz výpis na konci."
    fi
fi

log "Nastavuji práva"
# Kód zůstává rootovi, službě stačí číst — nemá důvod přepisovat si vlastní
# program a git pak nenaráží na cizí vlastnictví. Zapisovat se musí jen do
# adresáře s databází.
chown -R root:root "$APP_DIR"
chmod -R a+rX "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"

log "Připravuji soubor s tokeny"
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$APP_DIR/deploy/env.example" "$ENV_FILE"
    NEEDS_TOKENS=1
else
    echo "    $ENV_FILE už existuje, nechávám beze změny"
    NEEDS_TOKENS=0
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

log "Instaluji systemd jednotky"
cp "$APP_DIR"/deploy/slevy-*.service "$APP_DIR"/deploy/slevy-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now slevy-scan.timer slevy-travel.timer slevy-digest.timer

log "Hotovo"
systemctl list-timers 'slevy-*' --no-pager || true

cat <<EOF

------------------------------------------------------------------
Co zbývá udělat:

EOF

if [[ "$NEEDS_TOKENS" == "1" ]]; then
cat <<EOF
1) Vyplň tokeny:
     sudo nano $ENV_FILE

2) Ověř, že Telegram funguje:
     sudo -u $SERVICE_USER env \$(grep -v '^#' $ENV_FILE | xargs) \\
       $APP_DIR/.venv/bin/python -m src.main --test-telegram

EOF
fi

cat <<EOF
Užitečné příkazy:

  systemctl list-timers 'slevy-*'      kdy poběží příště
  journalctl -u slevy-scan -n 50       log posledního skenu
  journalctl -u slevy-scan -f          sledovat živě
  systemctl start slevy-scan           spustit sken hned
  systemctl start slevy-digest         poslat souhrn hned

Aktualizace na novou verzi:

  sudo bash $APP_DIR/deploy/install.sh

------------------------------------------------------------------
EOF
