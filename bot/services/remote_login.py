"""Telegram'dan başlatılan uzaktan Google girişi (yalnızca sunucu/kapsayıcı).

Sanal ekranı (Xvfb, ``DISPLAY``) geçici olarak noVNC ile yayınlar ve kalıcı
tarayıcı profiliyle YouTube Studio'yu açar. Kullanıcı tarayıcıdan bağlanıp
Google'a giriş yapar; oturum bitince yayın kapatılır.

Gerekenler: ``x11vnc`` + ``websockify`` (imajda kurulu) ve dışarıdan erişilen
adresi veren ``VNC_URL`` ortam değişkeni (Dokploy'da bu servisin 6080 portuna
bağlanan alan adı, ör. ``https://ornek.sslip.io/vnc.html``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import string
import sys

from playwright.async_api import BrowserContext, Page, async_playwright

from ..config import Config
from .studio_uploader import (
    STUDIO_URL,
    _clear_profile_locks,
    _is_login_url,
    _launch_kwargs,
)

logger = logging.getLogger(__name__)

SESSION_MINUTES = 15
_VNC_PORT = 5900
_WEB_PORT = 6080
_NOVNC_DIR = "/usr/share/novnc"


def vnc_url() -> str:
    return os.getenv("VNC_URL", "").strip()


def unavailable_reason() -> str | None:
    """Uzaktan giriş bu ortamda yapılamıyorsa nedenini (Türkçe) döndürür."""
    if not sys.platform.startswith("linux"):
        return ("Uzaktan giriş yalnızca sunucuda çalışır. Bu bilgisayarda "
                "<code>google_giris.bat</code> ile giriş yapın.")
    if not (shutil.which("x11vnc") and shutil.which("websockify")):
        return "Sunucuda x11vnc / websockify kurulu değil (imaj eski olabilir)."
    if not vnc_url():
        return ("<code>VNC_URL</code> ayarı tanımlı değil. Dokploy → Environment'a, bu servisin "
                "6080 portuna bağlı alan adını yazın, ör. "
                "<code>VNC_URL=https://ALAN-ADI/vnc.html</code>, sonra yeniden dağıtın.")
    return None


def _new_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))  # VNC parolası en çok 8 karakter


class RemoteLoginSession:
    """Tek bir uzaktan giriş oturumu: yayın + tarayıcı. ``run()`` bitince her şey kapanır."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self.password = _new_password()
        self.external_vnc = os.getenv("ENABLE_VNC", "").strip().lower() == "true"
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self._done = asyncio.Event()
        self._procs: list[asyncio.subprocess.Process] = []
        self._context: BrowserContext | None = None
        self.logged_in = False

    def finish(self) -> None:
        """Oturumu bitirir (kullanıcı 'Giriş tamam' dedi)."""
        self._done.set()

    async def type_text(self, text: str) -> bool:
        """Metni uzak tarayıcıda odaktaki alana yazar (klavye düzeni sorunlarına karşı)."""
        page = self._active_page()
        if page is None:
            return False
        await page.keyboard.insert_text(text)
        return True

    def _active_page(self) -> Page | None:
        if self._context is None or not self._context.pages:
            return None
        return self._context.pages[-1]

    async def _start_vnc(self) -> None:
        if self.external_vnc:
            return  # entrypoint zaten yayınlıyor (ENABLE_VNC=true); parola panelden gelir
        display = os.getenv("DISPLAY", ":99")
        x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", display, "-forever", "-shared", "-localhost",
            "-rfbport", str(_VNC_PORT), "-passwd", self.password, "-quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self._procs.append(x11vnc)
        web = await asyncio.create_subprocess_exec(
            "websockify", "--web", _NOVNC_DIR, str(_WEB_PORT), f"localhost:{_VNC_PORT}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self._procs.append(web)
        await asyncio.sleep(1.5)
        for proc in self._procs:
            if proc.returncode is not None:
                raise RuntimeError("Ekran yayını başlatılamadı (x11vnc/websockify hemen kapandı).")

    async def _stop_vnc(self) -> None:
        for proc in self._procs:
            if proc.returncode is None:
                proc.terminate()
        for proc in self._procs:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        self._procs.clear()

    async def run(self) -> None:
        """Yayını ve tarayıcıyı açar; 'bitti' denene ya da süre dolana kadar bekler."""
        try:
            await self._start_vnc()
            _clear_profile_locks(self._cfg)
            async with async_playwright() as pw:
                kwargs = _launch_kwargs(self._cfg)
                kwargs["headless"] = False  # giriş her zaman görünür tarayıcıda yapılır
                kwargs.pop("user_agent", None)
                self._context = await pw.chromium.launch_persistent_context(**kwargs)
                try:
                    page = self._context.pages[0] if self._context.pages else await self._context.new_page()
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
            await self._stop_vnc()

    async def _check_logged_in(self) -> bool:
        page = self._active_page()
        if page is None:
            return False
        try:
            await page.goto(STUDIO_URL, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            return "studio.youtube.com" in page.url and not _is_login_url(page.url)
        except Exception:  # noqa: BLE001
            return False
