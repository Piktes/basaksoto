"""ffmpeg ile görsel + ses → 1920×1080 H.264/AAC mp4 üretimi."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoError(RuntimeError):
    """ffmpeg üretim hatası."""


def ensure_ffmpeg() -> None:
    """ffmpeg PATH'te değilse anlaşılır bir hata fırlatır (bot açılışında çağrılır)."""
    if shutil.which("ffmpeg") is None:
        raise VideoError(
            "ffmpeg bulunamadı. Kurulum: Windows'ta 'winget install ffmpeg', "
            "Linux'ta 'sudo apt install ffmpeg'. Kurulumdan sonra terminali yeniden açın."
        )


async def create_video(image_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Görseli letterbox'layarak ses süresi kadar 1080p mp4 üretir.

    Görselin en-boy oranı bozulmaz; boşluklar siyah pad ile doldurulur.
    Süre ``-shortest`` ile ses dosyasının süresine eşitlenir.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    logger.info("ffmpeg başlıyor: %s + %s → %s", image_path.name, audio_path.name, output_path.name)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    stderr_text = stderr.decode("utf-8", errors="replace")

    if process.returncode != 0:
        tail = "\n".join(stderr_text.strip().splitlines()[-8:])
        logger.error("ffmpeg hata (kod %s):\n%s", process.returncode, stderr_text)
        raise VideoError(f"ffmpeg video üretemedi (çıkış kodu {process.returncode}):\n{tail}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise VideoError("ffmpeg bitti ama çıktı dosyası oluşmadı.")

    logger.info("Video hazır: %s (%.1f MB)", output_path, output_path.stat().st_size / 1_048_576)
    return output_path
