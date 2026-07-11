from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot

from ..config import Config
from . import db, drive

logger = logging.getLogger(__name__)

def get_seconds_until_next_run(now: datetime) -> float:
    """Hedef saatlere (09:00, 14:00, 18:00) kalan süreyi saniye cinsinden hesaplar."""
    targets = []
    for h in [9, 14, 18]:
        target_dt = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if target_dt > now:
            targets.append(target_dt)
            
    if not targets:
        # Eğer bugünün tüm saatleri geçtiyse, bir sonraki günün 09:00'unu hedefle
        tomorrow = now + timedelta(days=1)
        next_dt = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        next_dt = min(targets)
        
    return (next_dt - now).total_seconds()

async def check_pending_uploads(bot: Bot, config: Config) -> None:
    """Drive taraması yapar ve her iki kanala da yüklenmemiş klasörleri kullanıcılara bildirir."""
    logger.info("Zamanlanmış Drive taraması başlatılıyor...")
    try:
        # Drive alt klasörlerini listele
        folders = await asyncio.to_thread(drive.list_subfolders, config.drive_root_folder_id)
        
        # Veritabanı ile eşitle
        new_folders = db.sync_folders(folders)
        all_uploads = db.get_all_folder_uploads()
        
        # Hiçbir kanala yüklenmemiş (yükleme sayısı 0 olan) klasörleri filtrele
        pending_folders = []
        for f in new_folders:
            uploaded = all_uploads.get(f['folder_id'], [])
            if not uploaded:  # 0 kanala yüklendiyse (yani hiçbir kanala yüklenmemişse)
                pending_folders.append(f)
                
        if pending_folders:
            logger.info("Zamanlanmış tarama: %d adet bekleyen klasör bulundu. Bildirim gönderiliyor...", len(pending_folders))
            folder_list_str = "\n".join(f"• {f['folder_name']}" for f in pending_folders)
            msg_text = (
                "⚠️ <b>Bekleyen yüklemeler var!</b>\n\n"
                "Aşağıdaki klasörler henüz hiçbir kanala yüklenmedi:\n"
                f"{folder_list_str}\n\n"
                "Yükleme işlemini başlatmak için /baslat yazabilirsiniz."
            )
            for user_id in config.allowed_user_ids:
                try:
                    await bot.send_message(user_id, msg_text)
                    logger.info("Bildirim %s kullanıcısına gönderildi.", user_id)
                except Exception:
                    logger.exception("Bildirim %s kullanıcısına gönderilirken hata oluştu.", user_id)
        else:
            logger.info("Zamanlanmış tarama: Bekleyen klasör bulunamadı.")
    except Exception:
        logger.exception("Zamanlanmış tarama ve bildirim akışında hata oluştu.")

async def scheduler_loop(bot: Bot, config: Config) -> None:
    """Zamanlayıcı döngüsü: Belirlenen saatlerde tetiklenir."""
    logger.info("Zamanlayıcı döngüsü başlatıldı (Hedef saatler: 09:00, 14:00, 18:00).")
    while True:
        try:
            now = datetime.now()
            sleep_seconds = get_seconds_until_next_run(now)
            next_run_time = now + timedelta(seconds=sleep_seconds)
            logger.info("Bir sonraki tarama zamanı: %s (%d saniye sonra)", next_run_time.strftime('%Y-%m-%d %H:%M:%S'), int(sleep_seconds))
            
            await asyncio.sleep(sleep_seconds)
            
            # Zamanı geldiğinde taramayı çalıştır
            await check_pending_uploads(bot, config)
            
            # Aynı saniye içinde birden fazla çalışmayı önlemek için kısa bir süre bekle
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info("Zamanlayıcı döngüsü iptal edildi.")
            break
        except Exception:
            logger.exception("Zamanlayıcı döngüsünde beklenmeyen hata. 60 saniye sonra tekrar denenecek...")
            await asyncio.sleep(60)

def start_scheduler(bot: Bot, config: Config) -> asyncio.Task:
    """Zamanlayıcıyı arka plan görevi olarak başlatır."""
    return asyncio.create_task(scheduler_loop(bot, config))
