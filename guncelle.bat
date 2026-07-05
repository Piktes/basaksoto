@echo off
chcp 65001 >nul
title Basaksoto Bot - Guncelleme
cd /d "%~dp0"
echo ============================================
echo   Basaksoto Bot - Guncelleme
echo ============================================
echo.

REM --- git kontrolu
git --version >nul 2>nul
if errorlevel 1 (
    echo [HATA] git bulunamadi. Once sunu calistirin:
    echo    winget install --id Git.Git -e
    echo Sonra bu pencereyi kapatip guncelle.bat'i yeniden calistirin.
    pause
    exit /b 1
)

REM --- klasor git'e bagli degilse (ZIP kurulumu) bir kereligine bagla
if not exist .git (
    echo Klasor git'e baglaniyor ^(ilk seferlik^)...
    git init -b main
    git remote add origin https://github.com/Piktes/basaksoto.git
    git fetch origin
    if errorlevel 1 (
        echo [HATA] GitHub'a erisilemedi. Internet baglantisini kontrol edin.
        pause
        exit /b 1
    )
    git reset --hard origin/main
    git branch --set-upstream-to=origin/main main >nul 2>nul
    echo Baglandi ve en son surume esitlendi.
) else (
    echo Guncellemeler cekiliyor...
    git pull
    if errorlevel 1 (
        echo [HATA] Guncelleme basarisiz. Internet baglantisini kontrol edin.
        pause
        exit /b 1
    )
)

echo.
echo ============================================
echo   GUNCELLEME TAMAM!
echo   Bot aciksa kapatip bot_baslat.bat ile
echo   yeniden baslatin (yeni kod oyle devreye girer).
echo ============================================
echo.
pause
