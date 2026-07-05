@echo off
chcp 65001 >nul
title Basaksoto Bot - Google Oturum Kurulumu
cd /d "%~dp0"
echo Chrome penceresi acilacak - Google hesabiniza giris yapin,
echo studio.youtube.com acilinca buraya donup Enter'a basin.
echo.
py -m bot --login-setup
pause
