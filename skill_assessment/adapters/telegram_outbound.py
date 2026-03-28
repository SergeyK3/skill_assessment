# route: (telegram outbound) | file: skill_assessment/adapters/telegram_outbound.py
"""
Исходящие сообщения в Telegram Bot API.

- **http** — реальный ``httpx.post`` (нужен ``TELEGRAM_BOT_TOKEN``).
- **mock** — in-memory очередь + лог, без сети (CI, автотесты, локальный API с тестовой БД).

Переключение: переменная окружения ``SKILL_ASSESSMENT_TELEGRAM_OUTBOUND`` = ``mock`` | ``http``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramOutboundResult:
    ok: bool
    http_status: int | None = None
    description: str | None = None


class FakeTelegramOutbound:
    """Заглушка: записывает «отправки» в память (для проверок в тестах)."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> TelegramOutboundResult:
        self.messages.append(
            {
                "token_set": bool(token and len(token) >= 10),
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        _log.debug("fake_telegram: queued message to chat_id=%s", chat_id[:12] if chat_id else "")
        return TelegramOutboundResult(ok=True, http_status=200, description="fake_ok")

    def clear(self) -> None:
        self.messages.clear()


_fake_singleton: FakeTelegramOutbound | None = None


def clear_fake_telegram_outbound() -> None:
    """Сбросить очередь заглушки (между тестами)."""
    global _fake_singleton
    if _fake_singleton is not None:
        _fake_singleton.clear()


class HttpxTelegramOutbound:
    """Реальная отправка через Telegram Bot API."""

    def send_message(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> TelegramOutboundResult:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            r = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
                timeout=20.0,
            )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if not r.is_success:
                detail = data.get("description") if isinstance(data, dict) else r.text[:300]
                return TelegramOutboundResult(ok=False, http_status=r.status_code, description=str(detail))
            if isinstance(data, dict) and data.get("ok"):
                return TelegramOutboundResult(ok=True, http_status=r.status_code, description=None)
            return TelegramOutboundResult(ok=False, http_status=r.status_code, description=str(data)[:400])
        except Exception as e:
            _log.exception("telegram httpx send failed")
            return TelegramOutboundResult(ok=False, http_status=None, description=str(e)[:200])


def get_telegram_outbound() -> FakeTelegramOutbound | HttpxTelegramOutbound:
    """Фабрика: mock или реальный HTTP по ``SKILL_ASSESSMENT_TELEGRAM_OUTBOUND``."""
    raw = os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "").strip().lower()
    if raw == "mock":
        global _fake_singleton
        if _fake_singleton is None:
            _fake_singleton = FakeTelegramOutbound()
        return _fake_singleton
    return HttpxTelegramOutbound()
