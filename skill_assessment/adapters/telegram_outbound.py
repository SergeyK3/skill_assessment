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
import time
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
            timeout_sec = float((os.getenv("TELEGRAM_HTTP_TIMEOUT_SECONDS") or "45").strip() or "45")
        except ValueError:
            timeout_sec = 45.0
        try:
            max_attempts = max(1, min(8, int((os.getenv("TELEGRAM_SEND_MAX_ATTEMPTS") or "3").strip() or "3")))
        except ValueError:
            max_attempts = 3

        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        # Telegram Bot API ожидает объект в reply_markup; null даёт 400 "object expected as reply markup".
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        last_err: str | None = None
        last_status: int | None = None

        for attempt in range(max_attempts):
            try:
                r = httpx.post(url, json=payload, timeout=timeout_sec)
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                last_status = r.status_code
                if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
                    detail = data.get("description") if isinstance(data, dict) else r.text[:200]
                    last_err = str(detail) if detail else f"http_{r.status_code}"
                    wait = min(8.0, 1.5 * (2**attempt))
                    _log.warning(
                        "telegram sendMessage transient %s (attempt %s/%s), retry in %.1fs",
                        r.status_code,
                        attempt + 1,
                        max_attempts,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                if not r.is_success:
                    detail = data.get("description") if isinstance(data, dict) else r.text[:300]
                    return TelegramOutboundResult(ok=False, http_status=r.status_code, description=str(detail))
                if isinstance(data, dict) and data.get("ok"):
                    return TelegramOutboundResult(ok=True, http_status=r.status_code, description=None)
                return TelegramOutboundResult(ok=False, http_status=r.status_code, description=str(data)[:400])
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.WriteError) as e:
                last_err = str(e)[:200]
                if attempt + 1 < max_attempts:
                    wait = min(8.0, 1.5 * (2**attempt))
                    _log.warning(
                        "telegram sendMessage network error (attempt %s/%s): %s; retry in %.1fs",
                        attempt + 1,
                        max_attempts,
                        last_err,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                _log.exception("telegram httpx send failed after %s attempts", max_attempts)
                return TelegramOutboundResult(ok=False, http_status=last_status, description=last_err)
            except Exception as e:
                _log.exception("telegram httpx send failed")
                return TelegramOutboundResult(ok=False, http_status=last_status, description=str(e)[:200])

        return TelegramOutboundResult(ok=False, http_status=last_status, description=last_err or "send_failed")


def get_telegram_outbound() -> FakeTelegramOutbound | HttpxTelegramOutbound:
    """Фабрика: mock или реальный HTTP по ``SKILL_ASSESSMENT_TELEGRAM_OUTBOUND``."""
    raw = os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "").strip().lower()
    if raw == "mock":
        global _fake_singleton
        if _fake_singleton is None:
            _fake_singleton = FakeTelegramOutbound()
        return _fake_singleton
    return HttpxTelegramOutbound()
