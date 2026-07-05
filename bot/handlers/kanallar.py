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


def _prefix_key(channel_id: int) -> str:
    return f"title_prefix:{channel_id}"


def _channels_view() -> tuple[str, InlineKeyboardMarkup]:
    channels = db.list_channels()
    if channels:
        lines = []
        for c in channels:
            prefix = db.get_setting(_prefix_key(c["id"]))
            lines.append(f"• {esc(c['display_name'])}\n   ↳ başlık öneki: "
                         f"{esc(prefix) if prefix else '—'}")
        text = ("📺 <b>Kayıtlı kanallar:</b>\n" + "\n".join(lines) +
                "\n\nBaşlık öneki, şarkı adının başına otomatik eklenir "
                "(örn: <i>Osman BAYKARA - Şarkı Adı</i>).")
    else:
        text = ("📺 Henüz kanal kayıtlı değil.\n\n"
                "Studio'daki <b>görünen kanal adını</b> birebir yazarak ekleyin — "
                "Playwright bu ada göre kanal değiştirir.")
    rows = []
    for c in channels:
        rows.append([
            InlineKeyboardButton(text=f"✏️ Önek: {c['display_name'][:20]}",
                                 callback_data=f"chpre:{c['id']}"),
            InlineKeyboardButton(text="🗑 Sil", callback_data=f"chdel:{c['id']}"),
        ])
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


@router.callback_query(F.data.startswith("chpre:"))
async def cb_set_prefix(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id = int(callback.data.split(":", 1)[1])
    channel = db.get_channel(channel_id)
    if channel is None:
        await callback.answer("Kanal bulunamadı.", show_alert=True)
        return
    await state.set_state(ChannelFlow.waiting_prefix)
    await state.update_data(prefix_channel_id=channel_id)
    current = db.get_setting(_prefix_key(channel_id))
    current_line = f"\nMevcut önek: <code>{esc(current)}</code>" if current else ""
    await callback.message.answer(
        f"<b>{esc(channel['display_name'])}</b> için başlık önekini yazın "
        f"(örn: <code>Osman BAYKARA</code>).{current_line}\n"
        "Öneki kaldırmak için <b>sil</b> yazın."
    )
    await callback.answer()


@router.message(ChannelFlow.waiting_prefix, F.text)
async def msg_prefix(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    channel_id = data["prefix_channel_id"]
    text = message.text.strip()
    if text.lower() == "sil":
        db.set_setting(_prefix_key(channel_id), "")
        await message.answer("✅ Başlık öneki kaldırıldı.")
    else:
        db.set_setting(_prefix_key(channel_id), text)
        await message.answer(
            f"✅ Başlık öneki kaydedildi. Yeni videolar şöyle önerilecek:\n"
            f"<b>{esc(text)} - Şarkı Adı</b>"
        )
    await state.clear()
    view_text, keyboard = _channels_view()
    await message.answer(view_text, reply_markup=keyboard)


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
