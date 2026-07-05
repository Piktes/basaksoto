"""Whitelist güvenlik middleware'i.

Mesaj ve callback query'lerde gönderen ALLOWED_USER_IDS'te değilse hiç cevap
verilmez (sessiz mod); whitelist'teki ilk kullanıcıya saatte en fazla 1 kez
yetkisiz erişim bildirimi gönderilir.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, User

logger = logging.getLogger(__name__)

_NOTIFY_COOLDOWN_SECONDS = 3600


class WhitelistMiddleware(BaseMiddleware):
    """Yalnızca izinli kullanıcıların update'lerini handler'lara geçirir."""

    def __init__(self, allowed_ids: tuple[int, ...], bot: Bot) -> None:
        self._allowed = set(allowed_ids)
        self._primary_admin = allowed_ids[0]
        self._bot = bot
        self._last_notified: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id in self._allowed:
            return await handler(event, data)

        if user is not None:
            await self._notify_unauthorized(user)
        # Sessiz mod: yetkisiz kullanıcıya hiçbir cevap dönmez.
        return None

    async def _notify_unauthorized(self, user: User) -> None:
        now = time.monotonic()
        last = self._last_notified.get(user.id)
        if last is not None and now - last < _NOTIFY_COOLDOWN_SECONDS:
            return
        self._last_notified[user.id] = now
        username = f"@{user.username}" if user.username else "(kullanıcı adı yok)"
        logger.warning("Yetkisiz erişim denemesi: id=%s username=%s", user.id, username)
        try:
            await self._bot.send_message(
                self._primary_admin,
                f"⚠️ Yetkisiz erişim denemesi: ID <code>{user.id}</code>, {username}",
            )
        except Exception:  # noqa: BLE001 — bildirim hatası akışı bozmamalı
            logger.exception("Yetkisiz erişim bildirimi gönderilemedi.")
