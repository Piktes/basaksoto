"""/istatistik — yükleme raporları."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import db
from .common import esc, fmt_date

router = Router(name="istatistik")


def build_stats_text() -> str:
    stats = db.upload_stats()
    lines = [
        "📊 <b>Yükleme İstatistikleri</b>",
        "",
        f"Bu hafta: <b>{stats['week']}</b>",
        f"Bu ay: <b>{stats['month']}</b>",
        f"Toplam: <b>{stats['total']}</b>",
    ]

    by_channel = db.uploads_by_channel()
    if by_channel:
        lines += ["", "<b>Kanal bazında:</b>"]
        lines += [f"• {esc(row['channel_name'])}: {row['cnt']}" for row in by_channel]

    recent = db.last_uploads(10)
    if recent:
        lines += ["", "<b>Son 10 yükleme:</b>"]
        for row in recent:
            lines.append(
                f"• {fmt_date(row['uploaded_at'])} — {esc(row['title'])} — "
                f"{esc(row['channel_name'])} — <a href=\"{esc(row['video_url'])}\">link</a>"
            )
    else:
        lines += ["", "Henüz yükleme yapılmadı."]

    return "\n".join(lines)


@router.message(Command("istatistik"))
async def cmd_istatistik(message: Message) -> None:
    await message.answer(build_stats_text(), disable_web_page_preview=True)
