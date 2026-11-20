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
from .common import BATCH, CANCEL_CB, cancel_button, cancel_keyboard, esc, fmt_date, is_busy

logger = logging.getLogger(__name__)

router = Router(name="baslat")

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
_PREVIEW_LIMIT = 3000  # Telegram mesajında gösterilecek azami açıklama uzunluğu
_SAVED_IMAGES_LIMIT = 10

LAST_SCAN_KEY = "last_scan_at"

PAGE_SIZE = 8  # klasör listesinde sayfa başına düğme
FILTER_PENDING = "pending"
FILTER_ALL = "all"


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

def _fold(text: str) -> str:
    """Türkçe harflere duyarlı küçük harfe çevirme (arama için)."""
    return (text or "").replace("İ", "i").replace("I", "ı").lower()


def _is_complete(uploaded: set[str], channels: list[str]) -> bool:
    """Klasör kayıtlı tüm kanallara yüklendi mi? (Kanal yoksa: hiç yükleme var mı?)"""
    if channels:
        return all(c in uploaded for c in channels)
    return bool(uploaded)


def _filtered_folders(filter_: str, query: str) -> tuple[list[Any], list[str], dict[str, list[str]], int, int]:
    """Süzülmüş klasörler + kanal adları + yükleme haritası + toplam + bekleyen sayısı."""
    folders = db.get_active_folders()
    channels = db.get_all_channel_names()
    uploads = db.get_all_folder_uploads()
    needle = _fold(query)
    visible = []
    pending_total = 0
    for f in folders:
        done = _is_complete(set(uploads.get(f["folder_id"], [])), channels)
        if not done:
            pending_total += 1
        if filter_ == FILTER_PENDING and done:
            continue
        if needle and needle not in _fold(f["folder_name"]):
            continue
        visible.append(f)
    return visible, channels, uploads, len(folders), pending_total


def _folder_label(folder: Any, channels: list[str], uploads: dict[str, list[str]],
                  checked: bool | None = None) -> str:
    # Yükleme işaretleri adın ÖNÜNDE durur: Telegram uzun düğme metnini sondan "…" ile
    # kırptığı için sonda olsalardı uzun klasör adlarında görünmezlerdi.
    uploaded = uploads.get(folder["folder_id"], [])
    marks = "".join("✅" if c in uploaded else "▫️" for c in channels)
    if checked is not None:
        icon = "☑️" if checked else "⬜"
    else:
        icon = "⚠️" if folder["status"] == db.STATUS_ERROR else ""
    prefix = " ".join(part for part in (icon, marks) if part)
    return f"{prefix} {folder['folder_name']}" if prefix else f"📁 {folder['folder_name']}"


def _folder_list_view(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Klasör listesi (sayfalı, süzgeçli, aramalı); tek tek ve toplu seçim modları."""
    filter_ = data.get("fl_filter", FILTER_PENDING)
    query = data.get("fl_query", "")
    batch = bool(data.get("fl_batch"))
    selected = set(data.get("bq_sel", []))
    visible, channels, uploads, total, pending_total = _filtered_folders(filter_, query)

    pages = max(1, -(-len(visible) // PAGE_SIZE))
    page = min(max(int(data.get("fl_page", 0)), 0), pages - 1)

    rows: list[list[InlineKeyboardButton]] = []
    for f in visible[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        fid = f["folder_id"]
        if batch:
            rows.append([InlineKeyboardButton(
                text=_folder_label(f, channels, uploads, checked=fid in selected),
                callback_data=f"bqt:{fid}")])
        else:
            rows.append([InlineKeyboardButton(
                text=_folder_label(f, channels, uploads), callback_data=f"folder:{fid}")])

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"fl:p:{(page - 1) % pages}"),
            InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="fl:noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"fl:p:{(page + 1) % pages}"),
        ])
    rows.append([
        InlineKeyboardButton(text="✖️ Aramayı temizle", callback_data="fl:x") if query
        else InlineKeyboardButton(text="🔎 Ara", callback_data="fl:s"),
        InlineKeyboardButton(
            text="📋 Tümünü göster" if filter_ == FILTER_PENDING else "⏳ Yalnızca bekleyenler",
            callback_data="fl:f"),
    ])
    if batch:
        rows.append([
            InlineKeyboardButton(text="☑️ Görünenlerin tümü", callback_data="bq:all"),
            InlineKeyboardButton(text="⬜ Seçimi temizle", callback_data="bq:none"),
        ])
        rows.append([InlineKeyboardButton(
            text=f"➡️ Devam ({len(selected)} klasör seçili)", callback_data="bq:next")])
        rows.append([InlineKeyboardButton(text="↩️ Tek tek yüklemeye dön", callback_data="fl:single")])
    else:
        rows.append([InlineKeyboardButton(text="📦 Toplu yükle (sıraya al)", callback_data="fl:batch")])
    rows.append([cancel_button()])

    lines = ["📦 <b>Toplu yükleme — klasörleri işaretleyin</b>" if batch
             else "📂 <b>Yüklenecek klasörü seçin</b>"]
    lines.append(f"Bekleyen: <b>{pending_total}</b> · Drive'daki toplam: <b>{total}</b>")
    lines.append("Görünüm: " + ("yalnızca bekleyenler" if filter_ == FILTER_PENDING else "tümü")
                 + (f" · arama: “{esc(query)}”" if query else ""))
    if channels:
        lines.append("İşaretler sırasıyla: " + ", ".join(esc(c) for c in channels) + " (✅ yüklendi)")
    if not visible:
        lines.append("\n<i>Bu görünümde klasör yok.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_list(callback: CallbackQuery, state: FSMContext) -> None:
    text, keyboard = _folder_list_view(await state.get_data())
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — "message is not modified" vb.
        pass


@router.message(Command("baslat"))
async def cmd_baslat(message: Message, state: FSMContext) -> None:
    if db.get_setting("bot_status") == "paused":
        await message.answer(
            "⚠️ <b>Bot şu anda durdurulmuş durumda.</b>\n"
            "Yeni yükleme başlatmak için önce /restart yazarak bota start verin."
        )
        return
    if is_busy():
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
        folders = await asyncio.to_thread(drive.list_song_folders, get_config().drive_root_folder_id)
    except (DriveError, Exception) as exc:  # noqa: BLE001
        logger.exception("Drive taraması başarısız.")
        await _safe_edit(scan_msg, f"❌ Drive taranamadı: {esc(str(exc))}")
        return

    active = db.sync_folders(folders)
    db.set_setting(LAST_SCAN_KEY, datetime.now().isoformat(timespec="seconds"))

    if not active:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Geçmişi göster", callback_data="history")
        ]])
        await scan_msg.edit_text("Drive'da yüklenebilir klasör yok.", reply_markup=keyboard)
        return

    await state.set_state(UploadFlow.choosing_folder)
    await state.update_data(fl_filter=FILTER_PENDING, fl_query="", fl_page=0,
                            fl_batch=False, bq_sel=[])
    await _safe_edit(scan_msg, f"🔍 Tarama bitti — Drive'da {len(active)} şarkı klasörü var.")
    text, keyboard = _folder_list_view(await state.get_data())
    await scan_msg.answer(text, reply_markup=keyboard)


@router.callback_query(UploadFlow.choosing_folder, F.data == "fl:noop")
async def cb_list_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(UploadFlow.choosing_folder, F.data.startswith("fl:p:"))
async def cb_list_page(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(fl_page=int(callback.data.split(":")[2]))
    await callback.answer()
    await _refresh_list(callback, state)


@router.callback_query(UploadFlow.choosing_folder, F.data == "fl:f")
async def cb_list_filter(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    new_filter = FILTER_ALL if data.get("fl_filter", FILTER_PENDING) == FILTER_PENDING else FILTER_PENDING
    await state.update_data(fl_filter=new_filter, fl_page=0)
    await callback.answer()
    await _refresh_list(callback, state)


@router.callback_query(UploadFlow.choosing_folder, F.data == "fl:x")
async def cb_list_clear_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(fl_query="", fl_page=0)
    await callback.answer("Arama temizlendi.")
    await _refresh_list(callback, state)


@router.callback_query(UploadFlow.choosing_folder, F.data == "fl:s")
async def cb_list_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UploadFlow.searching_folder)
    await callback.message.answer(
        "🔎 Klasör adında aranacak kelimeyi yazın:", reply_markup=cancel_keyboard()
    )
    await callback.answer()


@router.message(UploadFlow.searching_folder, F.text)
async def msg_list_search(message: Message, state: FSMContext) -> None:
    await state.update_data(fl_query=message.text.strip()[:60], fl_page=0)
    await state.set_state(UploadFlow.choosing_folder)
    text, keyboard = _folder_list_view(await state.get_data())
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(UploadFlow.choosing_folder, F.data.in_({"fl:batch", "fl:single"}))
async def cb_list_mode(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(fl_batch=callback.data == "fl:batch", bq_sel=[])
    await callback.answer()
    await _refresh_list(callback, state)


# ============================================================ toplu seçim

@router.callback_query(UploadFlow.choosing_folder, F.data.startswith("bqt:"))
async def cb_batch_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    folder_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("bq_sel", []))
    if folder_id in selected:
        selected.remove(folder_id)
    else:
        selected.append(folder_id)
    await state.update_data(bq_sel=selected)
    await callback.answer()
    await _refresh_list(callback, state)


@router.callback_query(UploadFlow.choosing_folder, F.data.in_({"bq:all", "bq:none"}))
async def cb_batch_select_all(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if callback.data == "bq:none":
        selected: list[str] = []
    else:
        visible = _filtered_folders(data.get("fl_filter", FILTER_PENDING), data.get("fl_query", ""))[0]
        selected = list(dict.fromkeys(list(data.get("bq_sel", [])) + [f["folder_id"] for f in visible]))
    await state.update_data(bq_sel=selected)
    await callback.answer(f"{len(selected)} klasör seçili.")
    await _refresh_list(callback, state)


def _batch_channels_view(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    chosen = set(data.get("bq_ch", []))
    rows = []
    for c in db.list_channels():
        has_image = _channel_image(c["id"]) is not None
        mark = "☑️" if c["id"] in chosen else "⬜"
        note = "" if has_image else " — görsel yok"
        rows.append([InlineKeyboardButton(text=f"{mark} {c['display_name']}{note}",
                                          callback_data=f"bqc:{c['id']}")])
    rows.append([InlineKeyboardButton(text="➡️ Devam", callback_data="bqc:go")])
    rows.append([cancel_button()])
    text = (f"📦 <b>{len(data.get('bq_sel', []))} klasör seçildi.</b>\n"
            "Hangi kanal(lar)a yüklensin? Birden fazla işaretleyebilirsiniz.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(UploadFlow.choosing_folder, F.data == "bq:next")
async def cb_batch_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("bq_sel"):
        await callback.answer("Önce en az bir klasör işaretleyin.", show_alert=True)
        return
    if not db.list_channels():
        await callback.answer("Kayıtlı kanal yok. Önce /kanallar ile kanal ekleyin.", show_alert=True)
        return
    await state.update_data(bq_ch=[])
    await state.set_state(UploadFlow.batch_channels)
    await callback.answer()
    text, keyboard = _batch_channels_view(await state.get_data())
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(UploadFlow.batch_channels, F.data.startswith("bqc:"))
async def cb_batch_channel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chosen = list(data.get("bq_ch", []))
    action = callback.data.split(":", 1)[1]
    if action != "go":
        channel_id = int(action)
        if channel_id in chosen:
            chosen.remove(channel_id)
        else:
            chosen.append(channel_id)
        await state.update_data(bq_ch=chosen)
        await callback.answer()
        text, keyboard = _batch_channels_view(await state.get_data())
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except Exception:  # noqa: BLE001
            pass
        return

    if not chosen:
        await callback.answer("En az bir kanal işaretleyin.", show_alert=True)
        return
    channels = [c for c in db.list_channels() if c["id"] in chosen]
    without_image = [c["display_name"] for c in channels if _channel_image(c["id"]) is None]
    if without_image:
        await callback.answer(
            "Bu kanal(lar) için kayıtlı görsel yok: " + ", ".join(without_image)
            + ". Önce o kanala bir kez tek tek yükleme yapın; görsel orada seçilir ve hatırlanır.",
            show_alert=True,
        )
        return

    items = _batch_items(data.get("bq_sel", []), channels)
    planned = len(data.get("bq_sel", [])) * len(channels)
    if not items:
        await callback.answer("Seçilen klasörlerin hepsi bu kanallara zaten yüklenmiş.", show_alert=True)
        return

    names = []
    for folder_id in dict.fromkeys(fid for fid, _ in items):
        folder = db.get_folder(folder_id)
        if folder:
            names.append(folder["folder_name"])
    shown = "\n".join(f"• {esc(n)}" for n in names[:15])
    more = f"\n… ve {len(names) - 15} klasör daha" if len(names) > 15 else ""
    already = planned - len(items)
    default_tags = [t["name"] for t in db.list_tags() if t["is_default"] == 1]
    text = (
        "🚦 <b>Toplu yükleme — son kontrol</b>\n\n"
        f"📁 Klasör: <b>{len(names)}</b>\n"
        f"📺 Kanal: <b>{esc(', '.join(c['display_name'] for c in channels))}</b>\n"
        f"🎬 Toplam yükleme: <b>{len(items)}</b>"
        + (f" ({already} tanesi zaten yüklü, atlanacak)" if already else "") + "\n\n"
        f"{shown}{more}\n\n"
        "Her video için: başlık = kanal öneki + klasör adı, açıklama = Word metni, "
        f"görsel = kanalın kayıtlı görseli, etiketler = {esc(', '.join(default_tags)) or '—'}.\n"
        "🌐 Görünürlük: <b>Herkese Açık</b>\n\n"
        "Kanal her videoda otomatik doğrulanır; doğrulanamayan video yüklenmez ve atlanır. "
        "Tek tek onay sorulmaz."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Sırayı başlat ({len(items)} yükleme)", callback_data="bq:start")],
        [cancel_button()],
    ])
    await state.update_data(bq_items=[[fid, cid] for fid, cid in items])
    await state.set_state(UploadFlow.batch_confirm)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard)


def _batch_items(folder_ids: list[str], channels: list[Any]) -> list[tuple[str, int]]:
    """(klasör, kanal) çiftleri — o kanala zaten yüklenmiş olanlar hariç."""
    uploads = db.get_all_folder_uploads()
    items = []
    for folder_id in folder_ids:
        for channel in channels:
            if channel["display_name"] not in uploads.get(folder_id, []):
                items.append((folder_id, channel["id"]))
    return items


@router.callback_query(UploadFlow.batch_confirm, F.data == "bq:start")
async def cb_batch_start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    items = [(fid, cid) for fid, cid in data.get("bq_items", [])]
    await state.clear()
    if is_busy():
        await callback.answer("Başka bir yükleme sürüyor; bitince tekrar deneyin.", show_alert=True)
        return
    await callback.answer("Sıra başlıyor!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    BATCH["active"] = True
    BATCH["stop"] = False
    asyncio.create_task(_run_batch(bot, callback.message.chat.id, items))


@router.callback_query(F.data == "bq:stop")
async def cb_batch_stop(callback: CallbackQuery) -> None:
    if not BATCH["active"]:
        await callback.answer("Çalışan bir sıra yok.", show_alert=True)
        return
    BATCH["stop"] = True
    await callback.answer("Şu anki yükleme bitince sıra duracak.", show_alert=True)


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


# /temizle — toplu çöpe atma. Bilerek klasör listesinin dışında, ayrı bir komut:
# yanlışlıkla basılabilecek bir düğme olmasın.

def _trash_candidates(scope: str) -> list[Any]:
    folders = db.get_active_folders()
    if scope == "all":
        return folders
    channels = db.get_all_channel_names()
    uploads = db.get_all_folder_uploads()
    return [f for f in folders if _is_complete(set(uploads.get(f["folder_id"], [])), channels)]


@router.message(Command("temizle"))
async def cmd_temizle(message: Message, state: FSMContext) -> None:
    if is_busy():
        await message.answer("⏳ Bir yükleme sürüyor; bitince tekrar deneyin.")
        return
    await state.clear()
    done = _trash_candidates("done")
    everything = _trash_candidates("all")
    if not everything:
        await message.answer("Silinecek klasör yok. (Liste /baslat ile yapılan son taramaya göredir.)")
        return
    rows = []
    if done:
        rows.append([InlineKeyboardButton(
            text=f"🧹 Yalnızca tüm kanallara yüklenmişler ({len(done)})", callback_data="trash_scope:done")])
    rows.append([InlineKeyboardButton(
        text=f"🗑️ Yüklenmemişler dahil HEPSİ ({len(everything)})", callback_data="trash_scope:all")])
    rows.append([InlineKeyboardButton(text="❌ Vazgeç", callback_data="trash_scope:cancel")])
    await message.answer(
        "🧹 <b>Drive temizliği</b>\n\n"
        "Seçilen klasörler Google Drive <b>çöp kutusuna</b> taşınır "
        "(Drive çöp kutusundan 30 gün içinde geri alınabilir).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("trash_scope:"))
async def cb_trash_scope(callback: CallbackQuery) -> None:
    scope = callback.data.split(":", 1)[1]
    await callback.answer()
    if scope == "cancel":
        await callback.message.edit_text("❌ Temizlik yapılmadı.")
        return
    folders = _trash_candidates(scope)
    if not folders:
        await callback.message.edit_text("Silinecek klasör kalmamış.")
        return
    what = "yüklenmemişler dahil <b>bütün</b> klasörleri" if scope == "all" \
        else "tüm kanallara yüklenmiş klasörleri"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Evet, {len(folders)} klasörü taşı", callback_data=f"trash_go:{scope}"),
        InlineKeyboardButton(text="❌ Vazgeç", callback_data="trash_scope:cancel"),
    ]])
    await callback.message.edit_text(
        f"⚠️ Drive'daki {what} (<b>{len(folders)} adet</b>) çöp kutusuna taşımak "
        "istediğinize emin misiniz?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("trash_go:"))
async def cb_trash_go(callback: CallbackQuery) -> None:
    scope = callback.data.split(":", 1)[1]
    if is_busy():
        await callback.answer("Bir yükleme sürüyor; bitince tekrar deneyin.", show_alert=True)
        return
    folders = _trash_candidates(scope)
    await callback.answer("Taşınıyor...")
    await callback.message.edit_text(f"⏳ {len(folders)} klasör çöp kutusuna taşınıyor...")

    success_count = 0
    fail_count = 0
    for f in folders:
        try:
            await asyncio.to_thread(drive.trash_file, f["folder_id"])
            db.delete_folder(f["folder_id"])
            RETRY_JOBS.pop(f["folder_id"], None)
            success_count += 1
        except Exception:  # noqa: BLE001
            logger.exception("Klasör silinemedi: %s", f["folder_name"])
            fail_count += 1

    await callback.message.answer(
        f"🧹 Temizlik tamamlandı:\n✅ {success_count} klasör çöp kutusuna taşındı."
        + (f"\n❌ {fail_count} klasör taşınamadı." if fail_count else "")
    )


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


def _channel_image(channel_id: int) -> str | None:
    """Kanalın hatırlanan görseli (dosya hâlâ duruyorsa)."""
    saved_path = db.get_setting(_channel_image_key(channel_id))
    return saved_path if saved_path and Path(saved_path).exists() else None


def _suggest_title(channel_id: int | None, folder_name: str) -> tuple[str, bool]:
    """Önerilen başlık (kanal öneki + klasör adı) ve kırpılıp kırpılmadığı."""
    prefix = db.get_setting(f"title_prefix:{channel_id}") if channel_id else None
    full = f"{prefix} - {folder_name}" if prefix else folder_name
    return full[:TITLE_MAX], len(full) > TITLE_MAX


async def _load_description(folder_id: str, docx_file: dict[str, Any]) -> tuple[str, bool]:
    """Word dosyasını indirip açıklama metnini çıkarır; (metin, kırpıldı mı) döner."""
    docx_path = get_config().temp_dir / f"{folder_id}.docx"
    try:
        await asyncio.to_thread(drive.download_file, docx_file, docx_path)
        text = await asyncio.to_thread(extract_text, docx_path)
    finally:
        if docx_path.exists():
            try:
                docx_path.unlink()
            except OSError:
                pass
    return text[:DESCRIPTION_MAX], len(text) > DESCRIPTION_MAX


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
    saved_path = _channel_image(channel["id"])
    if saved_path:
        await state.set_state(UploadFlow.confirming_channel_image)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Hızlı yükle (bu görsel + varsayılanlar)",
                                  callback_data="chimg:quick")],
            [InlineKeyboardButton(text="✅ Bu görselle, adım adım", callback_data="chimg:keep")],
            [InlineKeyboardButton(text="🔄 Farklı görsel seç", callback_data="chimg:change")],
            [cancel_button()],
        ])
        try:
            await callback.message.answer_photo(
                FSInputFile(Path(saved_path)),
                caption=(f"📺 Kanal: <b>{esc(channel['display_name'])}</b> ✅\n\n"
                         "🖼 Bu kanalın kayıtlı görseli bu.\n"
                         "⚡ <b>Hızlı yükle</b>: etiket, başlık ve açıklama varsayılanlarla "
                         "hazırlanır; doğrudan son kontrol kartına gidilir."),
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
    saved_path = _channel_image(data["channel_id"])
    if not saved_path:
        await callback.answer("Görsel artık yok, yeniden seçin.", show_alert=True)
        await _ask_image_source(callback.message, state)
        return
    await state.update_data(image_path=saved_path, image_is_temp=False)
    await callback.answer("Kanal görseliyle devam ediliyor.")
    await _ask_tags(callback.message, state)


@router.callback_query(UploadFlow.confirming_channel_image, F.data == "chimg:quick")
async def cb_channel_quick(callback: CallbackQuery, state: FSMContext) -> None:
    """Hızlı yükleme: kayıtlı görsel + varsayılan etiketler + önerilen başlık + Word metni."""
    data = await state.get_data()
    saved_path = _channel_image(data["channel_id"])
    if not saved_path:
        await callback.answer("Görsel artık yok, yeniden seçin.", show_alert=True)
        await _ask_image_source(callback.message, state)
        return
    await callback.answer("Hazırlanıyor...")
    reading_msg = await callback.message.answer("📄 Word dosyası okunuyor...")
    try:
        description, _ = await _load_description(data["folder_id"], data["docx_file"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Açıklama metni çıkarılamadı.")
        await _safe_edit(reading_msg, f"❌ Word dosyası okunamadı: {esc(str(exc))}")
        await state.clear()
        return
    await _safe_edit(reading_msg, "📄 Word dosyası okundu ✅")
    title, _ = _suggest_title(data.get("channel_id"), data["folder_name"])
    await state.update_data(
        image_path=saved_path,
        image_is_temp=False,
        selected_tags=[t["name"] for t in db.list_tags() if t["is_default"] == 1],
        title=title,
        description=description,
    )
    await _final_confirm(callback.message, state)


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
    suggested, was_cut = _suggest_title(data.get("channel_id"), data["folder_name"])
    await state.update_data(title=suggested)
    await state.set_state(UploadFlow.confirming_title)
    note = ""
    if was_cut:
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

    try:
        text, was_cut = await _load_description(data["folder_id"], data["docx_file"])
    except (DriveError, DocxError, Exception) as exc:  # noqa: BLE001
        logger.exception("Açıklama metni çıkarılamadı.")
        await _safe_edit(reading_msg, f"❌ Word dosyası okunamadı: {esc(str(exc))}")
        await state.clear()
        return

    truncated_note = ""
    if was_cut:
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
        f"🏷️ Etiketler: {esc(', '.join(data.get('selected_tags') or [])) or '—'}\n"
        f"📝 Açıklama: {esc(description[:200])}{'…' if len(description) > 200 else ''}\n"
        f"🌐 Görünürlük: <b>Herkese Açık</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Yükle", callback_data="upload:go"), cancel_button()],
        [InlineKeyboardButton(text="✏️ Etiket / başlık / açıklamayı düzenle", callback_data="upload:edit")],
    ])
    try:
        await message.answer_photo(FSInputFile(Path(data["image_path"])),
                                   caption=caption, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — görsel gönderilemezse metinle devam
        logger.exception("Özet kartındaki görsel gönderilemedi.")
        await message.answer(caption, reply_markup=keyboard)


@router.callback_query(UploadFlow.final_confirm, F.data == "upload:edit")
async def cb_upload_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Son karttan geri dönüp etiket → başlık → açıklama adımlarını tek tek gözden geçirir."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await _ask_tags(callback.message, state)


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
    if is_busy():
        await callback.answer("Başka bir yükleme sürüyor, bitince tekrar deneyin.", show_alert=True)
        return
    await callback.answer("Tekrar deneniyor...")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    asyncio.create_task(_run_pipeline(bot, job))


# ============================================================ pipeline

async def _run_pipeline(bot: Bot, job: UploadJob, *, interactive: bool = True) -> str:
    """İndir → ffmpeg → Studio yükle. Hata durumunda mp4 saklanır, retry sunulur.

    Args:
        interactive: False ise (toplu sıra) kanal için ekran görüntülü onay ve
            "çöpe at" sorusu sorulmaz; kanalı uploader kendisi doğrular.

    Returns:
        ``"ok"``, ``"failed"``, ``"session"`` (Google oturumu düşmüş) veya ``"busy"``.
    """
    cfg = get_config()
    if UPLOAD_LOCK.locked():
        await bot.send_message(job.chat_id, "⏳ Başka bir yükleme sürüyor; bitince tekrar deneyin.")
        return "busy"

    result = "failed"
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
                    on_channel_confirm=on_channel_confirm if interactive else None,
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

            result = "ok"

            # Çöpe atma sorgusu (toplu sırada sorulmaz; /temizle ile topluca yapılır)
            if interactive:
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
            result = "session"
            await _handle_failure(bot, job, str(exc),
                                  header="🔑 Google oturumu düşmüş. /giris ile yeniden giriş yapın.")
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
    return result


# ============================================================ toplu sıra

async def _build_default_job(chat_id: int, folder: Any, channel: Any) -> UploadJob:
    """Toplu sıra için varsayılanlarla iş kurar.

    Raises:
        ValueError: Görsel ya da klasör dosyaları eksikse (Türkçe açıklamayla).
    """
    image_path = _channel_image(channel["id"])
    if not image_path:
        raise ValueError(f"'{channel['display_name']}' kanalının kayıtlı görseli yok.")
    files = await asyncio.to_thread(drive.list_files, folder["folder_id"])
    audio, docx = drive.find_audio_and_docx(files)
    if audio is None or docx is None:
        missing = " ve ".join(n for n, f in (("ses", audio), ("Word", docx)) if f is None)
        raise ValueError(f"'{folder['folder_name']}' klasöründe {missing} dosyası yok.")
    description, _ = await _load_description(folder["folder_id"], docx)
    title, _ = _suggest_title(channel["id"], folder["folder_name"])
    return UploadJob(
        chat_id=chat_id,
        folder_id=folder["folder_id"],
        folder_name=folder["folder_name"],
        channel_name=channel["display_name"],
        image_path=Path(image_path),
        image_is_temp=False,
        title=title,
        description=description,
        audio_file=audio,
        tags=[t["name"] for t in db.list_tags() if t["is_default"] == 1],
    )


async def _run_batch(bot: Bot, chat_id: int, items: list[tuple[str, int]]) -> None:
    """Seçilen (klasör, kanal) çiftlerini sırayla yükler; sonunda özet gönderir."""
    total = len(items)
    ok = failed = skipped = 0
    stopped_because = ""
    stop_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏹ Sırayı durdur", callback_data="bq:stop")
    ]])
    try:
        await bot.send_message(
            chat_id,
            f"📦 <b>Toplu yükleme başladı</b> — {total} yükleme sırada.\n"
            "Her biri bitince linki gönderilecek.",
            reply_markup=stop_keyboard,
        )
        for index, (folder_id, channel_id) in enumerate(items, start=1):
            if BATCH["stop"]:
                stopped_because = "Sıra isteğiniz üzerine durduruldu."
                break
            folder = db.get_folder(folder_id)
            channel = db.get_channel(channel_id)
            if folder is None or channel is None:
                skipped += 1
                continue
            if channel["display_name"] in db.get_uploaded_channels_for_folder(folder_id):
                skipped += 1
                continue

            await bot.send_message(
                chat_id,
                f"📦 <b>{index}/{total}</b> — {esc(folder['folder_name'])} → "
                f"{esc(channel['display_name'])}",
            )
            try:
                job = await _build_default_job(chat_id, folder, channel)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Toplu sırada iş kurulamadı: %s", folder["folder_name"])
                failed += 1
                await bot.send_message(chat_id, f"⚠️ Atlandı: {esc(str(exc))}")
                continue

            result = await _run_pipeline(bot, job, interactive=False)
            if result == "ok":
                ok += 1
            else:
                failed += 1
                if result == "session":
                    stopped_because = "Google oturumu düştüğü için sıra durduruldu."
                    break
    except Exception:  # noqa: BLE001
        logger.exception("Toplu yükleme sırası beklenmedik biçimde durdu.")
        stopped_because = "Beklenmedik bir hata yüzünden sıra durdu."
    finally:
        BATCH["active"] = False
        BATCH["stop"] = False

    remaining = total - ok - failed - skipped
    lines = ["📦 <b>Toplu yükleme bitti</b>" if not stopped_because
             else f"📦 <b>Toplu yükleme durdu</b> — {stopped_because}"]
    lines.append(f"✅ Yayınlanan: {ok}")
    if failed:
        lines.append(f"❌ Başarısız: {failed} (yukarıdaki 🔁 Tekrar dene düğmeleriyle yeniden denenebilir)")
    if skipped:
        lines.append(f"⏭ Atlanan (zaten yüklü): {skipped}")
    if remaining > 0:
        lines.append(f"⏸ Başlanmayan: {remaining}")
    try:
        await bot.send_message(chat_id, "\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.exception("Toplu yükleme özeti gönderilemedi.")


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
