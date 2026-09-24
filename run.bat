@echo off
setlocal
cd /d "%~dp0"
title Moonberry Save-Sync

REM Launches the app. On the very first run (or whenever a package from
REM requirements.txt is missing) it installs them first, in this console
REM window so the progress is visible. Normal launches just do a quick
REM check and hand off to pythonw, so no console stays open.

REM --- Opened straight from inside the zip? ------------------------------
REM Double-clicking run.bat inside a zip in File Explorer extracts only
REM run.bat itself to a temp folder, so the rest of the app isn't here.
if not exist "%~dp0main.py" goto :not_extracted

REM --- Find Python ---------------------------------------------------------
REM Use pythonw on PATH, else ask the "py" launcher (installed by default
REM by python.org's installer, even when "Add to PATH" was left unticked).
REM Plain "python" is deliberately NOT used to find it: Windows ships fake
REM python.exe "App Execution Alias" stubs that just open the Microsoft
REM Store, and they're on PATH even when Python isn't installed.
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if defined PYW goto :have_pythonw

set "PYEXE="
for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE goto :no_python
for %%i in ("%PYEXE%") do set "PYW=%%~dpipythonw.exe"
if not exist "%PYW%" set "PYW=%PYEXE%"

:have_pythonw
REM Console python.exe from the SAME install as pythonw, for the checks
REM and pip (so their output shows up and packages land in the right
REM Python).
for %%i in ("%PYW%") do set "PY=%%~dpipython.exe"
if not exist "%PY%" set "PY=%PYW%"

REM --- Check the environment ----------------------------------------------
"%PY%" "%~dp0scripts\check_setup.py"
if errorlevel 13 goto :launch
if errorlevel 12 goto :not_extracted
if errorlevel 11 goto :python_too_old
if errorlevel 10 goto :install
goto :launch

REM --- First-time setup -------------------------------------------------
:install
echo.
echo  First-time setup: installing required packages.
echo  This takes a minute or two -- please leave this window open.
echo.
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo  pip isn't installed yet -- setting it up first...
    "%PY%" -m ensurepip --upgrade
)
"%PY%" -m pip install --disable-pip-version-check --no-warn-script-location -r "%~dp0requirements.txt"
if errorlevel 1 goto :install_failed
"%PY%" "%~dp0scripts\check_setup.py" >nul
if errorlevel 1 goto :install_failed
echo.
echo  Setup complete.

REM Offer Desktop / Start Menu shortcuts, once ever (the marker file keeps
REM it from asking again; create-shortcuts.bat can make them any time).
if exist "%~dp0.shortcuts-offered" goto :launch
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\shortcuts.ps1" -FirstRun
type nul > "%~dp0.shortcuts-offered"
attrib +h "%~dp0.shortcuts-offered" >nul 2>nul

:launch
start "" "%PYW%" "%~dp0main.py"
exit /b 0

REM --- Problems ---------------------------------------------------------
:not_extracted
set "MB_TEXT=It looks like Moonberry Save-Sync is being run straight from inside the zip file.||Right-click the zip, choose 'Extract All...', pick a folder you'll keep (e.g. Documents), then double-click run.bat in the extracted folder."
call :msgbox Warning
exit /b 1

:no_python
set "MB_TEXT=Python was not found on this computer.||Click OK to open the Python download page. During setup, make sure to check 'Add python.exe to PATH' on the installer's first screen -- then run this again."
call :msgbox Warning
start "" "https://www.python.org/downloads/"
exit /b 1

:python_too_old
set "MB_TEXT=Your Python is too old for Moonberry Save-Sync -- it needs Python 3.11 or newer.||Click OK to open the Python download page. Install the latest version (check 'Add python.exe to PATH' on the installer's first screen), then run this again."
call :msgbox Warning
start "" "https://www.python.org/downloads/"
exit /b 1

:install_failed
echo.
echo  Setup didn't finish -- see the messages above.
set "MB_TEXT=Couldn't install the packages Moonberry Save-Sync needs.||Check your internet connection, then double-click run.bat to try again. The console window behind this message shows what went wrong."
call :msgbox Error
echo.
pause
exit /b 1

REM Shows %MB_TEXT% in a message box ("|" = line break).
REM %1 = icon: Warning, Error or Information.
:msgbox
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show(($env:MB_TEXT -replace '\|', [Environment]::NewLine),'Moonberry Save-Sync', 'OK', '%~1') | Out-Null"
exit /b 0
