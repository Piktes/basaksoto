"""Handler'ların ortak yardımcıları."""

from __future__ import annotations

import html
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..services.studio_uploader import UPLOAD_LOCK

CANCEL_CB = "flow:cancel"

# Toplu yükleme sırası: "active" sürerken yeni iş başlatılmaz; "stop" istenirse
# sıradaki klasöre geçilmeden durulur.
BATCH: dict[str, bool] = {"active": False, "stop": False}


def is_busy() -> bool:
    """Tarayıcı şu an bir işte mi (yükleme, toplu sıra, uzaktan giriş, kanal okuma)?"""
    return UPLOAD_LOCK.locked() or BATCH["active"]


def esc(text: str) -> str:
    """HTML parse mode için kullanıcı metnini kaçışlar."""
    return html.escape(text or "")


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="❌ İptal", callback_data=CANCEL_CB)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cancel_button()]])


def fmt_date(iso: str | None, fmt: str = "%d.%m.%Y") -> str:
    """ISO tarih metnini Türkçe kısa biçime çevirir (bozuksa olduğu gibi döner)."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(fmt)
    except ValueError:
        return iso
