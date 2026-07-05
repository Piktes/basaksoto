@echo off
chcp 65001 >nul
title Basaksoto Bot
cd /d "%~dp0"
echo Bot baslatiliyor... Bu pencere acik kaldigi surece bot calisir.
echo Durdurmak icin Ctrl+C veya pencereyi kapatin.
echo.
py -m bot
pause
