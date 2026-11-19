"""/giris — Google oturumu düştüğünde Telegram'dan uzaktan giriş başlatır.

Sunucudaki tarayıcı ekranı geçici olarak yayınlanır; bağlantı ve tek kullanımlık
parola bu sohbete gönderilir. Oturum sürerken bu sohbete yazılan metin uzak
tarayıcıda odaktaki alana yazılır (uzak klavyede çıkmayan @ gibi karakterler için).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import get_config
from ..services import remote_login
from ..services.studio_uploader import UPLOAD_LOCK
from .common import esc, is_busy

logger = logging.getLogger(__name__)

router = Router(name="giris")

# Süren oturum (aynı anda en çok bir tane): chat_id → oturum
_ACTIVE: dict[int, remote_login.RemoteLoginSession] = {}


def _has_active_session(message: Message) -> bool:
    return message.chat.id in _ACTIVE


@router.message(Command("giris"))
async def cmd_giris(message: Message, bot: Bot) -> None:
    reason = remote_login.unavailable_reason()
    if reason:
        await message.answer(f"⚠️ {reason}")
        return
    if _ACTIVE:
        await message.answer("ℹ️ Zaten açık bir giriş oturumu var.")
        return
    if is_busy():
        await message.answer("⏳ Bir yükleme sürüyor; bitince tekrar /giris yazın.")
        return
    asyncio.create_task(_run_session(bot, message.chat.id))


async def _run_session(bot: Bot, chat_id: int) -> None:
    session = remote_login.RemoteLoginSession(get_config())
    async with UPLOAD_LOCK:  # tarayıcı profili bu sırada başka işte kullanılamaz
        _ACTIVE[chat_id] = session
        runner = asyncio.create_task(session.run())
        try:
            await session.ready.wait()
            if session.failed:
                await bot.send_message(chat_id, f"❌ Giriş ekranı açılamadı: {esc(session.failed)}")
                return
            password_line = (
                "Parola: Dokploy'daki <code>VNC_PASSWORD</code> değeri" if session.external_vnc
                else f"Parola: <code>{session.password}</code>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Giriş tamam, kapat", callback_data="giris:done")
            ]])
            await bot.send_message(
                chat_id,
                "🔑 <b>Uzaktan giriş açıldı</b>\n\n"
                f"1. Şu adresi açıp <b>Connect</b>'e basın:\n{esc(remote_login.vnc_url())}\n"
                f"2. {password_line}\n"
                "3. Açılan tarayıcıda Google hesabına giriş yapın ve YouTube Studio'yu görün.\n"
                "4. Bitince aşağıdaki düğmeye basın.\n\n"
                "⌨️ Uzak ekranda yazamadığınız bir metni (ör. <code>@</code> içeren e-posta) "
                "bu sohbete yazın; tarayıcıda seçili kutuya yazılır ve mesajınız silinir.\n\n"
                f"Ekran {remote_login.SESSION_MINUTES} dakika sonra kendiliğinden kapanır.",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            await runner
        finally:
            _ACTIVE.pop(chat_id, None)
            if not runner.done():
                session.finish()
                await runner

    if session.failed:
        await bot.send_message(chat_id, f"❌ Giriş oturumu hata ile kapandı: {esc(session.failed)}")
    elif session.logged_in:
        await bot.send_message(
            chat_id, "✅ Google oturumu açık ve kaydedildi. Ekran kapatıldı; /baslat ile devam edebilirsiniz."
        )
    else:
        await bot.send_message(
            chat_id,
            "⚠️ Ekran kapatıldı ama YouTube Studio'ya giriş yapılmış görünmüyor. "
            "Tekrar denemek için /giris yazın.",
        )


@router.callback_query(F.data == "giris:done")
async def cb_giris_done(callback: CallbackQuery) -> None:
    session = _ACTIVE.get(callback.message.chat.id)
    if session is None:
        await callback.answer("Açık bir giriş oturumu yok.", show_alert=True)
        return
    await callback.answer("Oturum kaydediliyor, ekran kapatılıyor...")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    session.finish()


@router.message(_has_active_session, F.text, ~F.text.startswith("/"))
async def msg_type_into_remote(message: Message) -> None:
    """Giriş oturumu sürerken gelen metni uzak tarayıcıya yazar, mesajı sohbetten siler."""
    session = _ACTIVE.get(message.chat.id)
    typed = False
    if session is not None:
        try:
            typed = await session.type_text(message.text)
        except Exception:  # noqa: BLE001
            logger.exception("Metin uzak tarayıcıya yazılamadı.")
    try:
        await message.delete()  # parola olabilir; sohbet geçmişinde kalmasın
    except Exception:  # noqa: BLE001
        logger.warning("Kullanıcı mesajı silinemedi.")
    await message.answer(
        "⌨️ Yazıldı." if typed else "⚠️ Yazılamadı — uzak ekranda önce bir kutuya tıklayın."
    )
