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
    "/baslat — Drive'ı tara, yeni klasörden video yükle\n"
    "/istatistik — haftalık/aylık yükleme raporu\n"
    "/gorseller — kayıtlı görsel kütüphanesini yönet\n"
    "📷 Bota fotoğraf gönder → kütüphaneye veya thumbnail olarak kaydet\n"
    "/kanallar — YouTube kanal adlarını yönet\n"
    "/guncelle — botu en son sürüme güncelle ve yeniden başlat\n"
    "/yardim — bu mesaj"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_HELP_TEXT)


@router.message(Command("yardim", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP_TEXT)
