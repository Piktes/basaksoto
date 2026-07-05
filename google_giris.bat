@echo off
chcp 65001 >nul
title Basaksoto Bot - Google Oturum Kurulumu
cd /d "%~dp0"

set "PYCMD="
py --version >nul 2>nul && set "PYCMD=py"
if not defined PYCMD python --version >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
    echo [HATA] Python bulunamadi. Once kurulum.bat calistirin.
    pause
    exit /b 1
)

echo Chrome penceresi acilacak - Google hesabiniza giris yapin,
echo studio.youtube.com acilinca buraya donup Enter'a basin.
echo.
%PYCMD% -m bot --login-setup
pause
