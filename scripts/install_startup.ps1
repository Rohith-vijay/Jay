# Puts Jay on your desktop and makes it start (hidden, listening for "Hey Jay") when you sign in.
#   Install:        powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
#   Desktop only:   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -NoStartup
#   Remove both:    powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove
param([switch]$Remove, [switch]$NoStartup)

$root = Split-Path $PSScriptRoot -Parent
$desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jay.lnk'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Jay.lnk'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Jay.lnk'

if ($Remove) {
    foreach ($link in $desktop, $startup, $startMenu) {
        if (Test-Path $link) { Remove-Item $link; "Removed $link" }
    }
    return
}

function New-JayShortcut($path, $arguments, $description) {
    # The shortcut writer can't save into folders with non-English names (e.g. a OneDrive
    # Desktop), so build it in the temp folder and move it into place.
    $temp = Join-Path $env:TEMP "Jay-$([guid]::NewGuid().ToString('N')).lnk"
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($temp)
    $link.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe'    # no terminal window
    $link.Arguments = "-m jarvis $arguments".Trim()
    $link.WorkingDirectory = $root
    $link.IconLocation = Join-Path $root 'assets\jay.ico'
    $link.Description = $description
    $link.Save()
    Move-Item -LiteralPath $temp -Destination $path -Force
    if (Test-Path -LiteralPath $path) { "Created $path" } else { "FAILED to create $path" }
}

New-JayShortcut $desktop '' 'Open Jay'
New-JayShortcut $startMenu '' 'Open Jay'     # so Jay shows up when you search the Start menu
if (-not $NoStartup) { New-JayShortcut $startup '--standby' 'Jay, listening for "Hey Jay"' }
