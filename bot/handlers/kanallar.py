"""/kanallar — YouTube kanal görünen adlarının yönetimi.

Playwright, buradaki görünen ada göre Studio'da kanal değiştirir; ad
Studio'daki kanal adıyla birebir aynı olmalıdır.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..services import db
from ..states import ChannelFlow
from .common import esc

router = Router(name="kanallar")


def _channels_view() -> tuple[str, InlineKeyboardMarkup]:
    channels = db.list_channels()
    if channels:
        lines = "\n".join(f"• {esc(c['display_name'])}" for c in channels)
        text = f"📺 <b>Kayıtlı kanallar:</b>\n{lines}\n\nSilmek için butonları kullanın."
    else:
        text = ("📺 Henüz kanal kayıtlı değil.\n\n"
                "Studio'daki <b>görünen kanal adını</b> birebir yazarak ekleyin — "
                "Playwright bu ada göre kanal değiştirir.")
    rows = [[InlineKeyboardButton(text=f"🗑 {c['display_name']}", callback_data=f"chdel:{c['id']}")]
            for c in channels]
    rows.append([InlineKeyboardButton(text="➕ Kanal ekle", callback_data="chadd")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("kanallar"))
async def cmd_kanallar(message: Message) -> None:
    text, keyboard = _channels_view()
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "chadd")
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelFlow.waiting_name)
    await callback.message.answer(
        "Kanalın <b>Studio'daki görünen adını</b> yazın (birebir aynı olmalı):"
    )
    await callback.answer()


@router.message(ChannelFlow.waiting_name, F.text)
async def msg_channel_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await message.answer("Kanal adı boş olamaz, tekrar yazın:")
        return
    if db.add_channel(name):
        await message.answer(f"✅ Kanal eklendi: <b>{esc(name)}</b>")
    else:
        await message.answer(f"ℹ️ <b>{esc(name)}</b> zaten kayıtlı.")
    await state.clear()
    text, keyboard = _channels_view()
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("chdel:"))
async def cb_delete_channel(callback: CallbackQuery) -> None:
    channel_id = int(callback.data.split(":", 1)[1])
    channel = db.get_channel(channel_id)
    if channel:
        db.delete_channel(channel_id)
        await callback.answer(f"Silindi: {channel['display_name']}")
    else:
        await callback.answer("Kanal zaten silinmiş.")
    text, keyboard = _channels_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
