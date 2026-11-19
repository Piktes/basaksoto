"""/guncelle komutu.

Sunucuda (Docker/Dokploy) güncelleme GitHub'a gönderimle otomatik yapılır;
kapsayıcı içinde ``git pull`` çalışmaz. Depo klasöründen çalışan kurulumlarda
(Windows / systemd) eski davranış sürer: ``git pull`` + yeniden başlatma.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)
router = Router(name="guncelle")


@router.message(Command("guncelle"))
async def cmd_guncelle(message: Message) -> None:
    if not (PROJECT_ROOT / ".git").exists():
        await message.answer(
            "ℹ️ Bu kurulumda güncelleme otomatiktir: GitHub'a yeni sürüm gönderildiğinde "
            "sunucu kendiliğinden yeniden dağıtılır. Elle tetiklemek için Dokploy panelinde "
            "<b>Deploy</b>'a basın."
        )
        return

    await message.answer("📥 Güncellemeler GitHub'dan çekiliyor...")
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "pull",
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            await message.answer(f"❌ Güncelleme başarısız oldu:\n<code>{err_msg}</code>")
            return

        out_msg = stdout.decode(errors="replace").strip()
        if "Already up to date" in out_msg or "Zaten güncel" in out_msg:
            await message.answer("✅ Bot zaten en güncel sürümde. Yine de yeniden başlatılıyor...")
        else:
            await message.answer(
                f"🔄 Güncelleme tamamlandı:\n<code>{out_msg}</code>\n\n"
                "Bot şimdi yeniden başlatılıyor..."
            )

        # Servis yöneticisi (systemd Restart=always) botu yeniden ayağa kaldırır.
        sys.exit(1)

    except Exception as exc:
        logger.exception("Güncelleme sırasında hata oluştu")
        await message.answer(f"❌ Güncelleme sırasında beklenmedik hata:\n<code>{exc}</code>")
