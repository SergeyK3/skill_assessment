# route: (urls) | file: skill_assessment/services/public_url.py
"""Базовый URL для ссылок в Telegram: на телефоне недоступны localhost и 127.0.0.1."""

from __future__ import annotations

import os


def skill_assessment_public_base_url_for_device_links() -> str | None:
    """
    Возвращает ``SKILL_ASSESSMENT_PUBLIC_BASE_URL``, если он задан и не указывает на локальную машину.

    Для ссылок в SMS/Telegram на телефоне нужен реальный хост (например HTTPS из ngrok).
    """
    base = (os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    low = base.lower()
    if "127.0.0.1" in low or "localhost" in low or "0.0.0.0" in low:
        return None
    return base
