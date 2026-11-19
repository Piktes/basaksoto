"""Veritabanı yedeği: tutarlı kopya üretir ve Telegram'dan gönderir.

Yükleme geçmişi yalnızca ``bot.db`` içinde durur; sunucu/volume kaybolursa
geri getirmenin tek yolu bu yedeklerdir. Geri yükleme: yedek dosyası bota
belge olarak gönderilir (bkz. ``handlers/yedek.py``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from ..config import Config
from . import db

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "basaksoto-yedek-"


def create_backup(cfg: Config) -> Path:
    """Geçici klasöre zaman damgalı bir yedek dosyası yazar ve yolunu döndürür."""
    path = cfg.temp_dir / f"{BACKUP_PREFIX}{datetime.now():%Y%m%d-%H%M}.db"
    if path.exists():
        path.unlink()
    db.backup_to(path)
    return path


async def send_backup(bot: Bot, chat_id: int, cfg: Config, *, note: str = "") -> None:
    """Yedeği üretir ve ``chat_id``'ye belge olarak gönderir."""
    path = await asyncio.to_thread(create_backup, cfg)
    try:
        stats = await asyncio.to_thread(db.inspect_backup, path)
        caption = (
            f"💾 <b>Veritabanı yedeği</b>{note}\n"
            f"Yükleme: {stats['uploads']} · Klasör: {stats['folders']} · Kanal: {stats['channels']}\n\n"
            "Geri yüklemek için bu dosyayı bota belge olarak göndermeniz yeterli."
        )
        await bot.send_document(chat_id, FSInputFile(path), caption=caption)
    finally:
        try:
            path.unlink()
        except OSError:
            logger.warning("Yedek dosyası silinemedi: %s", path)
