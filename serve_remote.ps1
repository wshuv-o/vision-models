# Start all three pages so another machine on the network can use them.
#
#   .\serve_remote.ps1 -Password "something-long"
#
# The pages have no login of their own, so a password is required here rather
# than optional: they accept uploads and serve results back.
#
# Needs the firewall opened once, from an ADMIN PowerShell:
#   New-NetFirewallRule -DisplayName "vision-models UIs" -Direction Inbound `
#     -Action Allow -Protocol TCP -LocalPort 7860-7862 -Profile Private
param(
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$User = "esme",
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "python not found at $py" }

$env:VM_HOST = $BindHost
$env:VM_USER = $User
$env:VM_PASS = $Password

$apps = @(
    @{ Script = "app.py";          Port = 7860; Name = "OCR (PDF -> text/tables)" },
    @{ Script = "extract_ui.py";   Port = 7861; Name = "Extraction (pages -> rows)" },
    @{ Script = "appraisal_ui.py"; Port = 7862; Name = "Appraisal fields (PDFs -> one row each)" }
)

foreach ($a in $apps) {
    Get-NetTCPConnection -LocalPort $a.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Process -FilePath $py -ArgumentList "-u", $a.Script `
        -WorkingDirectory $root -WindowStyle Hidden
}

# app.py imports torch, which takes a good deal longer than the others, so wait
# for the ports rather than guessing at a sleep.
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    $up = @($apps | Where-Object {
        Get-NetTCPConnection -LocalPort $_.Port -State Listen -ErrorAction SilentlyContinue
    }).Count
    if ($up -eq $apps.Count) { break }
    Start-Sleep -Seconds 2
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notlike "127.*" -and
                      $_.IPAddress -notlike "169.254.*" -and
                      $_.InterfaceAlias -notlike "*WSL*" } |
       Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "Sign in as '$User' with the password you passed." -ForegroundColor Yellow
Write-Host ""
foreach ($a in $apps) {
    $listening = Get-NetTCPConnection -LocalPort $a.Port -State Listen -ErrorAction SilentlyContinue
    $mark = if ($listening) { "up  " } else { "DOWN" }
    Write-Host ("  [{0}] http://{1}:{2}  {3}" -f $mark, $ip, $a.Port, $a.Name)
}
Write-Host ""
Write-Host "Run one job at a time: the pages share a single 16GB GPU, and the" -ForegroundColor DarkGray
Write-Host "extraction pages shut down the OCR server to claim it." -ForegroundColor DarkGray
