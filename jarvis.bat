@echo off
rem Opens Jay (same as the desktop icon). Options pass through, e.g.
rem   jarvis.bat --standby          start hidden, just listening for "Hey Jay"
rem   jarvis.bat --no-voice         typing only
rem   jarvis.bat --console          use the terminal instead of the window
cd /d "%~dp0"
echo %* | find "--console" >nul
if %errorlevel%==0 (
    ".venv\Scripts\python.exe" -m jarvis %*
) else (
    start "" ".venv\Scripts\pythonw.exe" -m jarvis %*
)
