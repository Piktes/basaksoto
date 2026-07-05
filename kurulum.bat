@echo off
chcp 65001 >nul
title Basaksoto Bot - Kurulum
echo ============================================
echo   Basaksoto Bot - Otomatik Kurulum
echo ============================================
echo.

cd /d "%~dp0"

REM --- Python komutunu bul (py veya python)
set "PYCMD="
py --version >nul 2>nul && set "PYCMD=py"
if not defined PYCMD python --version >nul 2>nul && set "PYCMD=python"

if not defined PYCMD (
    echo [HATA] Python bulunamadi. Simdi winget ile kuruluyor...
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo winget ile kurulamadi. Elle kurun:
        echo   https://www.python.org/downloads/  adresinden indirin,
        echo   kurulumda "Add python.exe to PATH" kutusunu ISARETLEYIN.
        pause
        exit /b 1
    )
    echo.
    echo ============================================
    echo  Python kuruldu! Bu pencereyi KAPATIP
    echo  kurulum.bat dosyasini YENIDEN calistirin.
    echo ============================================
    pause
    exit /b 0
)

echo Python bulundu: %PYCMD%
%PYCMD% --version
echo.

echo [1/5] Python paketleri kuruluyor...
%PYCMD% -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [HATA] pip kurulumu basarisiz oldu.
    pause
    exit /b 1
)

echo.
echo [2/5] Playwright Chromium tarayicisi indiriliyor...
%PYCMD% -m playwright install chromium
if errorlevel 1 (
    echo [HATA] Chromium indirilemedi.
    pause
    exit /b 1
)

echo.
echo [3/5] ffmpeg kontrol ediliyor...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo ffmpeg bulunamadi, winget ile kuruluyor...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo NOT: ffmpeg PATH'e eklendi - botu baslatmadan once bu pencereyi kapatin.
) else (
    echo ffmpeg kurulu.
)

echo.
echo [4/5] .env dosyasi hazirlaniyor...
if not exist .env (
    copy .env.example .env >nul
    echo .env olusturuldu - LUTFEN DUZENLEYIN: bot token + Telegram ID'ler.
) else (
    echo .env zaten var, dokunulmadi.
)

echo.
echo [5/5] Klasorler olusturuluyor...
if not exist credentials mkdir credentials
if not exist data\images mkdir data\images

echo.
echo ============================================
echo   KURULUM TAMAM! Simdi sirasiyla:
echo ============================================
echo  1. .env dosyasini duzenleyin (bot token, Telegram ID'ler)
echo  2. credentials\client_secret.json dosyasini koyun
echo     (Google Cloud - Drive API - Desktop app OAuth istemcisi)
echo  3. google_giris.bat calistirin (Google/YouTube oturumu icin)
echo  4. bot_baslat.bat ile botu baslatin
echo.
pause
