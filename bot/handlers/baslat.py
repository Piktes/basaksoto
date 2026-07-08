"""/baslat — onay tabanlı ana akış (FSM).

Adımlar: Drive tarama → klasör seç → kanal seç → görsel seç → başlık onayla →
açıklama onayla → son onay → ffmpeg video → Playwright ile Studio yüklemesi.

Hata durumunda üretilen mp4 silinmez; ``RETRY_JOBS`` üzerinden yalnızca kalan
adımlar "🔁 Tekrar dene" ile tekrarlanır.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import get_config
from ..services import db, drive, video
from ..services.docx_reader import DocxError, extract_text
from ..services.drive import DriveError
from ..services.studio_uploader import (
    UPLOAD_LOCK,
    VERIFICATION_FUTURES,
    SessionExpiredError,
    StudioUploader,
    StudioUploadError,
)
from ..services.video import VideoError
from ..states import UploadFlow
from .common import CANCEL_CB, cancel_button, cancel_keyboard, esc, fmt_date

logger = logging.getLogger(__name__)

router = Router(name="baslat")

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
_PREVIEW_LIMIT = 3000  # Telegram mesajında gösterilecek azami açıklama uzunluğu
_SAVED_IMAGES_LIMIT = 10

LAST_SCAN_KEY = "last_scan_at"


@dataclass
class UploadJob:
    """Son onaydan sonra yükleme pipeline'ının ihtiyaç duyduğu her şey."""

    chat_id: int
    folder_id: str
    folder_name: str
    channel_name: str
    image_path: Path
    image_is_temp: bool
    title: str
    description: str
    audio_file: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    audio_path: Path | None = None
    video_path: Path | None = None
    created_at: datetime = field(default_factory=datetime.now)


# Başarısız işler: folder_id → job ("🔁 Tekrar dene" için)
RETRY_JOBS: dict[str, UploadJob] = {}

# Kanal onay bekleyenler: folder_id -> (asyncio.Event, Callable[[bool], None])
ACTIVE_CONFIRMS: dict[str, tuple[asyncio.Event, Any]] = {}


# ================================================================ yardımcılar

def _audio_ext(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if suffix in drive.AUDIO_EXTENSIONS else ".mp3"


async def _safe_edit(message: Message, text: str) -> None:
    """Mesajı düzenler; 'message is not modified' vb. hataları yutar."""
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_job_files(job: UploadJob) -> None:
    """Başarılı iş sonunda geçici dosyaları siler."""
    targets = [job.audio_path, job.video_path]
    if job.image_is_temp:
        targets.append(job.image_path)
    for path in targets:
        if path and Path(path).exists():
            try:
                Path(path).unlink()
            except OSError:
                logger.exception("Geçici dosya silinemedi: %s", path)


# ================================================================ /baslat

@router.message(Command("baslat"))
async def cmd_baslat(message: Message, state: FSMContext) -> None:
    if db.get_setting("bot_status") == "paused":
        await message.answer(
            "⚠️ **Bot şu anda durdurulmuş durumda.**\n"
            "Yeni yükleme başlatmak için önce `/restart` yazarak bota start verin."
        )
        return
    if UPLOAD_LOCK.locked():
        await message.answer(
            "⏳ Şu anda bir yükleme devam ediyor. Bitince tekrar /baslat yazabilirsiniz."
        )
        return
    if await state.get_state() is not None:
        await message.answer(
            "⚠️ Devam eden bir akış var. İptal edip yeniden başlamak için:",
            reply_markup=cancel_keyboard(),
        )
        return

    scan_msg = await message.answer("🔍 Drive taranıyor...")
    try:
        folders = await asyncio.to_thread(drive.list_subfolders, get_config().drive_root_folder_id)
    except (DriveError, Exception) as exc:  # noqa: BLE001
        logger.exception("Drive taraması başarısız.")
        await _safe_edit(scan_msg, f"❌ Drive taranamadı: {esc(str(exc))}")
        return

    new_folders = db.sync_folders(folders)
    all_channels = db.get_all_channel_names()
    all_uploads = db.get_all_folder_uploads()
    rows = []
    
    for f in new_folders:
        uploaded = all_uploads.get(f['folder_id'], [])
            
        status_parts = []
        for chan in all_channels:
            short_name = chan.split(" ")[0][:2].upper()
            icon = "✅" if chan in uploaded else "❌"
            status_parts.append(f"{short_name} {icon}")
        status_str = " | ".join(status_parts) if status_parts else ""
        
        icon = '⚠️' if f['status'] == db.STATUS_ERROR else '📁'
        if status_str:
            text = f"[{status_str}] {icon} {f['folder_name']}"
        else:
            text = f"{icon} {f['folder_name']}"
            
        rows.append([
            InlineKeyboardButton(text=text, callback_data=f"folder:{f['folder_id']}")
        ])

    db.set_setting(LAST_SCAN_KEY, datetime.now().isoformat(timespec="seconds"))

    if not rows:
        last_scan = db.get_setting(LAST_SCAN_KEY)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Geçmişi göster", callback_data="history")
        ]])
        await _safe_edit(scan_msg, "Yeni klasör yok.")
        await scan_msg.edit_text(
            f"Yeni veya tamamlanmamış klasör yok. Son tarama: {fmt_date(last_scan, '%d.%m.%Y %H:%M')}",
            reply_markup=keyboard,
        )
        return

    rows.append([
        InlineKeyboardButton(text="🗑️ Tümünü Çöpe At", callback_data="trash_all"),
    ])
    rows.append([cancel_button()])
    await state.set_state(UploadFlow.choosing_folder)
    await _safe_edit(scan_msg, f"🔍 Tarama bitti — {len(rows) - 1} klasör yüklenebilir durumda.")
    await scan_msg.answer(
        "Yüklenecek klasörü seçin:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery) -> None:
    uploads = db.last_uploads(10)
    if not uploads:
        await callback.answer("Henüz yükleme yok.", show_alert=True)
        return
    lines = ["📜 <b>Son yüklemeler:</b>"]
    for row in uploads:
        lines.append(
            f"• {fmt_date(row['uploaded_at'])} — {esc(row['title'])} — "
            f"{esc(row['channel_name'])} — <a href=\"{esc(row['video_url'])}\">link</a>"
        )
    await callback.message.answer("\n".join(lines), disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == CANCEL_CB)
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await callback.message.answer("❌ İşlem iptal edildi.")
    await callback.answer()


# ============================================================ klasör silme

@router.callback_query(F.data.startswith("pipeline_trash:ask:"))
async def cb_pipeline_trash_ask(callback: CallbackQuery) -> None:
    folder_id = callback.data.split(":", 2)[2]
    folder = db.get_folder(folder_id)
    folder_name = folder["folder_name"] if folder else "Klasör"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Evet, Eminim", callback_data=f"pipeline_trash:confirm:{folder_id}"),
            InlineKeyboardButton(text="❌ Hayır, İptal", callback_data=f"pipeline_trash:cancel:{folder_id}")
        ]
    ])
    await callback.message.edit_text(
        f"⚠️ <b>{esc(folder_name)}</b> klasörünü Google Drive çöp kutusuna taşımak istediğinize <b>kesin olarak emin misiniz</b>?",
        reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pipeline_trash:confirm:"))
async def cb_pipeline_trash_confirm(callback: CallbackQuery) -> None:
    folder_id = callback.data.split(":", 2)[2]
    folder = db.get_folder(folder_id)
    folder_name = folder["folder_name"] if folder else "Klasör"

    await callback.message.edit_text("⏳ Klasör siliniyor, lütfen bekleyin...")
    try:
        await asyncio.to_thread(drive.trash_file, folder_id)
        db.delete_folder(folder_id)
        await callback.message.edit_text(f"✅ <b>{esc(folder_name)}</b> başarıyla çöp kutusuna taşındı ve kaydı silindi.")
    except Exception as exc:
        logger.exception("Klasör silinirken hata.")
        await callback.message.edit_text(f"❌ Klasör silinemedi: {esc(str(exc))}")
    await callback.answer()


@router.callback_query(F.data.startswith("pipeline_trash:cancel:"))
async def cb_pipeline_trash_cancel(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.answer("İşlem iptal edildi.")


@router.callback_query(UploadFlow.choosing_folder, F.data == "trash_all")
async def cb_trash_all(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    folders = db.get_new_and_error_folders()
    if not folders:
        await callback.message.answer("Silinecek yeni klasör bulunamadı.")
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Evet, Hepsini Sil", callback_data="confirm_trash_all"),
            InlineKeyboardButton(text="❌ İptal", callback_data="cancel_trash"),
        ]
    ])
    await callback.message.edit_text(
        f"⚠️ Listedeki <b>tüm yeni klasörleri ({len(folders)} adet)</b> Google Drive çöp kutusuna taşımak istediğinize emin misiniz?",
        reply_markup=keyboard,
    )


@router.callback_query(UploadFlow.choosing_folder, F.data == "confirm_trash_all")
async def cb_confirm_trash_all(callback: CallbackQuery, state: FSMContext) -> None:
    folders = db.get_new_and_error_folders()
    if not folders:
        await callback.answer("Silinecek yeni klasör bulunamadı.", show_alert=True)
        await state.clear()
        return

    await callback.answer("Hepsi siliniyor...")
    await callback.message.edit_text(f"⏳ {len(folders)} klasör siliniyor, lütfen bekleyin...")

    success_count = 0
    fail_count = 0

    for f in folders:
        try:
            await asyncio.to_thread(drive.trash_file, f["folder_id"])
            db.delete_folder(f["folder_id"])
            RETRY_JOBS.pop(f["folder_id"], None)
            success_count += 1
        except Exception:
            logger.exception("Klasör silinemedi: %s", f["folder_name"])
            fail_count += 1

    await callback.message.answer(
        f"🧹 Temizlik tamamlandı:\n"
        f"✅ {success_count} klasör silindi.\n"
        f"❌ {fail_count} klasör silinemedi."
    )

    await state.clear()
    await cmd_baslat(callback.message, state)


@router.callback_query(UploadFlow.choosing_folder, F.data == "cancel_trash")
async def cb_cancel_trash(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await state.clear()
    await cmd_baslat(callback.message, state)


# ============================================================ klasör seçimi

@router.callback_query(UploadFlow.choosing_folder, F.data.startswith("folder:"))
async def cb_choose_folder(callback: CallbackQuery, state: FSMContext) -> None:
    folder_id = callback.data.split(":", 1)[1]
    folder = db.get_folder(folder_id)
    if folder is None:
        await callback.answer("Klasör kaydı bulunamadı.", show_alert=True)
        return
    await callback.answer()
    check_msg = await callback.message.answer(
        f"📂 <b>{esc(folder['folder_name'])}</b> içeriği kontrol ediliyor..."
    )

    try:
        files = await asyncio.to_thread(drive.list_files, folder_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Klasör içeriği listelenemedi.")
        await _safe_edit(check_msg, f"❌ Klasör içeriği okunamadı: {esc(str(exc))}")
        await state.clear()
        return

    audio, docx = drive.find_audio_and_docx(files)
    missing = []
    if audio is None:
        missing.append("audio (mp3/wav/m4a/flac)")
    if docx is None:
        missing.append("Word (.docx)")
    if missing:
        await _safe_edit(
            check_msg,
            f"❌ Bu klasörde {' ve '.join(missing)} dosyası bulunamadı.\n"
            "Dosyaları Drive'a ekledikten sonra tekrar /baslat yazın.",
        )
        await state.clear()
        return

    await state.update_data(
        folder_id=folder_id,
        folder_name=folder["folder_name"],
        audio_file=audio,
        docx_file=docx,
    )
    await _safe_edit(check_msg, f"📂 Klasör seçildi: <b>{esc(folder['folder_name'])}</b> ✅")

    channels = db.list_channels()
    if not channels:
        await callback.message.answer(
            "⚠️ Kayıtlı kanal yok. Önce /kanallar komutuyla Studio'daki görünen "
            "kanal ad(lar)ını ekleyin, sonra tekrar /baslat yazın."
        )
        await state.clear()
        return

    uploaded_channels = db.get_uploaded_channels_for_folder(folder_id)
    rows = []
    for c in channels:
        is_uploaded = c['display_name'] in uploaded_channels
        text = f"📺 {c['display_name']}" + (" (Yüklendi ✅)" if is_uploaded else "")
        cb_data = f"already_uploaded:{c['display_name']}" if is_uploaded else f"channel:{c['id']}"
        rows.append([InlineKeyboardButton(text=text, callback_data=cb_data)])
        
    rows.append([cancel_button()])
    await state.set_state(UploadFlow.choosing_channel)
    await callback.message.answer(
        "Hangi kanala yüklensin?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("already_uploaded:"))
async def cb_already_uploaded(callback: CallbackQuery) -> None:
    channel_name = callback.data.split(":", 1)[1]
    await callback.answer(f"⚠️ Bu klasör zaten '{channel_name}' kanalına yüklendi!", show_alert=True)


@router.callback_query(F.data.startswith("upload_confirm:"))
async def cb_upload_confirm(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    decision = parts[1]  # "yes" or "no"
    folder_id = parts[2]
    
    confirm_data = ACTIVE_CONFIRMS.get(folder_id)
    if confirm_data is None:
        await callback.answer("Bu onaylama süresi geçmiş veya iptal edilmiş.", show_alert=True)
        return
        
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
        
    event, set_approval = confirm_data
    if decision == "yes":
        await callback.message.reply("✅ Onaylandı, yükleme işlemine devam ediliyor...")
        set_approval(True)
    else:
        await callback.message.reply("❌ Yükleme durduruldu. Kanalı değiştirip tekrar /baslat yapabilirsiniz.")
        set_approval(False)
        
    event.set()


# ============================================================ kanal seçimi

def _channel_image_key(channel_id: int) -> str:
    return f"channel_image:{channel_id}"


async def _ask_image_source(message: Message, state: FSMContext) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Kayıtlı görseller", callback_data="imgsrc:saved")],
        [InlineKeyboardButton(text="📤 Telefondan görsel gönder", callback_data="imgsrc:upload")],
        [cancel_button()],
    ])
    await state.set_state(UploadFlow.choosing_image_source)
    await message.answer(
        "🖼 Videoda görünecek görseli nasıl seçmek istersiniz?", reply_markup=keyboard
    )


def _remember_channel_image(channel_id: int | None, image_path: str) -> None:
    """Kanal için son kullanılan (kalıcı) görseli hatırlar."""
    if channel_id:
        db.set_setting(_channel_image_key(channel_id), image_path)


@router.callback_query(UploadFlow.choosing_channel, F.data.startswith("channel:"))
async def cb_choose_channel(callback: CallbackQuery, state: FSMContext) -> None:
    channel = db.get_channel(int(callback.data.split(":", 1)[1]))
    if channel is None:
        await callback.answer("Kanal bulunamadı.", show_alert=True)
        return
    await state.update_data(channel_name=channel["display_name"], channel_id=channel["id"])
    await callback.answer()

    # Bu kanal için hatırlanan görsel varsa tek dokunuşla devam önerilir.
    saved_path = db.get_setting(_channel_image_key(channel["id"]))
    if saved_path and Path(saved_path).exists():
        await state.set_state(UploadFlow.confirming_channel_image)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Bu görselle devam", callback_data="chimg:keep")],
            [InlineKeyboardButton(text="🔄 Farklı görsel seç", callback_data="chimg:change")],
            [cancel_button()],
        ])
        try:
            await callback.message.answer_photo(
                FSInputFile(Path(saved_path)),
                caption=(f"📺 Kanal: <b>{esc(channel['display_name'])}</b> ✅\n\n"
                         "🖼 Bu kanalın kayıtlı görseli bu. Bununla devam edilsin mi?"),
                reply_markup=keyboard,
            )
            return
        except Exception:  # noqa: BLE001 — önizleme gönderilemezse normal akış
            logger.exception("Kanal görseli önizlemesi gönderilemedi: %s", saved_path)

    await callback.message.answer(f"📺 Kanal: <b>{esc(channel['display_name'])}</b> ✅")
    await _ask_image_source(callback.message, state)


@router.callback_query(UploadFlow.confirming_channel_image, F.data == "chimg:keep")
async def cb_channel_image_keep(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved_path = db.get_setting(_channel_image_key(data["channel_id"]))
    if not (saved_path and Path(saved_path).exists()):
        await callback.answer("Görsel artık yok, yeniden seçin.", show_alert=True)
        await _ask_image_source(callback.message, state)
        return
    await state.update_data(image_path=saved_path, image_is_temp=False)
    await callback.answer("Kanal görseliyle devam ediliyor.")
    await _ask_tags(callback.message, state)


@router.callback_query(UploadFlow.confirming_channel_image, F.data == "chimg:change")
async def cb_channel_image_change(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_image_source(callback.message, state)


# ============================================================ görsel seçimi

@router.callback_query(UploadFlow.choosing_image_source, F.data == "imgsrc:saved")
async def cb_image_source_saved(callback: CallbackQuery, state: FSMContext) -> None:
    cfg = get_config()
    db.sync_images_dir(cfg.images_dir)
    images = [img for img in db.list_images() if Path(img["file_path"]).exists()]
    await callback.answer()
    if not images:
        await callback.message.answer(
            "🖼 Kütüphanede görsel yok. Telefondan görsel gönderebilir veya "
            f"<code>{esc(str(cfg.images_dir))}</code> klasörüne görsel kopyalayabilirsiniz.",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.set_state(UploadFlow.choosing_saved_image)
    await callback.message.answer("Kayıtlı görseller — birini seçin:")
    for image in images[:_SAVED_IMAGES_LIMIT]:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Bu görseli seç", callback_data=f"img:{image['id']}")
        ]])
        try:
            await callback.message.answer_photo(
                FSInputFile(Path(image["file_path"])),
                caption=esc(image["display_name"]),
                reply_markup=keyboard,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Görsel önizlemesi gönderilemedi: %s", image["file_path"])
    await callback.message.answer("Ya da iptal edin:", reply_markup=cancel_keyboard())


@router.callback_query(UploadFlow.choosing_saved_image, F.data.startswith("img:"))
async def cb_choose_saved_image(callback: CallbackQuery, state: FSMContext) -> None:
    image = db.get_image(int(callback.data.split(":", 1)[1]))
    if image is None or not Path(image["file_path"]).exists():
        await callback.answer("Görsel bulunamadı.", show_alert=True)
        return
    await state.update_data(image_path=image["file_path"], image_is_temp=False)
    data = await state.get_data()
    _remember_channel_image(data.get("channel_id"), image["file_path"])
    await callback.answer(f"Seçildi: {image['display_name']}")
    await _ask_tags(callback.message, state)


@router.callback_query(UploadFlow.choosing_image_source, F.data == "imgsrc:upload")
async def cb_image_source_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UploadFlow.waiting_photo)
    await callback.message.answer(
        "📤 Görseli fotoğraf olarak gönderin:", reply_markup=cancel_keyboard()
    )
    await callback.answer()


@router.message(UploadFlow.waiting_photo, F.photo)
async def msg_photo_received(message: Message, state: FSMContext, bot: Bot) -> None:
    cfg = get_config()
    photo = message.photo[-1]  # en yüksek çözünürlük
    temp_path = cfg.temp_dir / f"tg_{photo.file_unique_id}.jpg"
    await bot.download(photo, destination=str(temp_path))
    await state.update_data(image_path=str(temp_path), image_is_temp=True)
    await state.set_state(UploadFlow.confirming_save_image)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Evet, kaydet", callback_data="saveimg:yes"),
         InlineKeyboardButton(text="➡️ Hayır, sadece bu iş için", callback_data="saveimg:no")],
        [cancel_button()],
    ])
    await message.answer("Bu görseli kütüphaneye kaydedeyim mi?", reply_markup=keyboard)


@router.message(UploadFlow.waiting_photo)
async def msg_photo_expected(message: Message) -> None:
    await message.answer("Lütfen bir <b>fotoğraf</b> gönderin (dosya değil).",
                         reply_markup=cancel_keyboard())


@router.callback_query(UploadFlow.confirming_save_image, F.data.startswith("saveimg:"))
async def cb_save_image(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    temp_path = Path(data["image_path"])
    if choice == "yes":
        cfg = get_config()
        target = cfg.images_dir / temp_path.name
        target.write_bytes(temp_path.read_bytes())
        display_name = f"Telegram {datetime.now():%d.%m.%Y %H:%M}"
        db.add_image(target, display_name)
        await state.update_data(image_path=str(target), image_is_temp=False)
        _remember_channel_image(data.get("channel_id"), str(target))
        try:
            temp_path.unlink()
        except OSError:
            pass
        await callback.answer("Kütüphaneye kaydedildi.")
    else:
        await callback.answer("Sadece bu yükleme için kullanılacak.")
    await _ask_tags(callback.message, state)


# ============================================================ etiketler

def _get_tags_keyboard(tags: list[Any], selected_tags: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for t in tags:
        name = t["name"]
        icon = "✅" if name in selected_tags else "⬜"
        rows.append([
            InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"tag:toggle:{t['id']}")
        ])
    
    rows.append([
        InlineKeyboardButton(text="Tümünü Seç", callback_data="tag:select_all"),
        InlineKeyboardButton(text="Temizle", callback_data="tag:clear_all")
    ])
    rows.append([
        InlineKeyboardButton(text="➕ Yeni etiket ekle", callback_data="tag:add_new")
    ])
    rows.append([
        InlineKeyboardButton(text="Devam Et ➡️", callback_data="tag:continue")
    ])
    rows.append([cancel_button()])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_tags(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected_tags = data.get("selected_tags")
    
    if selected_tags is None:
        db_tags = db.list_tags()
        selected_tags = [t["name"] for t in db_tags if t["is_default"] == 1]
        await state.update_data(selected_tags=selected_tags)
        
    db_tags = db.list_tags()
    keyboard = _get_tags_keyboard(db_tags, selected_tags)
    
    await state.set_state(UploadFlow.choosing_tags)
    await message.answer(
        "🏷️ <b>Videoya eklenecek etiketleri seçin:</b>\n"
        "(YouTube Studio'da gelişmiş ayarlara eklenecektir)",
        reply_markup=keyboard
    )


@router.callback_query(UploadFlow.choosing_tags, F.data.startswith("tag:toggle:"))
async def cb_tag_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    tag_id = int(callback.data.split(":", 2)[2])
    db_tags = db.list_tags()
    tag = next((t for t in db_tags if t["id"] == tag_id), None)
    if tag is None:
        await callback.answer("Etiket bulunamadı.")
        return
        
    data = await state.get_data()
    selected_tags = list(data.get("selected_tags", []))
    
    if tag["name"] in selected_tags:
        selected_tags.remove(tag["name"])
        await callback.answer(f"Çıkarıldı: {tag['name']}")
    else:
        selected_tags.append(tag["name"])
        await callback.answer(f"Eklendi: {tag['name']}")
        
    await state.update_data(selected_tags=selected_tags)
    
    keyboard = _get_tags_keyboard(db_tags, selected_tags)
    await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(UploadFlow.choosing_tags, F.data == "tag:select_all")
async def cb_tag_select_all(callback: CallbackQuery, state: FSMContext) -> None:
    db_tags = db.list_tags()
    selected_tags = [t["name"] for t in db_tags]
    await state.update_data(selected_tags=selected_tags)
    await callback.answer("Tüm etiketler seçildi.")
    
    keyboard = _get_tags_keyboard(db_tags, selected_tags)
    await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(UploadFlow.choosing_tags, F.data == "tag:clear_all")
async def cb_tag_clear_all(callback: CallbackQuery, state: FSMContext) -> None:
    db_tags = db.list_tags()
    selected_tags = []
    await state.update_data(selected_tags=selected_tags)
    await callback.answer("Tüm seçimler temizlendi.")
    
    keyboard = _get_tags_keyboard(db_tags, selected_tags)
    await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(UploadFlow.choosing_tags, F.data == "tag:add_new")
async def cb_tag_add_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UploadFlow.adding_new_tag)
    await callback.message.answer(
        "➕ Lütfen eklemek istediğiniz <b>yeni etiketi</b> yazın:\n"
        "(Örn: <code>yeni beste</code>)",
        reply_markup=cancel_keyboard()
    )
    await callback.answer()


@router.message(UploadFlow.adding_new_tag, F.text)
async def msg_add_new_tag(message: Message, state: FSMContext) -> None:
    tag_name = message.text.strip()
    if not tag_name:
        await message.answer("Etiket boş olamaz.")
        return
        
    db.add_tag(tag_name)
    data = await state.get_data()
    selected_tags = list(data.get("selected_tags", []))
    if tag_name not in selected_tags:
        selected_tags.append(tag_name)
    await state.update_data(selected_tags=selected_tags)
    
    await message.answer(f"✅ Yeni etiket eklendi ve seçildi: <code>{esc(tag_name)}</code>")
    await _ask_tags(message, state)


@router.callback_query(UploadFlow.choosing_tags, F.data == "tag:continue")
async def cb_tag_continue(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_title(callback.message, state)


# ============================================================ başlık

async def _ask_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    prefix = db.get_setting(f"title_prefix:{data['channel_id']}") if data.get("channel_id") else None
    full = f"{prefix} - {data['folder_name']}" if prefix else data["folder_name"]
    suggested = full[:TITLE_MAX]
    await state.update_data(title=suggested)
    await state.set_state(UploadFlow.confirming_title)
    note = ""
    if len(full) > TITLE_MAX:
        note = f"\n⚠️ Başlık {TITLE_MAX} karakteri aştığı için kırpıldı."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Onayla", callback_data="title:ok"),
         InlineKeyboardButton(text="✏️ Düzenle", callback_data="title:edit")],
        [cancel_button()],
    ])
    await message.answer(f"🎬 Başlık: <b>{esc(suggested)}</b>{note}", reply_markup=keyboard)


@router.callback_query(UploadFlow.confirming_title, F.data == "title:ok")
async def cb_title_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_description(callback.message, state)


@router.callback_query(UploadFlow.confirming_title, F.data == "title:edit")
async def cb_title_edit(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(UploadFlow.editing_title)
    await callback.message.answer(
        "Mevcut başlık (dokununca kopyalanır — yapıştırıp üzerinde düzeltin):\n"
        f"<code>{esc(data.get('title', ''))}</code>\n\n"
        f"Yeni başlığı tek mesaj olarak gönderin (en fazla {TITLE_MAX} karakter):",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(UploadFlow.editing_title, F.text)
async def msg_new_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        await message.answer("Başlık boş olamaz, tekrar yazın:")
        return
    if len(title) > TITLE_MAX:
        await message.answer(
            f"⚠️ Başlık {len(title)} karakter — en fazla {TITLE_MAX} olmalı. Kısaltıp tekrar gönderin:"
        )
        return
    await state.update_data(title=title)
    await message.answer(f"🎬 Başlık güncellendi: <b>{esc(title)}</b>")
    await _ask_description(message, state)


# ============================================================ açıklama

async def _ask_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    reading_msg = await message.answer("📄 Word dosyası okunuyor...")

    cfg = get_config()
    docx_file = data["docx_file"]
    docx_path = cfg.temp_dir / f"{data['folder_id']}.docx"
    try:
        await asyncio.to_thread(drive.download_file, docx_file, docx_path)
        text = await asyncio.to_thread(extract_text, docx_path)
    except (DriveError, DocxError, Exception) as exc:  # noqa: BLE001
        logger.exception("Açıklama metni çıkarılamadı.")
        await _safe_edit(reading_msg, f"❌ Word dosyası okunamadı: {esc(str(exc))}")
        await state.clear()
        return
    finally:
        if docx_path.exists():
            try:
                docx_path.unlink()
            except OSError:
                pass

    truncated_note = ""
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX]
        truncated_note = f"\n\n⚠️ Metin {DESCRIPTION_MAX} karakteri aştığı için kırpıldı."

    await state.update_data(description=text)
    await state.set_state(UploadFlow.confirming_description)

    preview = text[:_PREVIEW_LIMIT]
    preview_note = "" if len(text) <= _PREVIEW_LIMIT else "\n\n<i>(önizleme kırpıldı — tamamı yüklemede kullanılacak)</i>"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Onayla", callback_data="desc:ok"),
         InlineKeyboardButton(text="✏️ Düzenle", callback_data="desc:edit")],
        [cancel_button()],
    ])
    await _safe_edit(reading_msg, "📄 Word dosyası okundu ✅")
    await message.answer(
        f"📝 <b>Açıklama:</b>\n\n{esc(preview) or '<i>(boş)</i>'}{preview_note}{truncated_note}",
        reply_markup=keyboard,
    )


@router.callback_query(UploadFlow.confirming_description, F.data == "desc:ok")
async def cb_desc_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _final_confirm(callback.message, state)


@router.callback_query(UploadFlow.confirming_description, F.data == "desc:edit")
async def cb_desc_edit(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("description", "")
    await state.set_state(UploadFlow.editing_description)
    if current:
        # Telegram mesaj sınırı 4096; kopyalanabilir blok için pay bırakılır.
        chunk = current[:3900]
        more = "\n\n<i>(devamı kırpıldı)</i>" if len(current) > 3900 else ""
        await callback.message.answer(
            "Mevcut açıklama (dokununca kopyalanır — yapıştırıp üzerinde düzeltin):"
            f"\n<code>{esc(chunk)}</code>{more}"
        )
    await callback.message.answer(
        f"Yeni açıklamayı tek mesaj olarak gönderin (en fazla {DESCRIPTION_MAX} karakter):",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(UploadFlow.editing_description, F.text)
async def msg_new_description(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if len(text) > DESCRIPTION_MAX:
        await message.answer(
            f"⚠️ Açıklama {len(text)} karakter — en fazla {DESCRIPTION_MAX} olmalı. Kısaltıp tekrar gönderin:"
        )
        return
    await state.update_data(description=text)
    await message.answer("📝 Açıklama güncellendi.")
    await _final_confirm(message, state)


# ============================================================ son onay

async def _final_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(UploadFlow.final_confirm)
    description = data.get("description", "")
    caption = (
        "🚦 <b>Son kontrol</b>\n\n"
        f"📁 Klasör: <b>{esc(data['folder_name'])}</b>\n"
        f"📺 Kanal: <b>{esc(data['channel_name'])}</b>\n"
        f"🎬 Başlık: <b>{esc(data['title'])}</b>\n"
        f"📝 Açıklama: {esc(description[:200])}{'…' if len(description) > 200 else ''}\n"
        f"🌐 Görünürlük: <b>Herkese Açık</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Yükle", callback_data="upload:go"), cancel_button()],
    ])
    try:
        await message.answer_photo(FSInputFile(Path(data["image_path"])),
                                   caption=caption, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — görsel gönderilemezse metinle devam
        logger.exception("Özet kartındaki görsel gönderilemedi.")
        await message.answer(caption, reply_markup=keyboard)


@router.callback_query(UploadFlow.final_confirm, F.data == "upload:go")
async def cb_upload_go(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()  # etkileşimli akış bitti; iş artık pipeline'da
    await callback.answer("Başlıyoruz!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    job = UploadJob(
        chat_id=callback.message.chat.id,
        folder_id=data["folder_id"],
        folder_name=data["folder_name"],
        channel_name=data["channel_name"],
        image_path=Path(data["image_path"]),
        image_is_temp=bool(data.get("image_is_temp")),
        title=data["title"],
        description=data.get("description", ""),
        audio_file=data["audio_file"],
        tags=data.get("selected_tags", []),
    )
    asyncio.create_task(_run_pipeline(bot, job))


# ============================================================ tekrar dene

@router.callback_query(F.data.startswith("retry:"))
async def cb_retry(callback: CallbackQuery, bot: Bot) -> None:
    folder_id = callback.data.split(":", 1)[1]
    job = RETRY_JOBS.get(folder_id)
    if job is None:
        await callback.answer("Bu iş için tekrar deneme kaydı yok (bot yeniden başlamış olabilir).",
                              show_alert=True)
        return
    if UPLOAD_LOCK.locked():
        await callback.answer("Başka bir yükleme sürüyor, bitince tekrar deneyin.", show_alert=True)
        return
    await callback.answer("Tekrar deneniyor...")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    asyncio.create_task(_run_pipeline(bot, job))


# ============================================================ pipeline

async def _run_pipeline(bot: Bot, job: UploadJob) -> None:
    """İndir → ffmpeg → Studio yükle. Hata durumunda mp4 saklanır, retry sunulur."""
    cfg = get_config()
    if UPLOAD_LOCK.locked():
        await bot.send_message(job.chat_id, "⏳ Başka bir yükleme sürüyor; bitince tekrar deneyin.")
        return

    async with UPLOAD_LOCK:
        try:
            # 1) Ses dosyası (retry'da zaten inmişse atla)
            if not (job.audio_path and job.audio_path.exists()):
                msg = await bot.send_message(job.chat_id, "⬇️ Ses dosyası indiriliyor...")
                audio_path = cfg.temp_dir / f"{job.folder_id}{_audio_ext(job.audio_file['name'])}"
                await asyncio.to_thread(drive.download_file, job.audio_file, audio_path)
                job.audio_path = audio_path
                await _safe_edit(msg, "⬇️ Ses dosyası indirildi ✅")

            # 2) Video üretimi (retry'da mp4 varsa atla)
            if not (job.video_path and job.video_path.exists()):
                msg = await bot.send_message(job.chat_id, "🎞 Video hazırlanıyor...")
                output = cfg.temp_dir / f"{job.folder_id}.mp4"
                await video.create_video(job.image_path, job.audio_path, output)
                job.video_path = output
                await _safe_edit(msg, "🎞 Video hazır ✅")

            # 3) Studio yüklemesi
            status_msg = await bot.send_message(job.chat_id, "📤 YouTube Studio'ya yükleniyor...")

            async def on_progress(text: str) -> None:
                await _safe_edit(status_msg, f"📤 {text}")

            async def on_screenshot(png: bytes, caption: str) -> None:
                try:
                    await bot.send_photo(
                        job.chat_id, BufferedInputFile(png, filename="studio.png"), caption=caption
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Ekran görüntüsü gönderilemedi.")

            confirm_event = asyncio.Event()
            user_approved = False

            async def on_channel_confirm(screenshot_bytes: bytes) -> bool:
                nonlocal user_approved
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Devam Et", callback_data=f"upload_confirm:yes:{job.folder_id}"),
                        InlineKeyboardButton(text="❌ Durdur", callback_data=f"upload_confirm:no:{job.folder_id}"),
                    ]
                ])
                
                ACTIVE_CONFIRMS[job.folder_id] = (confirm_event, set_approved_flag)
                
                await bot.send_photo(
                    chat_id=job.chat_id,
                    photo=BufferedInputFile(screenshot_bytes, filename="kanal_kontrol.png"),
                    caption=(
                        f"👀 <b>Kanal Doğrulama Kontrolü</b>\n\n"
                        f"📂 Klasör: <b>{esc(job.folder_name)}</b>\n"
                        f"📺 Hedef Kanal: <b>{esc(job.channel_name)}</b>\n\n"
                        f"Lütfen yukarıdaki ekran görüntüsünü kontrol edin. Doğru kanal açık mı?\n"
                        f"Yükleme başlasın mı?"
                    ),
                    reply_markup=keyboard
                )
                await confirm_event.wait()
                return user_approved

            def set_approved_flag(approved: bool) -> None:
                nonlocal user_approved
                user_approved = approved

            uploader = StudioUploader(cfg)
            video_url = await asyncio.wait_for(
                uploader.upload(
                    video_path=job.video_path,
                    title=job.title,
                    description=job.description,
                    channel_name=job.channel_name,
                    chat_id=job.chat_id,
                    thumbnail_path=cfg.default_thumbnail,
                    tags=job.tags,
                    on_progress=on_progress,
                    on_screenshot=on_screenshot,
                    on_channel_confirm=on_channel_confirm,
                ),
                timeout=(cfg.upload_timeout_minutes + 10) * 60,
            )

            # 4) Başarı: kayıt + temizlik
            db.set_folder_status(job.folder_id, db.STATUS_UPLOADED,
                                 video_url=video_url, channel_name=job.channel_name)
            db.record_upload(job.folder_id, job.title, job.channel_name, video_url)
            RETRY_JOBS.pop(job.folder_id, None)
            _cleanup_job_files(job)
            await bot.send_message(
                job.chat_id,
                f"✅ <b>Yayınlandı!</b>\n🎬 {esc(job.title)}\n📺 {esc(job.channel_name)}\n🔗 {esc(video_url)}",
                disable_web_page_preview=False,
            )

            # Çöpe atma sorgusu
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗑️ Evet, Çöpe At", callback_data=f"pipeline_trash:ask:{job.folder_id}"),
                    InlineKeyboardButton(text="❌ Hayır", callback_data=f"pipeline_trash:cancel:{job.folder_id}")
                ]
            ])
            await bot.send_message(
                job.chat_id,
                f"📂 <b>{esc(job.folder_name)}</b> klasörünü Google Drive çöp kutusuna taşımak ister misiniz?",
                reply_markup=keyboard,
            )

        except SessionExpiredError as exc:
            await _handle_failure(bot, job, str(exc), header="🔑 Google oturumu düşmüş.")
        except asyncio.TimeoutError:
            await _handle_failure(
                bot, job,
                f"Yükleme {cfg.upload_timeout_minutes} dakikalık zaman aşımına takıldı.",
                header="⏱ Zaman aşımı.",
            )
        except StudioUploadError as exc:
            if exc.screenshot:
                try:
                    await bot.send_photo(
                        job.chat_id,
                        BufferedInputFile(exc.screenshot, filename="hata.png"),
                        caption=f"📸 Hata anı — adım: {esc(exc.step)}",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Hata ekran görüntüsü gönderilemedi.")
            await _handle_failure(bot, job, str(exc),
                                  header=f"❌ Studio yüklemesi '{esc(exc.step)}' adımında takıldı.")
        except VideoError as exc:
            await _handle_failure(bot, job, str(exc), header="❌ Video üretilemedi (ffmpeg).")
        except DriveError as exc:
            await _handle_failure(bot, job, str(exc), header="❌ Drive'dan dosya indirilemedi.")
        except Exception as exc:  # noqa: BLE001 — beklenmedik hata da kullanıcıya bildirilir
            logger.exception("Yükleme pipeline'ında beklenmedik hata.")
            await _handle_failure(bot, job, str(exc), header="❌ Beklenmedik bir hata oluştu.")


async def _handle_failure(bot: Bot, job: UploadJob, detail: str, *, header: str) -> None:
    """Hata mesajı + klasörü 'hata' durumuna al + 🔁 Tekrar dene butonu."""
    db.set_folder_status(job.folder_id, db.STATUS_ERROR)
    RETRY_JOBS[job.folder_id] = job  # mp4/audio saklanır; yalnızca kalan adımlar tekrarlanır
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Tekrar dene", callback_data=f"retry:{job.folder_id}")
    ]])
    kept = " Üretilen video saklandı; tekrar denemede yeniden üretilmez." \
        if job.video_path and job.video_path.exists() else ""
    await bot.send_message(
        job.chat_id,
        f"{header}\n\n<b>Klasör:</b> {esc(job.folder_name)}\n<b>Ayrıntı:</b> {esc(detail)}\n{kept}",
        reply_markup=keyboard,
    )


# ============================================================ doğrulama kodu dinleyicisi

def is_waiting_verification(message: Message) -> bool:
    return message.chat.id in VERIFICATION_FUTURES


@router.message(is_waiting_verification, F.text)
async def cb_verification_code_received(message: Message) -> None:
    code = message.text.strip()
    fut = VERIFICATION_FUTURES.get(message.chat.id)
    if fut and not fut.done():
        fut.set_result(code)
        await message.answer("🔑 Kod alındı, Google doğrulama sayfasına giriliyor...")
