"""Telegram komut ve callback handler'ları."""

from aiogram import Router

from . import baslat, gorsel, istatistik, kanallar, start, guncelle


def all_routers() -> list[Router]:
    """Dispatcher'a kaydedilecek tüm router'ları döndürür."""
    return [
        start.router,
        baslat.router,
        istatistik.router,
        gorsel.router,
        kanallar.router,
        guncelle.router,
    ]
