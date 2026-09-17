@echo off
chcp 65001 >nul
cd /d "%~dp0"
python PATCH_PREMIUM_QUALITY.py
echo.
pause
