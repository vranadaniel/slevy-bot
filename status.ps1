<#
.SYNOPSIS
    Zobrazení stavu slevy-bot časovačů a služeb na VPS.
#>

param(
    [string]$VpsHost = "68.183.26.214",
    [string]$SshKey = "$HOME\.ssh\eventtrader_do"
)

ssh -i $SshKey -o BatchMode=yes "root@$VpsHost" @"
echo '=== Stav časovačů (timers) ==='
systemctl list-timers 'slevy-*' --no-pager
echo ''
echo '=== Stav posledních běhů služeb ==='
systemctl status 'slevy-*.service' --no-pager --lines=3
"@
