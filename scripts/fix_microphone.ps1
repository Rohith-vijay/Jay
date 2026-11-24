# Right-click this file > "Run with PowerShell" as administrator (or run it from an admin terminal).
# It restarts the Windows audio services, which fixes a microphone that delivers only silence.
$ErrorActionPreference = "Continue"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this as administrator (right-click PowerShell > Run as administrator)." -ForegroundColor Yellow
    exit 1
}
Write-Host "Restarting Windows audio..."
Restart-Service -Name AudioEndpointBuilder -Force
Restart-Service -Name Audiosrv -Force
Start-Sleep 3
Get-Service Audiosrv, AudioEndpointBuilder | Format-Table Name, Status -AutoSize
Write-Host "Done. Now say something near the laptop: Settings > System > Sound > Input should show the bar moving."
Write-Host "If it is still flat, check the microphone mute key, AMD Software > noise suppression, and close apps that hold the microphone."
