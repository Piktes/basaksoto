"""Sunucuda Google oturumu açma modu (``LOGIN_MODE=true``).

Kalıcı tarayıcı profiliyle YouTube Studio'yu sanal ekranda açar ve kapsayıcı
durdurulana kadar açık tutar. Kullanıcı noVNC üzerinden elle giriş yapar;
kapsayıcı durdurulunca tarayıcı düzgün kapatılır ve oturum profile yazılır.
"""

from __future__ import annotations

import asyncio
import signal

from playwright.async_api import async_playwright

from bot.config import load_config

STUDIO_URL = "https://studio.youtube.com"


async def main() -> None:
    cfg = load_config()
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_file = cfg.browser_profile_dir / lock_name
        if lock_file.exists() or lock_file.is_symlink():
            lock_file.unlink(missing_ok=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
        print("Giriş modu: tarayıcı açık. noVNC'den Google'a giriş yapın.", flush=True)
        await stop.wait()
        await context.close()
    print("Oturum profili kaydedildi:", cfg.browser_profile_dir, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
