"""/start ve /yardim komutları."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="start")

_HELP_TEXT = (
    "👋 <b>Drive → YouTube Yükleme Botu</b>\n\n"
    "Drive'daki şarkı klasörlerini tarar, onayınla videoya çevirir ve "
    "YouTube Studio üzerinden Herkese Açık yayınlar.\n\n"
    "<b>Komutlar:</b>\n"
    "/baslat — Drive'ı tara; tek tek ya da toplu (sıralı) video yükle\n"
    "/istatistik — haftalık/aylık yükleme raporu\n"
    "/gorseller — kayıtlı görsel kütüphanesini yönet\n"
    "📷 Bota fotoğraf gönder → kütüphaneye veya thumbnail olarak kaydet\n"
    "/kanallar — YouTube kanallarını yönet (otomatik bulma dahil)\n"
    "/giris — Google oturumu düştüyse uzaktan yeniden giriş\n"
    "/yedek — veritabanı yedeğini gönder (her gece otomatik de gelir)\n"
    "💾 Yedek dosyasını (.db) bota gönder → geri yükleme\n"
    "/temizle — yüklenmiş klasörleri Drive çöp kutusuna taşı\n"
    "/durdur — botu geçici olarak durdurur (yeni yüklemeleri engeller)\n"
    "/restart — botu aktif hale getirir ve yeniden başlatır\n"
    "/yardim — bu mesaj"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_HELP_TEXT)


@router.message(Command("yardim", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP_TEXT)


@router.message(Command("durdur"))
async def cmd_durdur(message: Message) -> None:
    from ..services import db
    db.set_setting("bot_status", "paused")
    await message.answer(
        "⏸️ <b>Bot geçici olarak durduruldu.</b>\n"
        "Yeni yüklemeler engellenmiştir. Botu tekrar aktifleştirmek ve yeniden başlatmak için /restart komutunu kullanabilirsiniz."
    )


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    import sys
    from ..services import db
    db.set_setting("bot_status", "running")
    await message.answer("🔄 <b>Bot aktifleştiriliyor ve yeniden başlatılıyor...</b>\nLütfen bekleyin...")
    sys.exit(1)
