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
    audio_path: Path | None = None
    video_path: Path | None = None
    created_at: datetime = field(default_factory=datetime.now)


# Başarısız işler: folder_id → job ("🔁 Tekrar dene" için)
RETRY_JOBS: dict[str, UploadJob] = {}


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
    db.set_setting(LAST_SCAN_KEY, datetime.now().isoformat(timespec="seconds"))

    if not new_folders:
        last_scan = db.get_setting(LAST_SCAN_KEY)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Geçmişi göster", callback_data="history")
        ]])
        await _safe_edit(scan_msg, "Yeni klasör yok.")
        await scan_msg.edit_text(
            f"Yeni klasör yok. Son tarama: {fmt_date(last_scan, '%d.%m.%Y %H:%M')}",
            reply_markup=keyboard,
        )
        return

    rows = [
        [InlineKeyboardButton(
            text=(f"{'⚠️' if f['status'] == db.STATUS_ERROR else '📁'} "
                  f"{f['folder_name']} — {fmt_date(f['created_time'])}"),
            callback_data=f"folder:{f['folder_id']}",
        )]
        for f in new_folders
    ]
    rows.append([cancel_button()])
    await state.set_state(UploadFlow.choosing_folder)
    await _safe_edit(scan_msg, f"🔍 Tarama bitti — {len(new_folders)} yeni klasör bulundu.")
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

    rows = [[InlineKeyboardButton(text=f"📺 {c['display_name']}", callback_data=f"channel:{c['id']}")]
            for c in channels]
    rows.append([cancel_button()])
    await state.set_state(UploadFlow.choosing_channel)
    await callback.message.answer(
        "Hangi kanala yüklensin?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ============================================================ kanal seçimi

@router.callback_query(UploadFlow.choosing_channel, F.data.startswith("channel:"))
async def cb_choose_channel(callback: CallbackQuery, state: FSMContext) -> None:
    channel = db.get_channel(int(callback.data.split(":", 1)[1]))
    if channel is None:
        await callback.answer("Kanal bulunamadı.", show_alert=True)
        return
    await state.update_data(channel_name=channel["display_name"])
    await callback.answer()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Kayıtlı görseller", callback_data="imgsrc:saved")],
        [InlineKeyboardButton(text="📤 Telefondan görsel gönder", callback_data="imgsrc:upload")],
        [cancel_button()],
    ])
    await state.set_state(UploadFlow.choosing_image_source)
    await callback.message.answer(
        f"📺 Kanal: <b>{esc(channel['display_name'])}</b> ✅\n\n"
        "🖼 Videoda görünecek görseli nasıl seçmek istersiniz?",
        reply_markup=keyboard,
    )


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
    await callback.answer(f"Seçildi: {image['display_name']}")
    await _ask_title(callback.message, state)


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
        try:
            temp_path.unlink()
        except OSError:
            pass
        await callback.answer("Kütüphaneye kaydedildi.")
    else:
        await callback.answer("Sadece bu yükleme için kullanılacak.")
    await _ask_title(callback.message, state)


# ============================================================ başlık

async def _ask_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    suggested = data["folder_name"][:TITLE_MAX]
    await state.update_data(title=suggested)
    await state.set_state(UploadFlow.confirming_title)
    note = ""
    if len(data["folder_name"]) > TITLE_MAX:
        note = f"\n⚠️ Klasör adı {TITLE_MAX} karakteri aştığı için kırpıldı."
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
    await state.set_state(UploadFlow.editing_title)
    await callback.message.answer(
        f"Yeni başlığı yazın (en fazla {TITLE_MAX} karakter):", reply_markup=cancel_keyboard()
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
    await state.set_state(UploadFlow.editing_description)
    await callback.message.answer(
        f"Yeni açıklamayı tek mesaj olarak yazın (en fazla {DESCRIPTION_MAX} karakter):",
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

            uploader = StudioUploader(cfg)
            video_url = await asyncio.wait_for(
                uploader.upload(
                    video_path=job.video_path,
                    title=job.title,
                    description=job.description,
                    channel_name=job.channel_name,
                    thumbnail_path=cfg.default_thumbnail,
                    on_progress=on_progress,
                    on_screenshot=on_screenshot,
                ),
                timeout=(cfg.upload_timeout_minutes + 5) * 60,
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
