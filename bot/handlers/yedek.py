"""/yedek — veritabanı yedeği alma ve Telegram'dan geri yükleme."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import get_config
from ..services import backup, db
from ..services.studio_uploader import UPLOAD_LOCK
from .common import esc

logger = logging.getLogger(__name__)

router = Router(name="yedek")

_MAX_BACKUP_BYTES = 20 * 1024 * 1024  # Telegram bot indirme sınırı

# Geri yükleme onayı bekleyen dosyalar: user_id → geçici dosya yolu
_PENDING_RESTORES: dict[int, Path] = {}


@router.message(Command("yedek"))
async def cmd_yedek(message: Message, bot: Bot) -> None:
    try:
        await backup.send_backup(bot, message.chat.id, get_config())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Yedek gönderilemedi.")
        await message.answer(f"❌ Yedek alınamadı: {esc(str(exc))}")


@router.message(StateFilter(None), F.document.file_name.lower().endswith(".db"))
async def msg_backup_file(message: Message, bot: Bot) -> None:
    """Bota gönderilen .db dosyası: doğrula, geri yükleme onayı iste."""
    document = message.document
    if document.file_size and document.file_size > _MAX_BACKUP_BYTES:
        await message.answer("❌ Dosya çok büyük; bot yedeği olamaz.")
        return

    cfg = get_config()
    temp_path = cfg.temp_dir / f"restore_{message.from_user.id}.db"
    await bot.download(document, destination=str(temp_path))
    try:
        incoming = await asyncio.to_thread(db.inspect_backup, temp_path)
    except ValueError as exc:
        temp_path.unlink(missing_ok=True)
        await message.answer(f"❌ {esc(str(exc))}")
        return

    current = {
        "uploads": db.upload_stats()["total"],
        "folders": len(db.get_active_folders()),
        "channels": len(db.list_channels()),
    }
    _PENDING_RESTORES[message.from_user.id] = temp_path
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♻️ Evet, geri yükle", callback_data="restore:yes"),
        InlineKeyboardButton(text="❌ Vazgeç", callback_data="restore:no"),
    ]])
    await message.answer(
        "💾 <b>Yedek dosyası alındı.</b>\n\n"
        f"Yedekte: {incoming['uploads']} yükleme · {incoming['folders']} klasör · "
        f"{incoming['channels']} kanal\n"
        f"Şu an: {current['uploads']} yükleme · {current['folders']} klasör · "
        f"{current['channels']} kanal\n\n"
        "⚠️ Geri yükleme, şu anki kayıtların <b>tamamını</b> bu yedekle değiştirir.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("restore:"))
async def cb_restore(callback: CallbackQuery) -> None:
    temp_path = _PENDING_RESTORES.pop(callback.from_user.id, None)
    if temp_path is None or not temp_path.exists():
        await callback.answer("Kayıt bulunamadı — dosyayı yeniden gönderin.", show_alert=True)
        return
    if callback.data == "restore:no":
        temp_path.unlink(missing_ok=True)
        await callback.answer("Vazgeçildi.")
        await callback.message.edit_text("❌ Geri yükleme yapılmadı.")
        return
    if UPLOAD_LOCK.locked():
        _PENDING_RESTORES[callback.from_user.id] = temp_path
        await callback.answer("Bir yükleme sürüyor; bitince tekrar deneyin.", show_alert=True)
        return

    await callback.answer("Geri yükleniyor...")
    try:
        await asyncio.to_thread(db.restore_from, temp_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Geri yükleme başarısız.")
        await callback.message.edit_text(f"❌ Geri yüklenemedi: {esc(str(exc))}")
        return
    finally:
        temp_path.unlink(missing_ok=True)

    db.sync_images_dir(get_config().images_dir)
    await callback.message.edit_text(
        "✅ Veritabanı yedekten geri yüklendi.\n"
        f"Toplam yükleme kaydı: {db.upload_stats()['total']}"
    )
