<#
.SYNOPSIS
    Zobrazení logů slevy-bot z VPS.
#>

param(
    [string]$VpsHost = "68.183.26.214",
    [string]$SshKey = "$HOME\.ssh\eventtrader_do",
    [string]$Unit = "slevy-watch",
    [int]$Lines = 50,
    [switch]$Follow
)

$followFlag = if ($Follow) { "-f" } else { "" }
ssh -i $SshKey -o BatchMode=yes "root@$VpsHost" "journalctl -u $Unit -n $Lines $followFlag --no-pager"
