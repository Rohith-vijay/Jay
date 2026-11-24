# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\set_telegram_token.ps1
# Saves your Telegram bot token in Jay's .env (typing is hidden) and checks it with Telegram.
# The token must be the WHOLE thing BotFather gives you, like  1234567890:AAF...  (a number, a colon, then letters).
param([string]$EnvPath = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env"), [string]$Token = "")

if (-not $Token) {
    $secure = Read-Host "Paste the full bot token from @BotFather, then press Enter" -AsSecureString
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$Token = $Token.Trim()
if ($Token -notmatch '^\d{6,12}:[A-Za-z0-9_-]{30,50}$') {
    Write-Host "That is not a full bot token. It must look like 1234567890:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx (a number, a colon, then the long part). Your current one is missing the number and colon at the front. Nothing was changed." -ForegroundColor Yellow
    exit 1
}
try {
    $me = Invoke-RestMethod -Uri "https://api.telegram.org/bot$Token/getMe" -TimeoutSec 20
    Write-Host "Telegram accepted it: your bot is @$($me.result.username)" -ForegroundColor Green
} catch {
    Write-Host "Telegram did not accept that token ($($_.Exception.Message)). Nothing was changed." -ForegroundColor Yellow
    exit 1
}
$lines = @()
if (Test-Path $EnvPath) { $lines = @(Get-Content $EnvPath -Encoding UTF8 | Where-Object { $_ -notmatch '^\s*TELEGRAM_BOT_TOKEN=' }) }
$lines += "TELEGRAM_BOT_TOKEN=$Token"
Set-Content -Path $EnvPath -Value $lines -Encoding UTF8
Write-Host "Saved. Restart Jay, open its window, and send  /pair <the code it shows>  to @$($me.result.username) from your phone."
