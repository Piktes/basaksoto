"""SQLite katmanı: klasör geçmişi, kanallar, görsel kütüphanesi, yüklemeler, ayarlar.

Tüm fonksiyonlar senkron ve kısa işlemlidir; handler'lardan doğrudan çağrılabilir
(tek kullanıcılı bot için yeterli, kilitlenme riski yok).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_db_path: Path | None = None

# Klasör durumları
STATUS_NEW = "yeni"
STATUS_UPLOADED = "yuklendi"
STATUS_SKIPPED = "atlandi"
STATUS_ERROR = "hata"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    folder_id     TEXT PRIMARY KEY,
    folder_name   TEXT NOT NULL,
    created_time  TEXT,
    first_seen_at TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'yeni',
    video_url     TEXT,
    uploaded_at   TEXT,
    channel_name  TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    added_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id     TEXT REFERENCES folders(folder_id),
    title         TEXT NOT NULL,
    channel_name  TEXT NOT NULL,
    video_url     TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("db.init_db() çağrılmadan veritabanı kullanılamaz.")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path) -> None:
    """Veritabanını açar ve şemayı (idempotent) kurar."""
    global _db_path
    _db_path = path
    with closing(_connect()) as conn, conn:
        conn.executescript(_SCHEMA)
    logger.info("SQLite hazır: %s", path)


# ---------------------------------------------------------------- folders

def sync_folders(drive_folders: list[dict[str, Any]]) -> list[sqlite3.Row]:
    """Drive'dan gelen klasörleri DB ile eşitler; DB'de olmayanları 'yeni' ekler.

    Args:
        drive_folders: her biri ``{"id", "name", "createdTime"}`` içeren sözlükler.

    Returns:
        Eklenenler dahil, durumu 'yeni' veya 'hata' olan tüm klasör satırları
        (hata alanlar yeniden denenebilsin diye listede kalır).
    """
    with closing(_connect()) as conn, conn:
        for f in drive_folders:
            conn.execute(
                "INSERT OR IGNORE INTO folders (folder_id, folder_name, created_time, first_seen_at)"
                " VALUES (?, ?, ?, ?)",
                (f["id"], f["name"], f.get("createdTime"), _now()),
            )
        rows = conn.execute(
            "SELECT * FROM folders WHERE status IN (?, ?) ORDER BY created_time DESC",
            (STATUS_NEW, STATUS_ERROR),
        ).fetchall()
    return rows


def get_folder(folder_id: str) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()


def set_folder_status(folder_id: str, status: str, *, video_url: str | None = None,
                      channel_name: str | None = None) -> None:
    """Klasör durumunu günceller; 'yuklendi' ise url/kanal/tarih de yazılır."""
    with closing(_connect()) as conn, conn:
        if status == STATUS_UPLOADED:
            conn.execute(
                "UPDATE folders SET status=?, video_url=?, channel_name=?, uploaded_at=?"
                " WHERE folder_id=?",
                (status, video_url, channel_name, _now(), folder_id),
            )
        else:
            conn.execute("UPDATE folders SET status=? WHERE folder_id=?", (status, folder_id))


# ---------------------------------------------------------------- uploads

def record_upload(folder_id: str, title: str, channel_name: str, video_url: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO uploads (folder_id, title, channel_name, video_url, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (folder_id, title, channel_name, video_url, _now()),
        )


def last_uploads(limit: int = 10) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT * FROM uploads ORDER BY uploaded_at DESC LIMIT ?", (limit,)
        ).fetchall()


def upload_stats() -> dict[str, int]:
    """Bu hafta (Pazartesi'den beri) / bu ay / toplam yükleme sayıları."""
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with closing(_connect()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        week = conn.execute(
            "SELECT COUNT(*) FROM uploads WHERE uploaded_at >= ?", (week_start.isoformat(),)
        ).fetchone()[0]
        month = conn.execute(
            "SELECT COUNT(*) FROM uploads WHERE uploaded_at >= ?", (month_start.isoformat(),)
        ).fetchone()[0]
    return {"week": week, "month": month, "total": total}


def uploads_by_channel() -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT channel_name, COUNT(*) AS cnt FROM uploads GROUP BY channel_name ORDER BY cnt DESC"
        ).fetchall()


# ---------------------------------------------------------------- channels

def list_channels() -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM channels ORDER BY id").fetchall()


def add_channel(display_name: str) -> bool:
    """Kanal ekler; aynı ad zaten varsa False döner."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("INSERT INTO channels (display_name) VALUES (?)", (display_name,))
        return True
    except sqlite3.IntegrityError:
        return False


def get_channel(channel_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()


def delete_channel(channel_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))


# ---------------------------------------------------------------- images

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def sync_images_dir(images_dir: Path) -> None:
    """data/images/ içindeki dosyaları images tablosuyla eşitler.

    Kullanıcının elle kopyaladığı görseller (ör. 2 default görsel) böylece
    kütüphanede görünür; diskten silinenler tablodan düşülür.
    """
    on_disk = {p.resolve() for p in images_dir.iterdir()
               if p.is_file() and p.suffix.lower() in _IMAGE_EXTS} if images_dir.exists() else set()
    with closing(_connect()) as conn, conn:
        rows = conn.execute("SELECT id, file_path FROM images").fetchall()
        known: set[Path] = set()
        for row in rows:
            p = Path(row["file_path"]).resolve()
            if p in on_disk:
                known.add(p)
            else:
                conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
        for p in sorted(on_disk - known):
            conn.execute(
                "INSERT OR IGNORE INTO images (file_path, display_name, added_at) VALUES (?, ?, ?)",
                (str(p), p.stem, _now()),
            )


def list_images() -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM images ORDER BY id").fetchall()


def get_image(image_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


def add_image(file_path: Path, display_name: str) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO images (file_path, display_name, added_at) VALUES (?, ?, ?)",
            (str(file_path.resolve()), display_name, _now()),
        )
        return int(cur.lastrowid)


def delete_image(image_id: int) -> sqlite3.Row | None:
    """Kaydı siler ve silinen satırı döndürür (dosya silme çağırana aittir)."""
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        return row


# ---------------------------------------------------------------- settings

def get_setting(key: str) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
