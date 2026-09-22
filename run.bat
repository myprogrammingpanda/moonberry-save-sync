@echo off
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
    powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Python was not found on this computer.'+[Environment]::NewLine+[Environment]::NewLine+'Click OK to open the Python download page. During setup, make sure to check ''Add python.exe to PATH'' on the installer''s first screen -- then run this again.', 'Moonberry Save-Sync', 'OK', 'Warning') | Out-Null"
    start "" "https://www.python.org/downloads/"
    exit /b 1
)

start "" pythonw main.py
