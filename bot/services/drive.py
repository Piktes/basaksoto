"""Google Drive API v3 servisi (salt okuma).

Yalnızca ``drive.readonly`` scope'u kullanılır. İlk çalıştırmada tarayıcıdan
OAuth yetkilendirmesi yapılır; refresh token ``credentials/token.json``'da
saklanır ve otomatik yenilenir.

Buradaki fonksiyonlar senkrondur; async handler'lardan ``asyncio.to_thread``
ile çağrılmalıdır.
"""

from __future__ import annotations

import functools
import io
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from ..config import get_config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac")
DOCX_EXTENSION = ".docx"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FOLDER_MIME = "application/vnd.google-apps.folder"

_MAX_RETRIES = 4

T = TypeVar("T")


class DriveError(RuntimeError):
    """Drive erişim hatası (kullanıcıya gösterilebilir Türkçe mesaj taşır)."""


def _with_retry(func: Callable[..., T]) -> Callable[..., T]:
    """5xx ve bağlantı hatalarında exponential backoff ile yeniden dener."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        delay = 1.0
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except HttpError as exc:
                status = exc.resp.status if exc.resp else 0
                if status < 500 or attempt == _MAX_RETRIES:
                    raise
                logger.warning("Drive %s hatası (deneme %d/%d), %.1fs sonra tekrar: %s",
                               status, attempt, _MAX_RETRIES, delay, func.__name__)
            except (ConnectionError, TimeoutError, OSError):
                if attempt == _MAX_RETRIES:
                    raise
                logger.warning("Drive bağlantı hatası (deneme %d/%d), %.1fs sonra tekrar: %s",
                               attempt, _MAX_RETRIES, delay, func.__name__)
            time.sleep(delay + random.uniform(0, 0.5))
            delay *= 2
        raise DriveError("Drive isteği tekrar denemelere rağmen başarısız oldu.")

    return wrapper


_service: Any = None


def get_service() -> Any:
    """Yetkili Drive API istemcisini döndürür (gerekirse OAuth akışı başlatır)."""
    global _service
    if _service is not None:
        return _service

    cfg = get_config()
    creds: Credentials | None = None
    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Drive token yenileniyor...")
            creds.refresh(Request())
        else:
            if not cfg.client_secret_path.exists():
                raise DriveError(
                    "credentials/client_secret.json bulunamadı. README'deki Google Cloud "
                    "kurulum adımlarını tamamlayın."
                )
            logger.info("Drive OAuth yetkilendirmesi başlatılıyor (tarayıcı açılacak)...")
            flow = InstalledAppFlow.from_client_secrets_file(str(cfg.client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        cfg.token_path.write_text(creds.to_json(), encoding="utf-8")

    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


@_with_retry
def list_subfolders(root_folder_id: str) -> list[dict[str, Any]]:
    """Ana klasörün birinci seviye alt klasörlerini listeler.

    Returns:
        ``{"id", "name", "createdTime"}`` sözlükleri, oluşturulma tarihine göre.
    """
    service = get_service()
    folders: list[dict[str, Any]] = []
    page_token: str | None = None
    query = f"'{root_folder_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false"
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return folders


@_with_retry
def list_files(folder_id: str) -> list[dict[str, Any]]:
    """Bir klasörün içindeki (klasör olmayan) dosyaları listeler."""
    service = get_service()
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and mimeType!='{FOLDER_MIME}' and trashed=false"
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            orderBy="name",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


def find_audio_and_docx(files: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Dosya listesinden ilk audio ve ilk Word dosyasını seçer.

    Google Doküman'lar da (docx'e dönüştürülerek indirilebildiği için) Word
    dosyası sayılır.
    """
    audio = next((f for f in files if f["name"].lower().endswith(AUDIO_EXTENSIONS)), None)
    docx = next(
        (f for f in files
         if f["name"].lower().endswith(DOCX_EXTENSION) or f.get("mimeType") == GOOGLE_DOC_MIME),
        None,
    )
    return audio, docx


@_with_retry
def download_file(file: dict[str, Any], destination: Path) -> Path:
    """Dosyayı indirir; Google Doküman ise .docx olarak dışa aktarır."""
    service = get_service()
    if file.get("mimeType") == GOOGLE_DOC_MIME:
        request = service.files().export_media(fileId=file["id"], mimeType=DOCX_MIME)
    else:
        request = service.files().get_media(fileId=file["id"])

    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    destination.write_bytes(buffer.getvalue())
    logger.info("İndirildi: %s → %s (%d bayt)", file["name"], destination, destination.stat().st_size)
    return destination


@_with_retry
def trash_file(file_id: str) -> None:
    """Klasörü veya dosyayı Drive çöp kutusuna taşır."""
    service = get_service()
    service.files().update(fileId=file_id, body={"trashed": True}).execute()
    logger.info("Drive çöp kutusuna taşındı: %s", file_id)

