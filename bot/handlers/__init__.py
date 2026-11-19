"""Telegram komut ve callback handler'ları."""

from aiogram import Router

from . import baslat, giris, gorsel, guncelle, istatistik, kanallar, start, yedek


def all_routers() -> list[Router]:
    """Dispatcher'a kaydedilecek tüm router'ları döndürür.

    Sıra önemlidir: ``giris`` oturumu sürerken sohbete yazılan metin, başka bir
    handler'a düşmeden uzak tarayıcıya gitmelidir.
    """
    return [
        start.router,
        giris.router,
        baslat.router,
        istatistik.router,
        gorsel.router,
        kanallar.router,
        yedek.router,
        guncelle.router,
    ]
