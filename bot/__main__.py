"""Giriş noktası: ``python -m bot`` (normal mod) / ``python -m bot --login-setup``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import Config, ConfigError, load_config
from .handlers import all_routers
from .middlewares.auth import WhitelistMiddleware
from .services import db
from .services.studio_uploader import login_setup
from .services.video import VideoError, ensure_ffmpeg

logger = logging.getLogger(__name__)

_TEMP_MAX_AGE_DAYS = 7

_BOT_COMMANDS = [
    BotCommand(command="baslat", description="Drive'ı tara, video yükle"),
    BotCommand(command="istatistik", description="Yükleme raporları"),
    BotCommand(command="gorseller", description="Görsel kütüphanesi"),
    BotCommand(command="kanallar", description="Kanal adlarını yönet"),
    BotCommand(command="guncelle", description="Botu güncelle ve yeniden başlat"),
    BotCommand(command="durdur", description="Botu geçici olarak durdur"),
    BotCommand(command="restart", description="Botu aktifleştir ve yeniden başlat"),
    BotCommand(command="yardim", description="Yardım"),
]


def setup_logging(logs_dir: Path) -> None:
    """Konsol + günlük dönen dosya loglaması (14 gün saklanır)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = TimedRotatingFileHandler(
        logs_dir / "bot.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    # Gürültülü kütüphaneleri kıs
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cleanup_old_temp(temp_dir: Path, max_age_days: int = _TEMP_MAX_AGE_DAYS) -> None:
    """Açılışta 7 günden eski geçici dosyaları siler."""
    if not temp_dir.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in temp_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Eski geçici dosya silinemedi: %s", path)
    if removed:
        logger.info("%d eski geçici dosya temizlendi.", removed)


async def run_bot(config: Config) -> None:
    """Botu hazırlar ve long polling'i başlatır."""
    ensure_ffmpeg()
    db.init_db(config.db_path)
    db.sync_images_dir(config.images_dir)
    cleanup_old_temp(config.temp_dir)

    bot = Bot(token=config.telegram_bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())

    guard = WhitelistMiddleware(config.allowed_user_ids, bot)
    dispatcher.message.outer_middleware(guard)
    dispatcher.callback_query.outer_middleware(guard)

    for router in all_routers():
        dispatcher.include_router(router)

    await bot.set_my_commands(_BOT_COMMANDS)
    me = await bot.get_me()
    logger.info("Bot başladı: @%s (izinli kullanıcılar: %s)", me.username, config.allowed_user_ids)
    
    for user_id in config.allowed_user_ids:
        try:
            await bot.send_message(user_id, "🤖 **Bot aktif ve hazır!**")
        except Exception:
            pass
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def kill_other_instances() -> None:
    """Kendi PID'miz dışındaki diğer python -m bot süreçlerini bulur ve sonlandırır."""
    import os
    import signal
    try:
        my_pid = os.getpid()
        if sys.platform.startswith("linux"):
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.is_dir() or not pid_dir.name.isdigit():
                    continue
                pid = int(pid_dir.name)
                if pid == my_pid:
                    continue
                try:
                    cmdline_file = pid_dir / "cmdline"
                    if cmdline_file.exists():
                        cmdline = cmdline_file.read_text().replace("\x00", " ")
                        if "-m bot" in cmdline or "python -m bot" in cmdline:
                            # logging might not be fully configured yet, use print and fallback logger
                            print(f"Diğer bot süreci bulundu (PID {pid}), sonlandırılıyor...")
                            os.kill(pid, signal.SIGKILL)
                            time.sleep(0.5)
                except Exception:
                    pass
    except Exception as exc:
        print(f"Diğer süreçler kontrol edilirken hata: {exc}", file=sys.stderr)


def main() -> int:
    kill_other_instances()
    parser = argparse.ArgumentParser(
        prog="python -m bot",
        description="Telegram onaylı Drive → YouTube yükleme botu",
    )
    parser.add_argument(
        "--login-setup",
        action="store_true",
        help="Görünür Chrome açar; Google hesabına elle giriş yapıp oturumu profile kaydeder.",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Yapılandırma hatası: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.logs_dir)

    try:
        if args.login_setup:
            asyncio.run(login_setup(config))
        else:
            asyncio.run(run_bot(config))
    except VideoError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Bot durduruldu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
