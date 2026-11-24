# Run this in PowerShell (no administrator needed):  powershell -ExecutionPolicy Bypass -File scripts\set_claude_key.ps1
# It asks for your Anthropic API key (typing is hidden), saves it in Jay's .env, and makes the fast
# Claude model Jay's first brain, with Gemini and Groq as the fallbacks. The key stays on this laptop.
param(
    [string]$EnvPath = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env"),
    [string]$Key = ""
)

if (-not $Key) {
    $secure = Read-Host "Paste your Anthropic API key (starts with sk-ant-), then press Enter" -AsSecureString
    $Key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$Key = $Key.Trim()
if ($Key -notmatch '^sk-ant-[A-Za-z0-9_\-]{20,}$') {
    Write-Host "That doesn't look like an Anthropic API key (it should start with sk-ant-). Nothing was changed." -ForegroundColor Yellow
    exit 1
}

$settings = [ordered]@{
    "ANTHROPIC_API_KEY"        = $Key
    "JARVIS_CLAUDE_MODEL"      = "claude-haiku-5-5"
    "JARVIS_PROVIDER"          = "claude"
    "JARVIS_FALLBACK_ORDER"    = "claude,gemini,groq,ollama"
}
$lines = @()
if (Test-Path $EnvPath) { $lines = @(Get-Content $EnvPath -Encoding UTF8) }
foreach ($name in $settings.Keys) {
    $lines = @($lines | Where-Object { $_ -notmatch "^\s*$name=" })
    $lines += "$name=$($settings[$name])"
}
Set-Content -Path $EnvPath -Value $lines -Encoding UTF8
Write-Host "Saved. Jay will use Claude first (claude-haiku-5-5) after it restarts." -ForegroundColor Green
Write-Host "Tip: in console.anthropic.com > Settings > Limits, set a monthly spend limit so the bill can never surprise you."
