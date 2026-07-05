"""Yapılandırma: .env dosyasını okur, doğrular ve tekil Config nesnesi sunar."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = {"1", "true", "yes", "on", "evet"}


class ConfigError(RuntimeError):
    """Eksik veya hatalı .env değeri."""


def _resolve(path_str: str) -> Path:
    """Göreli yolları proje köküne göre mutlaklaştırır."""
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Config:
    """Bot yapılandırması (tümü .env'den yüklenir)."""

    telegram_bot_token: str
    allowed_user_ids: tuple[int, ...]
    drive_root_folder_id: str
    default_thumbnail: Path
    temp_dir: Path
    browser_profile_dir: Path
    headless: bool
    upload_timeout_minutes: int

    # Sabit yollar (proje köküne göre)
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    images_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "images")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "bot.db")
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    credentials_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "credentials")

    @property
    def client_secret_path(self) -> Path:
        return self.credentials_dir / "client_secret.json"

    @property
    def token_path(self) -> Path:
        return self.credentials_dir / "token.json"

    @property
    def primary_admin_id(self) -> int:
        """Güvenlik bildirimlerini alan ilk whitelist kullanıcısı."""
        return self.allowed_user_ids[0]


_config: Config | None = None


def load_config() -> Config:
    """.env'i yükler, zorunlu alanları doğrular ve dizinleri oluşturur."""
    load_dotenv(PROJECT_ROOT / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN boş. .env dosyasını .env.example'a göre doldurun.")

    raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
    try:
        ids = tuple(int(part.strip()) for part in raw_ids.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError(f"ALLOWED_USER_IDS sayısal ID listesi olmalı (virgülle ayrılmış): {raw_ids!r}") from exc
    if not ids:
        raise ConfigError("ALLOWED_USER_IDS boş — en az bir Telegram user ID gerekli.")

    root_folder = os.getenv("DRIVE_ROOT_FOLDER_ID", "").strip()
    if not root_folder:
        raise ConfigError("DRIVE_ROOT_FOLDER_ID boş. Drive ana klasör ID'sini .env'e yazın.")

    try:
        timeout_min = int(os.getenv("UPLOAD_TIMEOUT_MINUTES", "30"))
    except ValueError as exc:
        raise ConfigError("UPLOAD_TIMEOUT_MINUTES tam sayı olmalı.") from exc

    cfg = Config(
        telegram_bot_token=token,
        allowed_user_ids=ids,
        drive_root_folder_id=root_folder,
        default_thumbnail=_resolve(os.getenv("DEFAULT_THUMBNAIL", "data/images/default_thumbnail.jpg")),
        temp_dir=_resolve(os.getenv("TEMP_DIR", "data/temp")),
        browser_profile_dir=_resolve(os.getenv("BROWSER_PROFILE_DIR", "data/browser_profile")),
        headless=os.getenv("HEADLESS", "false").strip().lower() in _TRUTHY,
        upload_timeout_minutes=timeout_min,
    )

    for directory in (cfg.data_dir, cfg.images_dir, cfg.temp_dir, cfg.browser_profile_dir,
                      cfg.logs_dir, cfg.credentials_dir):
        directory.mkdir(parents=True, exist_ok=True)

    global _config
    _config = cfg
    return cfg


def get_config() -> Config:
    """Yüklenmiş Config'i döndürür (önce load_config çağrılmış olmalı)."""
    if _config is None:
        return load_config()
    return _config
