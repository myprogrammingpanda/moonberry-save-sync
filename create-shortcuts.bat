@echo off
REM Creates Desktop and/or Start Menu shortcuts for Moonberry Save-Sync
REM (a small window lets you pick which). Safe to run again any time,
REM e.g. after moving the app folder -- it just replaces the old ones.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\shortcuts.ps1"
