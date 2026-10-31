<#
.SYNOPSIS
    Nasazení aktuální verze slevy-bot na VPS.
.DESCRIPTION
    1. Zkontroluje stav gitu na lokálním PC.
    2. Odešle commity na GitHub (git push origin main).
    3. Připojí se přes SSH na VPS.
    4. Spustí aktualizační skript na VPS (bash /opt/slevy-bot/deploy/install.sh).
    5. Zobrazí stav časovačů (systemctl list-timers 'slevy-*').
#>

param(
    [string]$VpsHost = "68.183.26.214",
    [string]$SshKey = "$HOME\.ssh\eventtrader_do"
)

Write-Host "=== 1. Kontrola gitu na lokálním PC ===" -ForegroundColor Cyan
$status = git status --porcelain
if ($status) {
    Write-Warning "Pozor: Máte lokálně necommitnuté změny. Tyto změny se na server nedostanou:"
    git status -s
    $confirm = Read-Host "Chcete pokračovat i tak? (a/n)"
    if ($confirm -ne 'a') {
        Write-Host "Nasazení přerušeno." -ForegroundColor Yellow
        exit
    }
}

$unpushed = git log origin/main..main --oneline
if ($unpushed) {
    Write-Host "Odesílám lokální commity na GitHub..." -ForegroundColor Yellow
    git push origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Chyba při odesílání na GitHub."
        exit $LASTEXITCODE
    }
}

Write-Host "`n=== 2. Aktualizace na VPS ($VpsHost) ===" -ForegroundColor Cyan
$remoteCmd = @"
set -e
echo '[VPS] Spouštím aktualizaci slevy-bot...'
bash /opt/slevy-bot/deploy/install.sh
echo ''
echo '[VPS] Aktuální stav časovačů:'
systemctl list-timers 'slevy-*' --no-pager
"@

ssh -i $SshKey -o BatchMode=yes "root@$VpsHost" $remoteCmd

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n Deployment na VPS proběhl úspěšně!" -ForegroundColor Green
} else {
    Write-Host "`n Chyba při deploymentu na VPS." -ForegroundColor Red
}
