@echo off
chcp 65001 >nul
title Basaksoto Bot - Guncelleme
cd /d "%~dp0"
echo ============================================
echo   Basaksoto Bot - Guncelleme
echo ============================================
echo.

REM --- git kontrolu (PATH'te yoksa standart kurulum yollarini dene)
set "GIT=git"
git --version >nul 2>nul
if errorlevel 1 (
    if exist "%ProgramFiles%\Git\cmd\git.exe" (
        set "GIT=%ProgramFiles%\Git\cmd\git.exe"
    ) else if exist "%LocalAppData%\Programs\Git\cmd\git.exe" (
        set "GIT=%LocalAppData%\Programs\Git\cmd\git.exe"
    ) else (
        echo [HATA] git bulunamadi. Once sunu calistirin:
        echo    winget install --id Git.Git -e
        echo Sonra ACIK TUM PENCERELERI KAPATIP guncelle.bat'i yeniden calistirin.
        pause
        exit /b 1
    )
)

REM --- klasor git'e bagli degilse (ZIP kurulumu) bir kereligine bagla
if not exist .git (
    echo Klasor git'e baglaniyor ^(ilk seferlik^)...
    "%GIT%" init -b main
    "%GIT%" remote add origin https://github.com/Piktes/basaksoto.git
    "%GIT%" fetch origin
    if errorlevel 1 (
        echo [HATA] GitHub'a erisilemedi. Internet baglantisini kontrol edin.
        pause
        exit /b 1
    )
    "%GIT%" reset --hard origin/main
    "%GIT%" branch --set-upstream-to=origin/main main >nul 2>nul
    echo Baglandi ve en son surume esitlendi.
) else (
    echo Guncellemeler cekiliyor...
    "%GIT%" pull
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
