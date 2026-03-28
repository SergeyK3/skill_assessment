# route: (pytest) | file: tests/test_docs_survey_time.py
"""Локальный слот опроса по документам → UTC в БД."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest


def test_local_moscow_16_00_to_utc_naive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_SURVEY_LOCAL_TIMEZONE", "Europe/Moscow")
    from skill_assessment.services.docs_survey_time import local_slot_to_utc_naive

    d = date(2026, 3, 27)
    utc_naive = local_slot_to_utc_naive(d, 16, 0)
    # MSK = UTC+3 → 16:00 Москва = 13:00 UTC
    assert utc_naive.tzinfo is None
    assert utc_naive == datetime(2026, 3, 27, 13, 0, 0)


def test_utc_naive_to_aware_for_api(monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_assessment.services.docs_survey_time import utc_naive_to_aware_utc

    assert utc_naive_to_aware_utc(None) is None
    n = datetime(2026, 3, 27, 13, 0, 0)
    a = utc_naive_to_aware_utc(n)
    assert a is not None
    assert a.tzinfo == timezone.utc
    assert a.hour == 13


def test_reminder_minutes_before_invalid_env_falls_back_to_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_SURVEY_REMINDER_MINUTES_BEFORE", "not-a-number")
    from skill_assessment.services.docs_survey_time import reminder_minutes_before

    assert reminder_minutes_before() == 5


def test_docs_survey_hr_hint_contains_reminder_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_SURVEY_LOCAL_TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("DOCS_SURVEY_REMINDER_MINUTES_BEFORE", "30")
    from skill_assessment.services.docs_survey_time import docs_survey_hr_labels, reminder_minutes_before

    assert reminder_minutes_before() == 30
    # Слот 16:00 MSK = 13:00 UTC; напоминание ~15:30 MSK
    sched = datetime(2026, 3, 27, 13, 0, 0)
    hr = docs_survey_hr_labels(
        docs_survey_scheduled_at=sched,
        docs_survey_reminder_30m_sent_at=None,
        docs_survey_pd_consent_status="accepted",
    )
    assert hr["docs_survey_reminder_minutes_before"] == 30
    assert hr["docs_survey_slot_local_label"] and "16:00" in hr["docs_survey_slot_local_label"]
    assert hr["docs_survey_reminder_telegram_local_label"] and "15:30" in hr["docs_survey_reminder_telegram_local_label"]
    assert hr["docs_survey_telegram_schedule_hint"] and "30" in hr["docs_survey_telegram_schedule_hint"]
    assert hr["docs_survey_minutes_until_slot"] is not None
    assert hr["docs_survey_minutes_until_reminder"] is not None


def test_docs_survey_hr_no_telegram_reminder_hint_when_pd_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_SURVEY_LOCAL_TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("DOCS_SURVEY_REMINDER_MINUTES_BEFORE", "5")
    from skill_assessment.services.docs_survey_time import docs_survey_hr_labels

    sched = datetime(2026, 3, 27, 13, 0, 0)
    hr = docs_survey_hr_labels(
        docs_survey_scheduled_at=sched,
        docs_survey_reminder_30m_sent_at=None,
        docs_survey_pd_consent_status="timed_out",
    )
    assert hr["docs_survey_slot_local_label"]
    assert hr["docs_survey_minutes_until_slot"] is not None
    assert hr["docs_survey_minutes_until_reminder"] is None
    assert hr["docs_survey_reminder_telegram_local_label"] is None
    assert hr["docs_survey_telegram_schedule_hint"] and "не отправляется" in hr["docs_survey_telegram_schedule_hint"]


def test_reminder_send_window_and_catch_up() -> None:
    from skill_assessment.bootstrap import ensure_typical_infrastructure_on_path

    ensure_typical_infrastructure_on_path()
    from skill_assessment.services.docs_survey_reminder_30m import _should_send_reminder_now

    t = 5
    assert _should_send_reminder_now(5.0, t)
    assert _should_send_reminder_now(4.0, t)
    assert _should_send_reminder_now(6.0, t)
    assert _should_send_reminder_now(3.0, t)  # догон после пропуска узкого окна
    assert not _should_send_reminder_now(8.0, t)  # слишком рано
    assert not _should_send_reminder_now(0.0, t)
    assert not _should_send_reminder_now(-1.0, t)
