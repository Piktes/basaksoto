"""Sunucudaki sanal ekranın (Xvfb) geçici olarak noVNC ile yayınlanması.

İki yerde kullanılır: ``/giris`` (Google'a yeniden giriş) ve yükleme sırasında
Google'ın "Kimliğinizi doğrulayın" adımı (kullanıcı aynı tarayıcıda kendisi geçer).

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
from typing import Any, Callable

logger = logging.getLogger(__name__)

_VNC_PORT = 5900
_WEB_PORT = 6080
_NOVNC_DIR = "/usr/share/novnc"

# Ekran yayındayken sohbete yazılan metnin gideceği tarayıcı sayfası:
# chat_id → o anki sayfayı döndüren fonksiyon (bkz. handlers/giris.py).
TYPE_TARGETS: dict[int, Callable[[], Any]] = {}


def url() -> str:
    return os.getenv("VNC_URL", "").strip()


def unavailable_reason() -> str | None:
    """Ekran bu ortamda yayınlanamıyorsa nedenini (Türkçe) döndürür."""
    if not sys.platform.startswith("linux"):
        return ("Uzak ekran yalnızca sunucuda çalışır. Bu bilgisayarda tarayıcı zaten "
                "ekranınızda açılır; giriş için <code>google_giris.bat</code> kullanın.")
    if not (shutil.which("x11vnc") and shutil.which("websockify")):
        return "Sunucuda x11vnc / websockify kurulu değil (imaj eski olabilir)."
    if not url():
        return ("<code>VNC_URL</code> ayarı tanımlı değil. Dokploy → Environment'a, bu servisin "
                "6080 portuna bağlı alan adını yazın, ör. "
                "<code>VNC_URL=https://ALAN-ADI/vnc.html</code>, sonra yeniden dağıtın.")
    return None


def _new_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))  # VNC parolası en çok 8 karakter


class Broadcast:
    """``async with Broadcast() as b:`` süresince ekranı yayınlar; çıkışta kapatır.

    Her yayın için yeni, tek kullanımlık bir parola üretilir (``b.password``).
    Entrypoint ekranı zaten yayınlıyorsa (``ENABLE_VNC=true``) yeni süreç
    başlatılmaz; ``b.external`` True olur ve parola panelde tanımlı olandır.
    """

    def __init__(self) -> None:
        self.password = _new_password()
        self.external = os.getenv("ENABLE_VNC", "").strip().lower() == "true"
        self._procs: list[asyncio.subprocess.Process] = []

    @property
    def password_line(self) -> str:
        if self.external:
            return "Parola: Dokploy'daki <code>VNC_PASSWORD</code> değeri"
        return f"Parola: <code>{self.password}</code>"

    async def __aenter__(self) -> "Broadcast":
        if self.external:
            return self
        try:
            display = os.getenv("DISPLAY", ":99")
            self._procs.append(await asyncio.create_subprocess_exec(
                "x11vnc", "-display", display, "-forever", "-shared", "-localhost",
                "-rfbport", str(_VNC_PORT), "-passwd", self.password, "-quiet",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            ))
            self._procs.append(await asyncio.create_subprocess_exec(
                "websockify", "--web", _NOVNC_DIR, str(_WEB_PORT), f"localhost:{_VNC_PORT}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            ))
            await asyncio.sleep(1.5)
            if any(proc.returncode is not None for proc in self._procs):
                raise RuntimeError("Ekran yayını başlatılamadı (x11vnc/websockify hemen kapandı).")
        except BaseException:
            await self._stop()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stop()

    async def _stop(self) -> None:
        for proc in self._procs:
            if proc.returncode is None:
                proc.terminate()
        for proc in self._procs:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        self._procs.clear()
