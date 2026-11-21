# Run as administrator: allow inbound TCP 1935 used by the phone stream.
$ruleName = "CrDeckInspector RTMP 1935"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please right-click this script and choose 'Run as administrator'." -ForegroundColor Yellow
    pause
    exit 1
}

if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Host "Firewall rule already exists: $ruleName" -ForegroundColor Green
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort 1935 -Profile Any | Out-Null
    Write-Host "Allowed inbound TCP 1935 ($ruleName)" -ForegroundColor Green
}

pause
