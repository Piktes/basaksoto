@echo off
chcp 65001 >nul
title Basaksoto Bot
cd /d "%~dp0"

set "PYCMD="
py --version >nul 2>nul && set "PYCMD=py"
if not defined PYCMD python --version >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
    echo [HATA] Python bulunamadi. Once kurulum.bat calistirin.
    pause
    exit /b 1
)

echo Bot baslatiliyor... Bu pencere acik kaldigi surece bot calisir.
echo Durdurmak icin Ctrl+C veya pencereyi kapatin.
echo.
%PYCMD% -m bot
pause
