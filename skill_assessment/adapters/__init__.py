# route: (adapters) | file: skill_assessment/adapters/__init__.py
"""Внешние каналы: заглушки и реальные клиенты для тестов без телефонов и Telegram API."""

from skill_assessment.adapters.telegram_outbound import (
    FakeTelegramOutbound,
    HttpxTelegramOutbound,
    TelegramOutboundResult,
    clear_fake_telegram_outbound,
    get_telegram_outbound,
)

__all__ = [
    "FakeTelegramOutbound",
    "HttpxTelegramOutbound",
    "TelegramOutboundResult",
    "clear_fake_telegram_outbound",
    "get_telegram_outbound",
]
