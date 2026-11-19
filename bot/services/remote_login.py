"""Telegram'dan başlatılan uzaktan Google girişi (yalnızca sunucu/kapsayıcı).

Sanal ekranı geçici olarak yayınlar (bkz. ``vnc.py``) ve kalıcı tarayıcı
profiliyle YouTube Studio'yu açar. Kullanıcı tarayıcıdan bağlanıp Google'a
giriş yapar; oturum bitince yayın kapatılır.
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import BrowserContext, Page, async_playwright

from ..config import Config
from . import vnc
from .studio_uploader import (
    STUDIO_URL,
    _clear_profile_locks,
    _is_login_url,
    _launch_kwargs,
)

logger = logging.getLogger(__name__)

SESSION_MINUTES = 15

unavailable_reason = vnc.unavailable_reason
vnc_url = vnc.url


class RemoteLoginSession:
    """Tek bir uzaktan giriş oturumu: yayın + tarayıcı. ``run()`` bitince her şey kapanır."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self.broadcast = vnc.Broadcast()
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self._done = asyncio.Event()
        self._context: BrowserContext | None = None
        self.logged_in = False

    def finish(self) -> None:
        """Oturumu bitirir (kullanıcı 'Giriş tamam' dedi)."""
        self._done.set()

    def active_page(self) -> Page | None:
        if self._context is None or not self._context.pages:
            return None
        return self._context.pages[-1]

    async def run(self) -> None:
        """Yayını ve tarayıcıyı açar; 'bitti' denene ya da süre dolana kadar bekler."""
        try:
            async with self.broadcast:
                _clear_profile_locks(self._cfg)
                async with async_playwright() as pw:
                    kwargs = _launch_kwargs(self._cfg)
                    kwargs["headless"] = False  # giriş her zaman görünür tarayıcıda yapılır
                    kwargs.pop("user_agent", None)
                    self._context = await pw.chromium.launch_persistent_context(**kwargs)
                    try:
                        page = (self._context.pages[0] if self._context.pages
                                else await self._context.new_page())
                        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
                        self.ready.set()
                        try:
                            await asyncio.wait_for(self._done.wait(), timeout=SESSION_MINUTES * 60)
                        except asyncio.TimeoutError:
                            logger.info("Uzaktan giriş süresi doldu.")
                        self.logged_in = await self._check_logged_in()
                    finally:
                        await self._context.close()  # profil (oturum) diske yazılır
                        self._context = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Uzaktan giriş oturumu başarısız.")
            self.failed = str(exc)
        finally:
            self.ready.set()

    async def _check_logged_in(self) -> bool:
        page = self.active_page()
        if page is None:
            return False
        try:
            await page.goto(STUDIO_URL, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            return "studio.youtube.com" in page.url and not _is_login_url(page.url)
        except Exception:  # noqa: BLE001
            return False
