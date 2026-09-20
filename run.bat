@echo off
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo The app exited with an error -- see the message above.
    pause
)
