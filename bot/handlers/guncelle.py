"""/guncelle komutu: GitHub'dan en son sürümü çeker ve botu yeniden başlatır."""

from __future__ import annotations

import asyncio
import logging
import sys
import subprocess
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router(name="guncelle")


@router.message(Command("guncelle"))
async def cmd_guncelle(message: Message) -> None:
    await message.answer("📥 Güncellemeler GitHub'dan çekiliyor...")

    try:
        # git pull komutunu asenkron olarak çalıştır
        process = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            await message.answer(f"❌ Güncelleme başarısız oldu:\n<code>{err_msg}</code>")
            return

        out_msg = stdout.decode(errors="replace").strip()
        if "Already up to date" in out_msg or "Zaten güncel" in out_msg:
            await message.answer("✅ Bot zaten en güncel sürümde.")
            return

        await message.answer(
            f"🔄 Güncelleme tamamlandı:\n<code>{out_msg}</code>\n\n"
            "Bot şimdi yeniden başlatılıyor..."
        )
        
        # systemd servisi (Restart=always) botu otomatik ayağa kaldıracaktır
        sys.exit(1)

    except Exception as exc:
        logger.exception("Güncelleme sırasında hata oluştu")
        await message.answer(f"❌ Güncelleme sırasında beklenmedik hata:\n<code>{exc}</code>")
