# route: (tests) | file: tests/test_telegram_docs_survey.py
"""Разбор callback и клавиатура опроса по документам (без Telegram API)."""

from __future__ import annotations

from unittest.mock import MagicMock

from skill_assessment.services import telegram_docs_survey as tds
from skill_assessment.services.telegram_docs_survey_consent import build_pd_consent_inline_keyboard
from skill_assessment.services.telegram_docs_survey_readiness import build_readiness_inline_keyboard


def test_readiness_buttons_fit_callback_limit() -> None:
    kb = build_readiness_inline_keyboard("550e8400-e29b-41d4-a716-446655440000")
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_pd_consent_buttons_fit_callback_limit() -> None:
    kb = build_pd_consent_inline_keyboard("550e8400-e29b-41d4-a716-446655440000")
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_build_keyboards_fit_callback_limit() -> None:
    kb = tds.build_docs_survey_slot_keyboard("550e8400-e29b-41d4-a716-446655440000")
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_three_workday_keyboard_fits_callback_limit() -> None:
    kb = tds.build_docs_survey_slot_keyboard_days("550e8400-e29b-41d4-a716-446655440000", 3)
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_handle_callback_invalid_prefix_returns_none() -> None:
    db = MagicMock()
    assert tds.handle_docs_survey_callback(db, "1", "noop|x", "q") is None
