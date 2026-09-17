@echo off
chcp 65001 >nul
cd /d "%~dp0"
python PATCH_APP.py
echo.
pause
