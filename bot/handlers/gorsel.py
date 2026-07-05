"""/gorseller — görsel kütüphanesi yönetimi (listeleme + silme).

Ayrıca akış dışında bota gönderilen her fotoğraf için "kütüphaneye kaydet /
varsayılan thumbnail yap" seçenekleri sunulur.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import get_config
from ..services import db
from .common import esc

logger = logging.getLogger(__name__)

router = Router(name="gorsel")

# Akış dışı gönderilen fotoğraflar: user_id → geçici dosya yolu
_PENDING_PHOTOS: dict[int, Path] = {}


@router.message(Command("gorseller"))
async def cmd_gorseller(message: Message) -> None:
    cfg = get_config()
    db.sync_images_dir(cfg.images_dir)
    images = db.list_images()
    if not images:
        await message.answer(
            "🖼 Kütüphanede görsel yok.\n\n"
            f"<code>{esc(str(cfg.images_dir))}</code> klasörüne görsel kopyalayabilir "
            "veya /baslat akışında telefondan görsel gönderip kaydedebilirsiniz."
        )
        return

    await message.answer(f"🖼 Kütüphanede <b>{len(images)}</b> görsel var:")
    for image in images:
        path = Path(image["file_path"])
        if not path.exists():
            continue
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Sil", callback_data=f"imgdel:{image['id']}")
        ]])
        try:
            await message.answer_photo(
                FSInputFile(path), caption=esc(image["display_name"]), reply_markup=keyboard
            )
        except Exception:  # noqa: BLE001 — bozuk dosya listeyi durdurmasın
            logger.exception("Görsel gönderilemedi: %s", path)
            await message.answer(f"⚠️ Görsel gönderilemedi: {esc(image['display_name'])}")


@router.message(F.photo, StateFilter(None))
async def msg_photo_to_library(message: Message, bot: Bot) -> None:
    """Akış dışında gönderilen fotoğraf: kütüphaneye/thumbnail'e kaydetme önerisi."""
    cfg = get_config()
    photo = message.photo[-1]  # en yüksek çözünürlük
    temp_path = cfg.temp_dir / f"lib_{photo.file_unique_id}.jpg"
    await bot.download(photo, destination=str(temp_path))
    _PENDING_PHOTOS[message.from_user.id] = temp_path
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Kütüphaneye kaydet", callback_data="glib:save")],
        [InlineKeyboardButton(text="🌟 Kaydet + varsayılan thumbnail yap", callback_data="glib:thumb")],
        [InlineKeyboardButton(text="❌ Vazgeç", callback_data="glib:cancel")],
    ])
    await message.answer("Bu görseli ne yapayım?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("glib:"))
async def cb_photo_to_library(callback: CallbackQuery) -> None:
    action = callback.data.split(":", 1)[1]
    temp_path = _PENDING_PHOTOS.pop(callback.from_user.id, None)
    if temp_path is None or not temp_path.exists():
        await callback.answer("Kayıt bulunamadı — fotoğrafı yeniden gönderin.", show_alert=True)
        return
    cfg = get_config()
    if action == "cancel":
        temp_path.unlink(missing_ok=True)
        await callback.answer("Vazgeçildi.")
        await callback.message.edit_text("❌ Görsel kaydedilmedi.")
        return

    target = cfg.images_dir / temp_path.name
    target.write_bytes(temp_path.read_bytes())
    db.sync_images_dir(cfg.images_dir)  # aynı dosya tabloda yoksa ekler
    lines = ["✅ Görsel kütüphaneye kaydedildi — /baslat akışında 📚 listesinde görünecek."]
    if action == "thumb":
        cfg.default_thumbnail.parent.mkdir(parents=True, exist_ok=True)
        cfg.default_thumbnail.write_bytes(temp_path.read_bytes())
        lines.append("🌟 Ayrıca varsayılan thumbnail olarak ayarlandı (tüm videolarda kullanılır).")
    temp_path.unlink(missing_ok=True)
    await callback.answer("Kaydedildi.")
    await callback.message.edit_text("\n".join(lines))


@router.callback_query(F.data.startswith("imgdel:"))
async def cb_delete_image(callback: CallbackQuery) -> None:
    cfg = get_config()
    image_id = int(callback.data.split(":", 1)[1])
    row = db.delete_image(image_id)
    if row is None:
        await callback.answer("Görsel zaten silinmiş.")
        return
    path = Path(row["file_path"])
    # Sabit thumbnail dosyası kütüphaneden çıksa da diskten silinmez.
    if path.exists() and path.resolve() != cfg.default_thumbnail.resolve():
        try:
            path.unlink()
        except OSError:
            logger.exception("Görsel dosyası silinemedi: %s", path)
    await callback.answer("Silindi.")
    await callback.message.edit_caption(
        caption=f"🗑 Silindi: {esc(row['display_name'])}", reply_markup=None
    )
